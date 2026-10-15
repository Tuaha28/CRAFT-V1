"""Authoritative P2 solve after exact CalendarExecutionRegime compression."""
import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, '.')
from ortools.sat.python import cp_model

from crafter.phenotype import validate
from crafter.repair.calendar_regimes import _execution
from crafter.repair.cp_repair import build_repair_model
from crafter.repair.engine import run_p2_repair, _routing_aware_earliest
from crafter.lns_probe.joint_modes import JointExecutableMode
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode

OUT = Path('outputs/crafter/p2_compressed_search_v5')
OUT.mkdir(parents=True, exist_ok=True)


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def sig(config):
    return (int(config.line_id), int(config.k), tuple(config.machine_ids), tuple(config.worker_ids))


def incumbent_choice(state, key, configs):
    item = next(x for x in state['baseline'].operations if x.assignment.operation_key == key)
    wanted = (int(item.assignment.line_id), int(item.assignment.machine_count),
              tuple(sorted(int(x) for x in item.assignment.machine_ids)),
              tuple(sorted(str(x) for x in item.employee_ids)))
    exact = next((c for c in configs if sig(c) == wanted), None)
    if exact is not None:
        return exact, {'status': 'EXACT_INCUMBENT_CONFIGURATION', 'signature': wanted}
    # This is explicitly a deterministic seed, never described as incumbent.
    fallback = sorted(configs, key=sig)[0]
    return fallback, {'status': 'INCUMBENT_CONFIGURATION_ABSENT_DETERMINISTIC_FALLBACK',
                      'requested_signature': wanted, 'fallback_signature': sig(fallback)}


def diverse(configs, anchor, limit):
    selected = [anchor]
    remaining = [c for c in configs if c != anchor]
    while remaining and len(selected) < limit:
        def score(c):
            distance = min(
                len(set(c.worker_ids) ^ set(s.worker_ids)) * 4
                + len(set(c.machine_ids) ^ set(s.machine_ids)) * 3
                + (c.k != s.k) * 2 + (c.line_id != s.line_id) * 2
                for s in selected)
            return (distance, sig(c))
        choice = max(remaining, key=score)
        selected.append(choice); remaining.remove(choice)
    return tuple(selected)


def restrict_regimes(state, regimes, selected_configs, window_days):
    bounds = _routing_aware_earliest(state, regimes)
    restricted = {}
    for key in sorted(state['keys']):
        allowed = set(selected_configs[key])
        lower = int(bounds['earliest_start'].get(key, 0))
        upper = int(state['problem'].horizon_seconds)
        if window_days is not None:
            upper = min(upper, lower + int(window_days * 86400))
        rows = []
        for regime in regimes[key]:
            if regime['config'] not in allowed:
                continue
            lo, hi = max(int(regime['lo']), lower), min(int(regime['hi']), upper)
            if lo <= hi:
                rows.append({**regime, 'lo': lo, 'hi': hi})
        restricted[key] = tuple(rows)
    return restricted, bounds


def add_hints(state, regimes, artifact, bounds):
    provenance = []
    for key in sorted(state['keys']):
        wanted = int(bounds['earliest_start'].get(key, 0))
        options = [(i, r) for i, r in enumerate(regimes[key])
                   if int(r['lo']) <= wanted <= int(r['hi'])]
        if not options:
            continue
        i, r = options[0]
        a, b = r['formula'][0]
        artifact['model'].AddHint(artifact['reg_lit'][key][i], 1)
        artifact['model'].AddHint(artifact['starts'][key], wanted)
        artifact['model'].AddHint(artifact['ends'][key], int(a) * wanted + int(b))
        provenance.append({'operation_key': key, 'regime_index': i,
                           'start_seconds': wanted, 'kind': 'ROUTE_AWARE_EARLIEST_HINT'})
    return provenance


