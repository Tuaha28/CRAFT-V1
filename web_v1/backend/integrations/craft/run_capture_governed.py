"""Capture-only worker; real CRAFT solver, no database access, no approvals."""
import json
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


def prepare(factory_id, target):
    source, readiness, normalized, source_hash = runner._source(factory_id)
    routes = runner._select_routes(runner._eligible_order_routes(source, normalized), str(target))
    state = runner._prepare(factory_id, source, readiness, normalized, source_hash, routes)
    keys = list(state['keys'])
    ledger, executions, orders, dispositions, reasons = {}, {}, {}, {}, {}
    for op in normalized['operations']:
        key, q = op['operation_key'], op['quantity']
        ledger[key] = dict(required=int(q['target']), completed=int(q['completed']),
                           in_flight=int(q['in_flight']), remaining=int(q['not_started']))
        executions[key], orders[key] = op['execution_state'], str(op['order_id'])
        dispositions[key] = ('SELECTED' if key in keys else 'COMPLETED'
                             if op['execution_state'] == 'FULLY_COMPLETED' else 'BLOCKED')
        if dispositions[key] == 'BLOCKED':
            reasons[key] = 'OUTSIDE_PREPARED_TEST_DOMAIN_OR_EXECUTION_EVIDENCE_MISSING'
    machines = {str(m['asset_id']): int(m['line_id']) for m in source['resources']['machines']}
    horizon = state['problem'].horizon_seconds
    holds = [{'factory_id': factory_id, 'operation_key': h['operation_key'],
              'machine_id': h['machine_id'], 'line_id': machines[str(h['machine_id'])],
              'worker_segments': [[str(h['worker_id']), 0, horizon]],
              'start_seconds': 0, 'end_seconds': horizon,
              'provenance': 'TEST_QUARANTINE_UNKNOWN_RELEASE:' + str(h['lot_id'])}
             for h in state['test_partial_holds']]
    contracts = state['operation_execution_contracts']
    releases = {o.order_id: o.release_seconds for o in state['problem'].orders}
    packet = seal({
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
                        for k, c in contracts.items()},
        'calendar_windows': {k: [[0, horizon]] for k in keys},
        'route_releases': {k: releases[orders[k]] for k in keys},
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
    # Rebuild the complete preflight domain independently from the pinned source.
    base = prepare(factory_id, len(global_state['execution_contracts']))
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
        output = runner._run(factory_id, 'all', selected_keys=snapshot['selected_keys'],
            approved_rows=global_state.get('approved_rows', []),
            approval_fingerprint=global_state['fingerprint'], checkpoint=save,
            resume=resume, max_new_attempts=1, route_ready_objective=True)
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
        result = prepare(int(sys.argv[2]), int(sys.argv[3]))
        destination = Path(sys.argv[4])
    else:
        destination = Path(sys.argv[1])
        snapshot = json.loads(Path(sys.argv[2]).read_text())
        checkpoint = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else None
        result = advance(snapshot, checkpoint)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, sort_keys=True, default=str)
