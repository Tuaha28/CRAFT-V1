import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, '.')

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import (GlobalCRAFTPlanner, RollingWindow,
                                            factory_future_operation_keys)
from crafter.repair.planning import (PlanningContextV1, build_planning_state,
                                     prepare_planning_schedule)
from crafter.repair.scoped_validation import (validate_planning_window,
                                               validate_two_window_assignments)
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype

OUT = Path('outputs/crafter/global_craft_v1')
OUT.mkdir(parents=True, exist_ok=True)
started = time.perf_counter()
checkpoints = []
current_stage = 'startup'


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def mark(stage, **fields):
    global current_stage
    current_stage = stage
    row = {'stage': stage, 'seconds': time.perf_counter() - started, **fields}
    checkpoints.append(row)
    write_json('window1_checkpoints.json', checkpoints)
    print(json.dumps(row), flush=True)


def selected_rows(prepared):
    acceptance = prepared.get('acceptance', {})
    rows = list(acceptance.get('selected_schedule', ()))
    state = prepared['state']
    for row in rows:
        op = state['problem'].operation_by_key[str(row['operation_key'])]
        row['order_id'] = str(op.order_id)
        row['setup_seconds'] = int(row['resource_configuration']['setup_seconds'])
        row['productive_seconds'] = int(row['resource_configuration']['productive_seconds'])
        row['productive_minutes'] = float(row['resource_configuration'].get('productive_minutes', 0.0))
    return rows


def boundary_facts(boundary):
    facts = [dict(row) for row in boundary.machine_commitments]
    facts.extend(dict(row) for row in boundary.worker_commitments)
    for key, route in boundary.routing_completion_state.items():
        facts.append({'operation_key': key, **dict(route),
                      'authoritative_end_seconds': route['authoritative_end_seconds']})
    return tuple(facts)


def resource_overlap(rows):
    machine_intervals = {}
    worker_intervals = {}
    for row in rows:
        cfg = row['resource_configuration']
        start, end = int(row['start_seconds']), int(row['end_seconds'])
        for machine in cfg['machine_ids']:
            machine_intervals.setdefault(int(machine), []).append((start, end, row['operation_key']))
        for worker in cfg['worker_ids']:
            for left, right in row['worker_segments']:
                worker_intervals.setdefault(str(worker), []).append((int(left), int(right), row['operation_key']))
    def count(intervals):
        total = 0
        for values in intervals.values():
            values.sort()
            for left, right in zip(values, values[1:]):
                if int(left[1]) > int(right[0]) and left[2] != right[2]:
                    total += 1
        return total
    return count(machine_intervals), count(worker_intervals)


