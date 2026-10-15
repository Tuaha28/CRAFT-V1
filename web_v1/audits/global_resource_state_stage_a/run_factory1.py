"""Pinned Factory-1 simulation-only Stage-A replay; never opens legacy DB.

The global snapshot binds all 35 hypothetical fixed holds. There is no
application-approved Factory-1 future schedule in this capture. The broad
calendar envelope below is ONLY a global-conflict check: exact calendars,
quantities and routes remain independently checked by validate_bounded.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError('LEGACY_DB_FORBIDDEN'))

from utils.machine_taxonomy import machine_type_to_operation_type
from web_v1.backend.integrations.craft.global_resource_state import seal, validate_candidate
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SNAPSHOT, SCENARIO, SOURCE_SHA
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import next_earliest_order_keys, run
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload

HERE = Path(__file__).resolve().parent
PRIOR = ROOT / 'web_v1/audits/factory1_realistic_neighborhood_v1/bounded_25_expanded_v1.json'


def build_snapshot(source, normalized, scenario, keys):
    origin = datetime.fromisoformat(source['reference_time'])
    seconds = lambda value: int((datetime.fromisoformat(value) - origin).total_seconds())
    fixed = []
    for item in scenario['partial_operations']:
        for hold in item['fixed_in_flight_holds']:
            fixed.append({'factory_id': 1, 'operation_key': item['operation_key'],
                          'lot_id': hold['lot_id'], 'machine_id': hold['machine_id'],
                          'line_id': hold['line_id'],
                          'start_seconds': seconds(hold['machine_hold_from']),
                          'end_seconds': seconds(hold['machine_hold_until']),
                          'worker_segments': [[str(hold['worker_id']), seconds(a), seconds(b)]
                                              for a, b in hold['worker_productive_segments']],
                          'provenance': 'SIMULATED_FACTORY1_INFLIGHT_NOT_OBSERVED'})
    ops = {row['operation_key']: row for row in normalized['operations']}
    routes = {f"{row['production_order_id']}_{row['operation_id']}": row
              for row in source['operations']}
    machines = {row['asset_id'] for row in source['resources']['machines']
                if row['current_status'] == 'Active' and row['factory_id'] == 1}
    workers = {row['employee_id'] for row in source['resources']['workers']
               if row['is_active'] and row['factory_id'] == 1}
    leave = {row['employee_id'] for row in source['resources']['work_schedules'] if row['is_on_leave']}
    eligible = {}
    for key in keys:
        capability = machine_type_to_operation_type(routes[key]['required_machine_type']).lower()
        eligible[key] = {
            'machines': sorted({row['asset_id'] for row in source['resources']['machine_capabilities']
                                if row['asset_id'] in machines and row['operation_type'].lower() == capability}),
            'workers': sorted({str(row['employee_id']) for row in source['resources']['skills']
                               if row['employee_id'] in workers and row['employee_id'] not in leave
                               and row['operation_type'].lower() == capability}),
            'lines': sorted({row['line_id'] for row in source['resources']['lines']
                             if row['factory_id'] == 1 and row['is_active']})}
    packet = {'contract': 'GLOBAL_RESOURCE_STATE_V1', 'classification': 'SIMULATED_TEST',
              'factory_id': 1, 'source_hash': SOURCE_SHA,
              'source_identity': normalized['snapshot_hash'],
              'scenario_hash': scenario['scenario_hash'],
              'reference_time': source['reference_time'], 'timezone': 'UNSPECIFIED_FACTORY_LOCAL',
              'resource_revision': 0, 'calendar_revision': normalized['snapshot_hash'],
              'eligibility_revision': SOURCE_SHA, 'active_version_id': None,
              'machine_lines': {str(row['asset_id']): row['line_id']
                                for row in source['resources']['machines'] if row['asset_id'] in machines},
              'worker_ids': sorted(str(worker) for worker in workers),
              'selected_keys': sorted(keys), 'approved_outside': [],
              'fixed_commitments': fixed, 'eligibility': eligible,
              'calendar_windows': {key: [[0, 30 * 86400]] for key in keys},
              'route_releases': {key: 0 for key in keys},
              'route_edges': {key: {'predecessor': ops[key]['predecessor'],
                                    'successor': ops[key]['successor']} for key in keys},
              'completed_keys': [key for key, row in ops.items()
                                 if row['execution_state'] == 'FULLY_COMPLETED'],
              'operation_ledger': {key: {
                  'required': row['quantity']['target'],
                  'completed': row['quantity']['completed'],
                  'in_flight': row['quantity']['in_flight'],
                  'remaining': row['quantity']['not_started']} for key, row in ops.items()},
              'execution_states': {key: row['execution_state'] for key, row in ops.items()},
              'order_identities': {key: row['order_id'] for key, row in ops.items()},
              'schedule_disposition': {
                  key: ('SELECTED' if key in keys else 'COMPLETED'
                        if row['execution_state'] == 'FULLY_COMPLETED' else 'DEFERRED')
                  for key, row in ops.items()},
              'disposition_reasons': {
                  key: 'OUTSIDE_BOUNDED_SIMULATION_WINDOW_NO_FIXED_FUTURE_ASSIGNMENT'
                  for key, row in ops.items() if key not in keys and
                  row['execution_state'] != 'FULLY_COMPLETED'},
              'production_admission_blocked_keys': sorted(
                  key for key, row in ops.items() if row['execution_state'] == 'PARTIALLY_EXECUTED'),
              'priority_revision': 0}
    return seal(packet)


def main():
    started = time.perf_counter()
    source_bytes = SOURCE.read_bytes()
    if sha256(source_bytes).hexdigest() != SOURCE_SHA:
        raise RuntimeError('PINNED_SOURCE_CHANGED')
    source = json.loads(source_bytes)
    normalized = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    scenario = json.loads(SCENARIO.read_text(encoding='utf-8'))
    keys = next_earliest_order_keys()
    t = time.perf_counter()
    global_state = build_snapshot(source, normalized, scenario, keys)
    snapshot_seconds = time.perf_counter() - t
    old = json.loads(PRIOR.read_text(encoding='utf-8'))
    t = time.perf_counter()
    before = validate_candidate(global_state, [row for window in old['windows']
                                                for row in window['selected_schedule']])
    prior_conflict_seconds = time.perf_counter() - t
    if '--saved-only' in sys.argv[1:]:
        print(json.dumps({'status': before['status'],
                          'global_operations_accounted': len(global_state['operation_ledger']),
                          'production_partials_blocked': len(global_state['production_admission_blocked_keys']),
                          'fixed_holds': len(global_state['fixed_commitments'])}))
        return
    output = run(keys, domain_width=4, global_resource_state=global_state)
    t = time.perf_counter()
    independent = validate_payload(source, normalized, scenario, output, source_bytes)
    independent_seconds = time.perf_counter() - t
    t = time.perf_counter()
    global_report = validate_candidate(global_state, [row for window in output['windows']
                                                       for row in window['selected_schedule']])
    global_conflict_seconds = time.perf_counter() - t
    summary = {'classification': 'SIMULATION_ONLY_NOT_PRODUCTION',
               'source_sha256': SOURCE_SHA, 'scenario_hash': scenario['scenario_hash'],
               'global_state_fingerprint': global_state['fingerprint'],
               'actual_application_approved_outside_assignments': 0,
               'simulated_fixed_holds': len(global_state['fixed_commitments']),
               'selected_operations': len(keys), 'scheduled_operations': len(output['accepted_keys']),
               'accepted_windows': sum(w['status'] == 'ACCEPTED' for w in output['windows']),
               'prior_saved_global_conflict_check': before,
               'new_global_conflict_check': global_report,
               'independent_assembled': independent,
               'total_wall_seconds': time.perf_counter() - started,
               'bounded_run_seconds': output['total_wall_seconds'],
               'snapshot_build_seconds': snapshot_seconds,
               'prior_saved_global_conflict_seconds': prior_conflict_seconds,
               'global_conflict_seconds': global_conflict_seconds,
               'independent_assembled_seconds': independent_seconds,
               'resource_construction_seconds': output['construction_seconds'],
               'calendar_regime_seconds': sum(w['calendar_seconds'] for w in output['windows']),
               'solve_and_validation_seconds': sum(w['solve_and_validation_seconds']
                                                   for w in output['windows']),
               'cp_solve_seconds_reported': sum(stage.get('solve_seconds') or 0
                                                for w in output['windows'] for stage in w['stages']),
               'sampled_peak_rss_mb': output['sampled_peak_rss_mb'],
               'windows': [{k: w[k] for k in ('number', 'status', 'calendar_seconds',
                                              'solve_and_validation_seconds', 'replay',
                                              'physical', 'scope', 'calendar_regimes')}
                           for w in output['windows']]}
    HERE.mkdir(exist_ok=True)
    (HERE / 'factory1_global_state_snapshot.json').write_text(json.dumps(global_state, indent=2, sort_keys=True) + '\n')
    (HERE / 'factory1_bounded_25_stage_a.json').write_text(json.dumps(output, indent=2, sort_keys=True, default=str) + '\n')
    (HERE / 'factory1_stage_a_result.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: summary[k] for k in ('selected_operations', 'scheduled_operations',
                                             'accepted_windows', 'bounded_run_seconds',
                                             'sampled_peak_rss_mb')}))
    if len(output['accepted_keys']) != len(keys) or independent['status'] != 'PASS' or global_report['status'] != 'PASS':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
