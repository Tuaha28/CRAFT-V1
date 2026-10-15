"""TEST-only worker proof: CRAFT consumes an application-exported active schedule."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('LEGACY_DB_FORBIDDEN'))

from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.authoritative_semantics import authoritative_changeover_seconds
from crafter.repair.solve import run_canonical_schedule
from web_v1.backend.integrations.craft.global_resource_state import verify, validate_candidate
from web_v1.backend.integrations.craft.priority_fixture import prepare, ORIGIN
from web_v1.backend.integrations.craft.residual_execution import validate_residual_output


def solve(snapshot):
    if snapshot.get('contract') != 'SYNTHETIC_GLOBAL_RESOURCE_V1':
        raise ValueError('GLOBAL_FIXTURE_CONTRACT')
    global_state = snapshot['global_resource_state']
    verify(global_state)
    if global_state['selected_keys'] != snapshot['selected_keys'] or global_state['factory_id'] != 1:
        raise ValueError('GLOBAL_FIXTURE_SELECTION')
    key, = global_state['selected_keys']
    if key not in ('A_residual', 'B_residual'):
        raise ValueError('GLOBAL_FIXTURE_OPERATION')
    prepared, packets, metadata = prepare()
    state = prepared['state']
    state['problem'] = replace(state['problem'], operations=tuple(
        op for op in state['problem'].operations if op.operation_key == key))
    state['keys'] = (key,)
    state['classified'] = {key: {'state': 'MOVABLE_WINDOW'}}
    state['_machine_res'] = {}
    state['_worker_res'] = {}
    state['boundary_facts'] = ()
    facts = []
    for item in global_state['fixed_commitments'] + global_state['approved_outside']:
        start, end = item['start_seconds'], item['end_seconds']
        machine = item['machine_id']
        order = ('MAINTENANCE' if item['operation_key'].startswith('event:')
                 else item['operation_key'].split('_')[0])
        state['_machine_res'].setdefault(machine, []).append((start, end, order, item['operation_key']))
        facts.append({'machine_id': machine, 'start_seconds': start, 'end_seconds': end,
                      'operation_key': item['operation_key']})
        for worker, left, right in item['worker_segments']:
            state['_worker_res'].setdefault(worker, []).append((ORIGIN + timedelta(seconds=left),
                                                                 ORIGIN + timedelta(seconds=right)))
            facts.append({'worker_id': worker, 'start_seconds': left, 'end_seconds': right,
                          'operation_key': item['operation_key']})
    state['boundary_facts'] = tuple(facts)
    configurations = {key: prepared['resource_configurations'][key]}
    probes = [item['end_seconds'] + authoritative_changeover_seconds(
        item['operation_key'].split('_')[0], key.split('_')[0])
              for item in global_state['approved_outside']]
    regimes, _, count, equivalence = generate_calendar_regimes(
        state, configurations, additional_breakpoints_by_operation={key: probes})
    if not regimes.get(key):
        raise RuntimeError(f'GLOBAL_FIXTURE_NO_REGIME:{state["_machine_res"]}:{state["_worker_res"]}')
    result = run_canonical_schedule({'state': state, 'resource_configurations': configurations,
                                     'regimes': regimes}, plan=((1, None, 5),), objective_mode=False)
    acceptance = result['acceptance']
    if acceptance['status'] != 'ACCEPTED':
        raise RuntimeError(f"GLOBAL_FIXTURE_SOLVE_NOT_ACCEPTED:{acceptance['status']}:"
                           f"{acceptance.get('winning_stage')}:{acceptance.get('stages')}")
    packet = next(p for p in packets if p['operation_key'] == key)
    quantity = validate_residual_output(packet, acceptance.get('selected_schedule') or [],
                                        decision_time=ORIGIN.isoformat())
    global_report = validate_candidate(global_state, acceptance.get('selected_schedule') or [])
    return {'source': 'SYNTHETIC_RESOURCE_TEST', 'controller_status': acceptance['status'],
            'acceptance': acceptance, 'quantity': quantity,
            'global_conflict_validation': global_report,
            'fixture': {'contract': 'SYNTHETIC_GLOBAL_RESOURCE_V1',
                        'operation_key': key, 'regime_count': count,
                        'calendar_equivalence': equivalence,
                        'outside_commitments': len(global_state['approved_outside']) +
                                               len(global_state['fixed_commitments']),
                        **metadata}}


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Expected output and request snapshot paths')
    output, request = (Path(x) for x in sys.argv[1:])
    start = time.perf_counter()
    result = solve(json.loads(request.read_text(encoding='utf-8')))
    result['runtime_seconds'] = time.perf_counter() - start
    output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding='utf-8')


if __name__ == '__main__':
    main()