try:
    current_stage = 'state_load'
    loaded = _load_state()
    problem, operator_index, rows, _durations, _op_details, idx, *_ = loaded
    mark('state_load', operations=len(problem.operations), rows=len(rows),
         loaded_tuple_fields=len(loaded), route_successor_type=type(idx.route_successor).__name__,
         route_successor_entries=len(idx.route_successor))

    factory_counts = {}
    for op in problem.operations:
        factory_id = int(problem.order_by_id[op.order_id].factory_id)
        factory_counts[factory_id] = factory_counts.get(factory_id, 0) + 1
    factory = min(factory_counts, key=lambda value: (factory_counts[value], value))
    factory_keys = {str(op.operation_key) for op in problem.operations
                    if int(problem.order_by_id[op.order_id].factory_id) == factory}
    successor_map = idx.route_successor
    chain = []
    for key in sorted(factory_keys):
        successor = successor_map.get(key) if isinstance(successor_map, dict) else getattr(successor_map, key, None)
        if successor is not None and str(successor) in factory_keys:
            chain = [key, str(successor)]
            break
    if len(chain) < 2:
        raise RuntimeError('no genuine same-factory route chain')
    mark('fixture_selected', factory=factory, operation_count=factory_counts[factory],
         chain=chain, factory_counts=factory_counts)

    def prepare(window, selected, boundary=None, provisional_reference=None):
        global current_stage
        mode = 'INITIAL_PLAN' if boundary is None else 'ROLLING_PLAN'
        context = PlanningContextV1(
            window.window_id, mode, factory, window.start_seconds,
            window.start_seconds, window.end_seconds, window.commit_end_seconds,
            tuple(selected), 'FROZEN_BENCHMARK_RAW',
            previous_provisional_plan_reference=provisional_reference,
            snapshot_as_of_seconds=0)
        current_stage = f'{window.window_id}:build_planning_state'
        state = build_planning_state(loaded, context, boundary=boundary)
        mark('build_planning_state', window=window.window_id, mode=mode,
             selected=list(selected), boundary_operations=sorted(boundary.committed_operation_keys) if boundary else [],
             worker_reservation_count=sum(len(value) for value in state['_worker_res'].values()))
        current_stage = f'{window.window_id}:prepare_planning_schedule'
        prepared = prepare_planning_schedule(state, max_machine_sets=1, max_worker_crews=1)
        prepared['required_window_keys'] = tuple(selected)
        prepared['boundary_facts'] = boundary_facts(boundary) if boundary else ()
        mark('prepare_planning_schedule', window=window.window_id,
             regimes=prepared['regime_count'], boundary_facts=len(prepared['boundary_facts']),
             snapshot_as_of_seconds=prepared['state']['snapshot_as_of_seconds'],
             window_start_seconds=window.start_seconds)
        return prepared

    def solve_and_validate(window, selected, boundary=None, provisional_reference=None):
        prepared = prepare(window, selected, boundary, provisional_reference)
        current_stage = f'{window.window_id}:canonical_solve'
        prepared = run_canonical_schedule(prepared, objective_mode=False)
        acceptance = prepared.get('acceptance', {})
        mark('canonical_solve', window=window.window_id,
             status=acceptance.get('status'), winning_stage=acceptance.get('winning_stage'))
        mark('materialization', window=window.window_id,
             seconds_from_kernel=acceptance.get('timing', {}).get('materialization_seconds', 0.0),
             mismatch_count=acceptance.get('materialization', {}).get('mismatch_count', 0))
        rows_out = selected_rows(prepared)
        future = factory_future_operation_keys(prepared['state'], factory)
        # Required keys come from the pre-solve admission contract, never
        # from the solver's returned rows.
        scope = ValidationScopeV1('WINDOW', tuple(prepared['required_window_keys']), future)
        phenotype, _ = build_window_phenotype(
            rows_out, boundary_facts=prepared.get('boundary_facts', ()),
            future_outside_operation_keys=future, validation_scope=scope)
        current_stage = f'{window.window_id}:scoped_validation'
        window_acceptance = validate_planning_window(prepared['state'], phenotype)
        mark('scoped_replay', window=window.window_id,
             status=window_acceptance.get('replay_status'))
        mark('physical_validation', window=window.window_id,
             status=window_acceptance.get('physical_status'))
        return prepared, acceptance, rows_out, phenotype, window_acceptance

    # Window 1 is deliberately committed at 20,000 seconds so its first
    # operation is hard and its second operation remains a soft overlap for
    # the rolling re-optimization proof.
    window1 = RollingWindow(factory, 0, 86400, 20000, 'window-1')
    prepared1, acceptance1, rows1, phenotype1, wa1 = solve_and_validate(window1, tuple(chain))
    w1_status = 'WINDOW_1_ACCEPTED' if wa1.get('accepted') else 'WINDOW_1_REJECTED'
    committed_result = {'acceptance': {'status': 'ACCEPTED', 'selected_schedule': rows1},
                       'state': prepared1['state']}
    boundary = None
    if wa1.get('accepted'):
        boundary = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
            committed_result, factory_id=factory,
            commit_end_seconds=window1.commit_end_seconds, source_repair_id=window1.window_id)
    committed_keys = list(boundary.committed_operation_keys) if boundary else []
    provisional_keys = sorted(set(chain) - set(committed_keys))
    window1_payload = {
        'status': w1_status,
        'scope_status': wa1.get('scope_status'), 'replay_status': wa1.get('replay_status'),
        'physical_status': wa1.get('physical_status'), 'accepted': bool(wa1.get('accepted')),
        'factory_complete_claimed': False, 'factory_id': factory,
        'factory_operation_count': factory_counts[factory], 'chain': chain,
        'window_id': window1.window_id, 'commit_end_seconds': window1.commit_end_seconds,
        'snapshot_as_of_seconds': int(prepared1['state']['snapshot_as_of_seconds']),
        'window_start_seconds': window1.start_seconds,
        'canonical_acceptance_status': acceptance1.get('status'),
        'acceptance': acceptance1, 'window_acceptance': wa1,
        'scope_required_operation_keys': list(phenotype1.validation_scope.required_operation_keys),
        'selected_assignment_keys': list(phenotype1.assignment_keys),
        'future_outside_operation_keys': list(phenotype1.future_outside_operation_keys),
        'committed_operation_keys': committed_keys, 'provisional_operation_keys': provisional_keys,
        'historical_future_assignments_backfilled': 0, 'database_writes': 0,
        'total_seconds': time.perf_counter() - started, 'checkpoints': checkpoints,
    }
    write_json('window1_result.json', window1_payload)
    if not wa1.get('accepted'):
        raise RuntimeError(f'Window 1 rejected: {wa1}')

    # Window 2 re-optimizes the provisional second operation while carrying
    # the exact first operation as a hard routing/resource boundary.
    window2 = RollingWindow(factory, 20000, 86400, 60000, 'window-2')
    prepared2, acceptance2, rows2, phenotype2, wa2 = solve_and_validate(
        window2, (chain[1],), boundary=boundary,
        provisional_reference=window1.window_id)
    w2_status = 'WINDOW_2_ACCEPTED' if wa2.get('accepted') else 'WINDOW_2_REJECTED'
    window2_payload = {
        'status': w2_status,
        'scope_status': wa2.get('scope_status'), 'replay_status': wa2.get('replay_status'),
        'physical_status': wa2.get('physical_status'), 'accepted': bool(wa2.get('accepted')),
        'factory_complete_claimed': False, 'factory_id': factory,
        'factory_operation_count': factory_counts[factory], 'chain': chain,
        'window_id': window2.window_id, 'commit_end_seconds': window2.commit_end_seconds,
        'snapshot_as_of_seconds': int(prepared2['state']['snapshot_as_of_seconds']),
        'window_start_seconds': window2.start_seconds,
        'canonical_acceptance_status': acceptance2.get('status'),
        'acceptance': acceptance2, 'window_acceptance': wa2,
        'scope_required_operation_keys': list(phenotype2.validation_scope.required_operation_keys),
        'selected_assignment_keys': list(phenotype2.assignment_keys),
        'future_outside_operation_keys': list(phenotype2.future_outside_operation_keys),
        'boundary_operation_keys': list(boundary.committed_operation_keys),
        'boundary_facts': list(boundary_facts(boundary)),
        'historical_future_assignments_backfilled': 0, 'database_writes': 0,
        'total_seconds': time.perf_counter() - started, 'checkpoints': checkpoints,
    }
    # Persist Window 2 before any continuity assembly.
    write_json('window2_result.json', window2_payload)
    if not wa2.get('accepted'):
        raise RuntimeError(f'Window 2 rejected: {wa2}')

    committed_rows = [row for row in rows1 if str(row['operation_key']) in set(committed_keys)]
    continuity_rows = committed_rows + rows2
    continuity_validation = validate_two_window_assignments(
        prepared2['state'], continuity_rows,
        # The continuity universe is declared by the accepted commitment set
        # plus Window-2's pre-solve admission contract, not by re-reading the
        # rows returned for validation.
        required_operation_keys=tuple(sorted(set(committed_keys)
                                             | set(phenotype2.validation_scope.required_operation_keys))))
    counts = continuity_validation.get('violation_counts', {})
    machine_overlaps = int(counts.get('machine_overlaps', 0))
    worker_overlaps = int(counts.get('worker_overlaps', 0))
    predecessor_key = chain[0]
    successor_key = chain[1]
    predecessor_row = next(row for row in committed_rows if str(row['operation_key']) == predecessor_key)
    successor_row = next(row for row in rows2 if str(row['operation_key']) == successor_key)
    slack = int(successor_row['start_seconds']) - int(predecessor_row['end_seconds'])
    boundary_errors = continuity_validation.get('boundary_errors', ())
    routing_boundary_violations = int(counts.get('routing_boundary_violations', 0))
    changeover_violations = int(counts.get('changeover_violations', 0))
    duplicates = len([key for key in [str(row['operation_key']) for row in continuity_rows]
                     if [str(x['operation_key']) for x in continuity_rows].count(key) > 1])
    # Derive commitment integrity from the carried boundary payload instead
    # of assuming success.  This compares every immutable field that Window 2
    # must receive against the accepted Window-1 materialization.
    committed_changed = 0
    for row in committed_rows:
        key = str(row['operation_key']); cfg = row['resource_configuration']
        machine_facts = [x for x in boundary.machine_commitments if str(x['operation_key']) == key]
        worker_facts = [x for x in boundary.worker_commitments if str(x['operation_key']) == key]
        route_fact = boundary.routing_completion_state.get(key, {})
        if ({int(x['machine_id']) for x in machine_facts} != {int(x) for x in cfg['machine_ids']}
                or {str(x['worker_id']) for x in worker_facts} != {str(x) for x in cfg['worker_ids']}
                or any(int(x['start_seconds']) != int(row['start_seconds'])
                       or int(x['end_seconds']) != int(row['end_seconds'])
                       or int(x.get('line_id', cfg['line_id'])) != int(cfg['line_id'])
                       for x in machine_facts)
                or int(route_fact.get('authoritative_end_seconds', -1)) != int(row['end_seconds'])):
            committed_changed += 1
    provisional_hard_frozen = sum(
        1 for fact in boundary_facts(boundary)
        if str(fact.get('operation_key', '')) in set(provisional_keys))
    scope_report = continuity_validation.get('scope', {})
    missing_required = len(scope_report.get('missing_required_operations', ()))
    duplicates = len(scope_report.get('duplicate_operations', ()))
    precedence_count = int(counts.get('precedence_violations', 0))
    if slack < 0:
        precedence_count = max(precedence_count, 1)
    boundary_routing = sum(1 for error in wa2.get('physical', {}).get('boundary_errors', ())
                           if error.get('code') == 'routing_boundary_violation')
    boundary_changeover = sum(1 for error in wa2.get('physical', {}).get('boundary_errors', ())
                              if error.get('code') == 'changeover_boundary_violation')
    continuity = {
        'status': 'TWO_WINDOW_CONTINUITY_VALID' if continuity_validation.get('physical_status') == 'VALID' and slack >= 0 and duplicates == 0 and committed_changed == 0 and provisional_hard_frozen == 0 else 'TWO_WINDOW_CONTINUITY_INVALID',
        'validation_scope': 'TWO_WINDOW_CONTINUITY', 'factory_complete_claimed': False,
        'factory_id': factory, 'window1_committed_operations': committed_keys,
        'window2_selected_operations': [str(row['operation_key']) for row in rows2],
        'committed_operations_changed': committed_changed,
        'provisional_operations_checked': len(provisional_keys),
        'provisional_hard_frozen': provisional_hard_frozen,
        'predecessor_successor': {
            'predecessor_operation_key': predecessor_key,
            'predecessor_end_seconds': int(predecessor_row['end_seconds']),
            'successor_operation_key': successor_key,
            'successor_start_seconds': int(successor_row['start_seconds']),
            'slack_seconds': slack, 'violation': slack < 0,
        },
        'precedence_violations': precedence_count,
        'routing_boundary_violations': int(counts.get('routing_boundary_violations', 0)) + boundary_routing,
        'changeover_violations': int(counts.get('changeover_violations', 0)) + boundary_changeover,
        'machine_overlaps': machine_overlaps,
        'worker_overlaps': worker_overlaps,
        'factory_violations': int(counts.get('factory_violations', 0)),
        'eligibility_violations': sum(int(counts.get(k, 0)) for k in (
            'line_eligibility_violations', 'machine_eligibility_violations',
            'worker_eligibility_violations')),
        'line_eligibility_violations': int(counts.get('line_eligibility_violations', 0)),
        'machine_eligibility_violations': int(counts.get('machine_eligibility_violations', 0)),
        'worker_eligibility_violations': int(counts.get('worker_eligibility_violations', 0)),
        'calendar_violations': int(counts.get('calendar_violations', 0)),
        'reservation_violations': int(counts.get('reservation_violations', 0)),
        'release_violations': int(counts.get('release_violations', 0)),
        'commit_violations': committed_changed,
        'duplicates': duplicates, 'missing_required_operations': missing_required,
        'physical_validation': continuity_validation,
        'database_writes': 0, 'total_seconds': time.perf_counter() - started,
    }
    write_json('two_window_continuity_validation.json', continuity)
    print(json.dumps({'status': continuity['status'], 'factory': factory,
                      'window1': w1_status, 'window2': w2_status,
                      'committed': committed_keys, 'slack': slack,
                      'seconds': continuity['total_seconds']}), flush=True)
except Exception as exc:
    failure = {'status': 'TWO_WINDOW_FAILED', 'failed_stage': current_stage,
               'error_type': type(exc).__name__, 'error': str(exc),
               'traceback': traceback.format_exc(), 'database_writes': 0,
               'total_seconds': time.perf_counter() - started,
               'checkpoints': checkpoints}
    write_json('two_window_continuity_validation.json', failure)
    print(json.dumps({'status': 'TWO_WINDOW_FAILED', 'failed_stage': current_stage,
                      'error': str(exc)}), flush=True)
    raise
