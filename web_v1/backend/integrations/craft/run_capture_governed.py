"""Capture-only worker; real CRAFT solver, no database access, no approvals."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner
from web_v1.backend.integrations.craft.global_resource_state import seal, verify, validate_candidate, digest
from web_v1.backend.integrations.craft.capture_test_state import CONTRACT, SOURCE
from crafter.production.cycles import seal as checkpoint_seal
from crafter.production.checkpoints import CONTRACT as CHECKPOINT_CONTRACT


def _remaining_routes(eligible_routes, approved, excluded=frozenset()):
    """Route suffixes after an approved prefix; approval must be prefix-closed.

    A route stops at its first excluded operation: that operation and every
    successor stay out of later cycles (explicitly reported, never dropped).
    """
    remaining, prefixes = [], {}
    for release, order_id, rows, order in eligible_routes:
        done = [row for row in rows if row['operation_key'] in approved]
        if [row['operation_key'] for row in done] != [row['operation_key'] for row in rows[:len(done)]]:
            raise ValueError(f'CAPTURE_APPROVED_NOT_ROUTE_PREFIX:{order_id}')
        prefixes[order_id] = done
        suffix = []
        for row in rows[len(done):]:
            if row['operation_key'] in excluded:
                break
            suffix.append(row)
        if suffix:
            remaining.append((release, order_id, suffix, order))
    return remaining, prefixes


def prepare(factory_id, target, approved_keys=(), excluded_keys=()):
    """Prepare the next bounded domain.

    ``approved_keys`` rebases past approved cycles; ``excluded_keys`` are
    operations deferred by an earlier cycle (with their route successors).
    """
    source, readiness, normalized, source_hash = runner._source(factory_id)
    eligible_routes = runner._eligible_order_routes(source, normalized)
    approved, excluded = set(approved_keys), set(excluded_keys)
    known = {row['operation_key'] for _, _, rows, _ in eligible_routes for row in rows}
    if not approved <= known or not excluded <= known or approved & excluded:
        raise ValueError('CAPTURE_APPROVED_OR_EXCLUDED_OUTSIDE_UNSTARTED_TEST_ROUTES')
    remaining, prefixes = _remaining_routes(eligible_routes, approved, excluded)
    available = sum(len(route) for _release, _order, route, _order_row in remaining)
    requested = int(target)
    if requested < 1 or available < 1:
        raise ValueError(f'CAPTURE_NO_SOURCE_ADMISSIBLE_OPERATIONS:F{factory_id}')
    routes = runner._select_routes(remaining, str(min(requested, available)))
    # Approved predecessors stay in each route so route edges and contracts
    # are derived exactly as in the first cycle; they are not re-selected.
    routes = [(prefixes[str(order['production_order_id'])] + rows, order) for rows, order in routes]
    state = runner._prepare(factory_id, source, readiness, normalized, source_hash, routes)
    keys = [key for key in state['keys'] if key not in approved]
    ledger, executions, orders, dispositions, reasons = {}, {}, {}, {}, {}
    eligible_keys = {row['operation_key'] for _, _, route, _ in eligible_routes for row in route}
    for op in normalized['operations']:
        key, q = op['operation_key'], op['quantity']
        ledger[key] = dict(required=int(q['target']), completed=int(q['completed']),
                           in_flight=int(q['in_flight']), remaining=int(q['not_started']))
        executions[key], orders[key] = op['execution_state'], str(op['order_id'])
        if op['execution_state'] == 'FULLY_COMPLETED':
            dispositions[key] = 'COMPLETED'
        elif op['execution_state'] == 'PARTIALLY_EXECUTED':
            dispositions[key] = 'EXECUTING'
        elif key in approved:
            dispositions[key] = 'APPROVED'
        elif key in keys:
            dispositions[key] = 'SELECTED'
        elif key in eligible_keys:
            dispositions[key] = 'DEFERRED'
            reasons[key] = 'SOURCE_ADMISSIBLE_OUTSIDE_CURRENT_PREPARED_CYCLE'
        else:
            dispositions[key] = 'BLOCKED'
            reasons[key] = 'SOURCE_ROUTE_WIP_OR_EXECUTION_BOUNDARY_NOT_ADMITTED'
    machines = {str(m['asset_id']): int(m['line_id']) for m in source['resources']['machines']}
    horizon = state['problem'].horizon_seconds
    holds = [{'factory_id': factory_id, 'operation_key': h['operation_key'],
              'machine_id': h['machine_id'], 'line_id': machines[str(h['machine_id'])],
              'worker_segments': [[str(h['worker_id']), 0, horizon]],
              'start_seconds': 0, 'end_seconds': horizon,
              'provenance': 'TEST_QUARANTINE_UNKNOWN_RELEASE:' + str(h['lot_id'])}
             for h in state['test_partial_holds']]
    contracts = {key: state['operation_execution_contracts'][key] for key in keys}
    releases = {o.order_id: o.release_seconds for o in state['problem'].orders}
    # Approved rows re-enter as fixed commitments and are checked against the
    # same source eligibility and route releases, derived by the same rules.
    approved_contracts, approved_horizon = {}, horizon
    approved_routes = [(prefixes[order_id], order) for _, order_id, _, order in eligible_routes
                       if prefixes.get(order_id)]
    if approved_routes:
        prior = runner._prepare(factory_id, source, readiness, normalized, source_hash, approved_routes)
        approved_contracts = prior['operation_execution_contracts']
        approved_horizon = max(horizon, prior['problem'].horizon_seconds)
        releases.update({o.order_id: o.release_seconds for o in prior['problem'].orders})
    rebase = {'rebased_on_approved_keys': sorted(approved)} if approved else {}
    if excluded:
        rebase['excluded_deferred_keys'] = sorted(excluded)
    packet = seal({
        **rebase,
        'contract': 'GLOBAL_RESOURCE_STATE_V1', 'classification': 'SIMULATED_TEST',
        'factory_id': factory_id, 'source_hash': source_hash,
        'source_identity': source['dataset'], 'reference_time': source['reference_time'],
        'timezone': 'CAPTURE_NAIVE_FACTORY_LOCAL_TEST', 'resource_revision': 0,
        'calendar_revision': readiness['normalized_snapshot_hash'],
        'eligibility_revision': readiness['normalized_snapshot_hash'],
        'scenario_hash': state['source_resource_provenance']['scenario_hash'],
        'execution_contracts': contracts, 'test_assumptions': state['test_assumptions'],
        'machine_lines': machines,
        'worker_ids': [str(w['employee_id']) for w in source['resources']['workers']],
        'active_version_id': None, 'selected_keys': keys, 'approved_outside': [],
        'fixed_commitments': holds, 'operation_ledger': ledger,
        'execution_states': executions, 'order_identities': orders,
        'schedule_disposition': dispositions, 'disposition_reasons': reasons,
        'completed_keys': [k for k, s in dispositions.items() if s == 'COMPLETED'],
        'eligibility': {k: {'machines': c['eligible_machine_ids'],
                            'workers': c['eligible_worker_ids'], 'lines': c['eligible_line_ids']}
                        for k, c in {**approved_contracts, **contracts}.items()},
        'calendar_windows': {**{k: [[0, approved_horizon]] for k in approved_contracts},
                             **{k: [[0, horizon]] for k in keys}},
        'route_releases': {k: releases[orders[k]] for k in [*approved_contracts, *keys]},
        'route_edges': {k: {'predecessor': state['after_index'].route_predecessor[k],
                             'successor': state['after_index'].route_successor[k]} for k in keys},
        'priority_revision': 0})
    verify(packet)
    return packet


def advance(snapshot, checkpoint=None):
    if snapshot.get('contract') != CONTRACT or snapshot.get('source') != SOURCE:
        raise ValueError('CAPTURE_WORKER_CONTRACT')
    global_state = snapshot['global_resource_state']
    verify(global_state)
    factory_id = snapshot['factory_id']
    if factory_id != global_state['factory_id'] or snapshot['source_fingerprint'] != global_state['source_hash']:
        raise ValueError('CAPTURE_WORKER_SOURCE')
    # Rebuild the complete preflight domain independently from the pinned
    # source on a run's first invocation. Resumed invocations are bound to that
    # verified state by the sealed checkpoint identity (source, snapshot,
    # scenario, approval fingerprint, implementation hash), so repeating the
    # rebuild each window only added minutes per cycle.
    if checkpoint is None:
        base = prepare(factory_id, len(global_state['execution_contracts']),
                       approved_keys=global_state.get('rebased_on_approved_keys', ()),
                       excluded_keys=global_state.get('excluded_deferred_keys', ()))
        if (base['fingerprint'] != snapshot['capture_base_fingerprint'] or
                digest(base['execution_contracts']) != digest(global_state['execution_contracts'])):
            raise ValueError('CAPTURE_EXECUTION_CONTRACT_CHANGED')
    captured = []
    def save(identity, payload):
        captured.append(checkpoint_seal({'contract': CHECKPOINT_CONTRACT,
            'approval_status': 'TENTATIVE_NOT_APPROVED', 'identity': identity, 'payload': payload}))
    with tempfile.TemporaryDirectory(prefix='craft-capture-resume-') as directory:
        resume = None
        if checkpoint:
            resume = Path(directory) / 'checkpoint.json'
            resume.write_text(json.dumps(checkpoint), encoding='utf-8')
        budget = float(os.environ.get('CRAFT_ATTEMPT_BUDGET_SECONDS', '0') or 0)
        output = runner._run(factory_id, 'all', selected_keys=snapshot['selected_keys'],
            approved_rows=global_state.get('approved_rows', []),
            approval_fingerprint=global_state['fingerprint'], checkpoint=save,
            resume=resume, route_ready_objective=True,
            window_size=snapshot.get('window_size'),
            # Budget 0 keeps the original one-attempt-per-invocation contract.
            max_new_attempts=None if budget > 0 else 1,
            attempt_time_budget_seconds=budget if budget > 0 else None)
    if not captured:
        raise ValueError('CAPTURE_NO_CHECKPOINT_PROGRESS')
    cp = captured[-1]
    rows = output['selected_schedule']
    # Validate the accepted prefix against the same approved global state.
    prefix = {k: v for k, v in global_state.items() if k != 'fingerprint'}
    prefix['selected_keys'] = [r['operation_key'] for r in rows]
    prefix['schedule_disposition'] = dict(global_state['schedule_disposition'])
    prefix['disposition_reasons'] = dict(global_state['disposition_reasons'])
    for key in set(snapshot['selected_keys']) - set(prefix['selected_keys']):
        prefix['schedule_disposition'][key] = 'DEFERRED'
        prefix['disposition_reasons'][key] = 'PENDING_CAPTURE_CHECKPOINT'
    global_check = validate_candidate(seal(prefix), rows) if rows else None
    result = {'source': SOURCE, 'checkpoint': cp, 'capture_result': output,
              'runtime_seconds': output['total_wall_seconds'],
              'sampled_peak_rss_mb': output['sampled_peak_rss_mb'],
              'progress': {'accepted': len(rows), 'remaining': len(output['remaining_selected_keys']),
                           'deferred': output['deferred_operations']},
              'controller_status': 'CHECKPOINTED' if output['pending_windows'] else 'COMPUTATIONAL_LIMIT',
              'global_prefix': global_check, 'physical_infeasibility_proven': False}
    if output['controller_status'] == 'COMPLETE':
        global_check = validate_candidate(global_state, rows)
        windows = [{'status': 'ACCEPTED', 'replay': w['replay'], 'physical': w['physical'],
                    'scope': w['scope'], 'materialization_mismatch_count': 0}
                   for w in output['windows'] if w['accepted']]
        result.update(controller_status='ACCEPTED',
            acceptance={'status': 'ACCEPTED', 'selected_schedule': rows}, quantity={'status': 'PASS'},
            multi_window={'windows': windows, 'windows_accepted': len(windows),
                          'operations_selected': len(rows),
                          'independent_assembled': {'status': 'PASS' if output['assembled_schedule_validation']['accepted'] else 'FAIL'}},
            global_conflict_validation=global_check)
    return result


if __name__ == '__main__':
    if sys.argv[1] == '--prepare':
        rebase = json.loads(Path(sys.argv[5]).read_text()) if len(sys.argv) > 5 else {}
        result = prepare(int(sys.argv[2]), int(sys.argv[3]),
                         approved_keys=rebase.get('approved', ()),
                         excluded_keys=rebase.get('excluded', ()))
        destination = Path(sys.argv[4])
    else:
        destination = Path(sys.argv[1])
        snapshot = json.loads(Path(sys.argv[2]).read_text())
        checkpoint = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else None
        result = advance(snapshot, checkpoint)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, sort_keys=True, default=str)