def selected_modes(state, artifact, solver):
    modes, rows, materialization = {}, [], []
    origin = state['problem'].origin
    for key in sorted(state['keys']):
        ri = next(i for i, lit in enumerate(artifact['reg_lit'][key]) if solver.Value(lit))
        regime = artifact['reg_info'][(key, ri)]
        config = regime['config']
        start = solver.Value(artifact['starts'][key])
        cp_end = solver.Value(artifact['ends'][key])
        authoritative_end, seconds_segments = _execution(state, config, start)
        f = regime['formula']
        cp_segments = tuple((int(f[1+2*i][0])*start + int(f[1+2*i][1]),
                             int(f[2+2*i][0])*start + int(f[2+2*i][1]))
                            for i in range(int(regime['segments'])))
        mismatch = []
        if authoritative_end != cp_end: mismatch.append('END')
        if tuple(seconds_segments) != cp_segments: mismatch.append('WORKER_SEGMENTS')
        materialization.append({'operation_key': key, 'mismatches': mismatch,
                                'cp_end': cp_end, 'authoritative_end': authoritative_end,
                                'cp_segments': cp_segments,
                                'authoritative_segments': seconds_segments})
        dt_segments = tuple((origin + timedelta(seconds=a), origin + timedelta(seconds=b))
                            for a, b in cp_segments)
        productive_start = start + int(config.setup_seconds)
        mode = JointExecutableMode(
            f'compressed_{key}_{ri}', key, int(config.factory_id), int(config.line_id),
            int(config.k), tuple(config.machine_ids), tuple(config.worker_ids), start,
            productive_start, int(config.setup_seconds), int(config.productive_seconds),
            float(config.productive_minutes or (config.productive_seconds / 60.0)), cp_end, cp_end-start,
            max(0, cp_end-productive_start-int(config.productive_seconds)), dt_segments,
            (), (), tuple(config.provenance), False, start, cp_end,
            'P2_COMPRESSED_REPAIR', 'CP_SELECTED_EXACT_REGIME', state['event'].event_id)
        modes[key] = mode
        rows.append({'operation_key': key, 'regime_index': ri, 'start_seconds': start,
                     'end_seconds': cp_end, 'resource_configuration': asdict(config),
                     'worker_segments': seconds_segments})
    return modes, rows, materialization


def acceptance(state, modes, rows, materialization):
    mismatch_count = sum(bool(x['mismatches']) for x in materialization)
    replay_rows, replay_mismatches = [], []
    neighborhood = SimpleNamespace(operation_keys=tuple(sorted(state['keys'])))
    if mismatch_count == 0:
        for key, mode in modes.items():
            end, segments, mismatches = _replay_mode(
                state['problem'], state['operator_index'], state['after_index'], neighborhood, mode)
            replay_rows.append({'operation_key': key, 'selected_end_seconds': mode.end_seconds,
                                'replayed_end_seconds': end, 'exact': not mismatches,
                                'mismatches': mismatches})
            replay_mismatches.extend({'operation_key': key, **m} for m in mismatches)
    replay = {'status': 'PASS' if mismatch_count == 0 and not replay_mismatches else 'FAIL',
              'mismatch_count': len(replay_mismatches), 'rows': replay_rows,
              'mismatches': replay_mismatches,
              'path': 'crafter.lns_probe.run_e1_7_final_verified._replay_mode'}
    validator = {'status': 'NOT_REACHED'}
    if replay['status'] == 'PASS':
        phenotype = _phenotype(state['problem'], state['baseline'], modes)
        checked = validate(state['problem'], phenotype,
                           state.get('validation_operator_index', state['operator_index']))
        validator = {'status': 'VALID' if checked.status.value == 'VALIDATED_FEASIBLE' else checked.status.value,
                     'phenotype_status': checked.status.value,
                     'validation_report': checked.validation_report}
    return replay, validator


def main():
    started = time.time()
    result = run_p2_repair(max_machine_sets=3, max_worker_crews=3,
                           max_closure_iterations=30, max_local_operations=150,
                           progress=lambda row: print(json.dumps(row), flush=True))
    state, configs, regimes = result['state'], result['resource_configurations'], result['regimes']
    equivalence = result['calendar_equivalence']
    write('calendar_regime_equivalence.json', equivalence)
    if equivalence['mismatches']:
        raise RuntimeError('calendar regime equivalence gate failed')
    summary = result['regime_summary']
    write('calendar_regime_compression.json', {
        'before_regimes': sum(int(x.get('raw_regime_count', x['regime_count'])) for x in summary),
        'after_regimes': sum(int(x['regime_count']) for x in summary),
        'coalesced': sum(int(x.get('coalesced_regimes', 0)) for x in summary),
        'per_operation': summary,
        'equivalence_mismatches': equivalence['mismatches']})

    anchors, anchor_provenance = {}, {}
    for key in sorted(state['keys']):
        anchors[key], anchor_provenance[key] = incumbent_choice(state, key, configs[key])
    write('resource_anchor_provenance.json', anchor_provenance)

    # Increasing exact subsets.  A time window restricts search but never
    # changes an admitted regime's semantics; infeasibility of a subset is not
    # reported as infeasibility of the complete legal domain.
    plan = [(1, None, 30), (2, 7, 180), (3, 7, 240), (3, 14, 300), (999, 14, 300)]
    stage_results = []; solution = None
    for stage, (limit, window_days, seconds) in enumerate(plan, 1):
        chosen = {key: diverse(configs[key], anchors[key], min(limit, len(configs[key])))
                  for key in sorted(state['keys'])}
        stage_regimes, bounds = restrict_regimes(state, regimes, chosen, window_days)
        if any(not rows for rows in stage_regimes.values()):
            stage_results.append({'stage': stage, 'status': 'ZERO_DOMAIN_AFTER_WINDOW',
                                  'config_limit': limit, 'window_days': window_days})
            continue
        build_started = time.time()
        artifact = build_repair_model(state, stage_regimes)
        validation_error = artifact['model'].Validate()
        if validation_error:
            raise RuntimeError(f'CP model validation failed: {validation_error}')
        hints = add_hints(state, stage_regimes, artifact, bounds)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = seconds
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = 42
        solver.parameters.use_optimization_hints = True
        solver.parameters.cp_model_presolve = True
        code = solver.Solve(artifact['model'])
        row = {'stage': stage, 'config_limit': limit, 'window_days': window_days,
               'resource_configurations': sum(len(v) for v in chosen.values()),
               'regimes': sum(len(v) for v in stage_regimes.values()),
               'hint_count': len(hints), 'model_validation': 'PASS',
               'build_and_solve_seconds': time.time()-build_started,
               'status': solver.StatusName(code), 'solver_wall_time': solver.WallTime(),
               'branches': solver.NumBranches(), 'conflicts': solver.NumConflicts(),
               **artifact['size']}
        stage_results.append(row); write('stage_results.json', stage_results)
        print(json.dumps({'phase': 'search_stage', **row}), flush=True)
        if code in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            solution = (artifact, solver, row)
            break

    selected, materialization = [], []
    replay, validator = {'status': 'NOT_REACHED'}, {'status': 'NOT_REACHED'}
    if solution:
        artifact, solver, winning = solution
        modes, selected, materialization = selected_modes(state, artifact, solver)
        replay, validator = acceptance(state, modes, selected, materialization)
    materialization_count = sum(bool(x.get('mismatches')) for x in materialization)
    write('selected_schedule.json', {'status': 'SELECTED' if selected else 'NOT_REACHED', 'operations': selected})
    write('materialization_equivalence.json', {'status': 'PASS' if selected and materialization_count == 0 else 'NOT_REACHED' if not selected else 'FAIL',
                                               'mismatch_count': materialization_count, 'operations': materialization})
    write('replay_result.json', replay); write('validator_result.json', validator)
    solved = bool(solution) and materialization_count == 0 and replay['status'] == 'PASS' and validator['status'] == 'VALID'
    status = 'CRAFT_P2_REPAIR_SOLVED' if solved else 'CRAFT_P2_SEARCH_LIMIT'
    write('cp_result.json', {'status': solution[2]['status'] if solution else stage_results[-1]['status'],
                            'stages': stage_results})
    write('root_cause_classification.json', {
        'status': status,
        'assessment': 'CALENDAR_REGIME_OPTIONAL_INTERVAL_EXPANSION_WAS_DOMINANT_SEARCH_MULTIPLIER',
        'reductions': ['CONTIGUOUS_IDENTICAL_AFFINE_REGIME_COALESCING',
                       'DIRECT_AFFINE_OPTIONAL_INTERVAL_EXPRESSIONS',
                       'ROUTE_AWARE_EXACT_TEMPORAL_SUBSET_SEARCH',
                       'DIVERSE_LEGAL_RESOURCE_SUBSET_STAGING'],
        'physical_constraints_relaxed': False, 'database_writes': 0})
    write('compression_validation.json', {
        'calendar_equivalence_mismatches': equivalence['mismatches'],
        'cp_models_validated': all(x.get('model_validation') == 'PASS' for x in stage_results if 'model_validation' in x),
        'focused_tests': 'tests/test_calendar_regime_coalescing.py; tests/test_repair_routing_dispatch.py; tests/test_repair_resource_config_independence.py',
        'old_full_reference': {'regimes': 16253, 'variables': 195739, 'constraints': 461384, 'optional_intervals': 221243},
        'elapsed_seconds': time.time()-started})
    (OUT/'final_report.md').write_text(
        f'# P2 compressed authoritative search\n\nPrimary status: `{status}`.\n\n'
        f'Corrected causal region: {len(state["keys"])} operations. Calendar equivalence mismatches: '
        f'{equivalence["mismatches"]}. Search stages: {json.dumps(stage_results)}. '
        f'Materialization mismatches: {materialization_count}; independent replay: `{replay["status"]}`; '
        f'validator: `{validator["status"]}`; database writes: 0.\n', encoding='utf-8')
    files = {str(p.relative_to(OUT)).replace('\\','/'): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(OUT.rglob('*')) if p.is_file() and p.name != 'artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json', {'files': files, 'artifact_count': len(files), 'status': status, 'database_writes': 0})
    print(json.dumps({'status': status, 'stages': stage_results,
                      'materialization_mismatches': materialization_count,
                      'replay': replay['status'], 'validator': validator['status']}, indent=2), flush=True)


if __name__ == '__main__':
    main()
