from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from copy import deepcopy

import pytest

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.solve import run_canonical_schedule
from web_v1.backend.integrations.craft.contracts import digest
from web_v1.backend.integrations.craft.residual_execution import (
    normalize_problem, validate_residual_output, ResidualEvidenceError,
)


def fixture(cross_shift=False):
    origin = datetime(2026, 9, 10, 13, 55) if cross_shift else datetime(2026, 9, 10, 6)
    operations = tuple(OperationSpec('1_' + str(i), '1', str(i), i, 'sew', (1,),
                                     1000 if i == 2 else 120, 0) for i in (2, 3))
    problem = SchedulingProblem('residual-controlled', origin, 90000 if cross_shift else 3600,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ('sew',)),),
        (OrderSpec('1', 1, 0, 3500),), operations, ())
    evidence = {'operation_key': '1_2', 'order_id': '1', 'factory_id': 1,
        'as_of': origin.isoformat(), 'event_time': origin.isoformat(),
        'captured_at': origin.isoformat(), 'available_at': origin.isoformat(),
        'valid_until': (origin + timedelta(days=1)).isoformat(),
        'authorization_id': 'SYNTHETIC-AUTH-1', 'source_operation_hash': digest(asdict(operations[0])),
        'execution_mode': 'SINGLE_UNSTARTED_TAIL', 'residual_transfer_authorized': True,
        'required_quantity': 100, 'in_flight': [], 'future_holds': [], 'remaining_setup_seconds': 0,
        'lots': [{'lot_id': 'completed', 'quantity': 40, 'state': 'COMPLETED',
                  'completed_at': (origin - timedelta(hours=1)).isoformat()},
                 {'lot_id': 'tail', 'quantity': 60, 'state': 'NOT_STARTED'}],
        'remaining_work': {'serial_productive_seconds': 600, 'source': 'STANDARD_WORK_MODEL',
                           'model_version': 'fixture-v1', 'assumptions': ['10 productive seconds/unit; one machine']}}
    index = {'by_factory': {(1, 'sew'): [1]}, 'by_line': {(1, 'sew'): [1]},
             'by_skill': {'sew': [1]}, 'line_factory': {1: 1},
             'shift_of': {1: (6, 14)}, 'crew_of': {}, 'efficiency_of': {(1, 'sew'): 1.0}}
    return problem, evidence, index


def solve_fixture(worker_hold=False, machine_hold=False, cross_shift=False, two_residuals=False):
    problem, evidence, index = fixture(cross_shift)
    second_evidence = None
    if two_residuals:
        second = replace(problem.operations[0], operation_key='2_1', order_id='2', operation_id='1', sequence=1)
        problem = replace(problem, horizon_seconds=7200, orders=problem.orders+(OrderSpec('2', 1, 0, 7100),),
                          operations=problem.operations+(second,))
        second_evidence = {**deepcopy(evidence), 'operation_key': '2_1', 'order_id': '2',
                           'source_operation_hash': digest(asdict(second))}
        problem, _ = normalize_problem(problem, second_evidence, decision_time=problem.origin.isoformat())
    problem, ledger = normalize_problem(problem, evidence, decision_time=problem.origin.isoformat())
    keys = tuple(x.operation_key for x in problem.operations)
    predecessors, successors = {}, {}
    for order in problem.orders:
        route_keys = [op.operation_key for op in sorted(problem.operations, key=lambda o:o.sequence) if op.order_id == order.order_id]
        predecessors.update(zip(route_keys, [None]+route_keys[:-1]))
        successors.update(zip(route_keys, route_keys[1:]+[None]))
    route = SimpleNamespace(rows=(), by_key={k: {'start_minutes': 0} for k in keys},
        route_predecessor=predecessors, route_successor=successors)
    state = {'problem': problem, 'operator_index': index, 'after_index': route,
        'keys': keys, 'baseline': SimpleNamespace(operations=()), 'event': None,
        'planning_context': {'mode': 'INITIAL_PLAN'}, 'objective_profile': 'INITIAL_PLAN_NO_STABILITY',
        'classified': {k: {'state': 'MOVABLE_WINDOW'} for k in keys},
        '_machine_res': {1: [(0, 300, '1', 'maintenance')]} if machine_hold else {},
        '_worker_res': {'1': [(problem.origin, problem.origin + timedelta(seconds=300))]} if worker_hold else {},
        'boundary_facts': (),
    }
    facts = []
    if machine_hold:
        facts.append({'machine_id': 1, 'start_seconds': 0, 'end_seconds': 300, 'production_order_id': '1'})
    if worker_hold:
        facts.append({'worker_id': '1', 'start_seconds': 0, 'end_seconds': 300})
    state['boundary_facts'] = tuple(facts)
    configs = {op.operation_key: (ResourceConfiguration(op.operation_key, 1, 1, 1, (1,), ('1',),
                0, op.serial_productive_seconds, ('CONTROLLED_SINGLE_LEGAL_DOMAIN',), op.serial_productive_seconds / 60),)
               for op in problem.operations}
    regimes, summary, count, equivalence = generate_calendar_regimes(state, configs)
    prepared = {'state': state, 'resource_configurations': configs, 'regimes': regimes}
    result = run_canonical_schedule(prepared, plan=((1, None, 5),), objective_mode=False)
    acceptance = result['acceptance']
    assert acceptance['status'] == 'ACCEPTED', acceptance
    quantity = validate_residual_output(evidence, acceptance['selected_schedule'], decision_time=problem.origin.isoformat())
    second_quantity = validate_residual_output(second_evidence, acceptance['selected_schedule'], decision_time=problem.origin.isoformat()) if second_evidence else None
    return {'acceptance': acceptance, 'quantity': quantity, 'ledger': ledger,
            'second_quantity': second_quantity, 'regime_count': count, 'calendar_equivalence': equivalence}


def test_actual_canonical_residual_vertical_slice():
    result = solve_fixture()
    assert result['acceptance']['replay']['status'] == 'PASS'
    scoped = result['acceptance']['validator']['scope_acceptance']
    assert scoped['scope_status'] == 'SCOPE_VALID'
    assert scoped['physical_status'] == 'VALID'
    assert result['quantity']['scheduled_remaining_quantity'] == 60
    rows = {r['operation_key']: r for r in result['acceptance']['selected_schedule']}
    assert '1_1' not in rows  # no fictional predecessor reintroduced as new work
    assert rows['1_2']['end_seconds'] <= rows['1_3']['start_seconds']
    assert rows['1_2']['end_seconds'] - rows['1_2']['start_seconds'] == 600


@pytest.mark.parametrize('worker,machine', [(True, False), (False, True), (True, True)])
def test_actual_solve_preserves_unavailability(worker, machine):
    assert solve_fixture(worker, machine)['acceptance']['status'] == 'ACCEPTED'


@pytest.mark.parametrize('change', ['future', 'quantity', 'inflight', 'source'])
def test_source_evidence_rejected_before_solve(change):
    p, evidence, _ = fixture()
    if change == 'future': evidence['available_at'] = '2026-09-11T00:00:00'
    if change == 'quantity': evidence['lots'][1]['quantity'] = 61
    if change == 'inflight': evidence['in_flight'] = ['tail']
    if change == 'source': evidence['source_operation_hash'] = 'wrong'
    with pytest.raises(ResidualEvidenceError):
        normalize_problem(p, evidence, decision_time=p.origin.isoformat())


def test_independent_source_work_check_rejects_shortened_candidate():
    result = solve_fixture()
    _, source, _ = fixture()
    rows = deepcopy(result['acceptance']['selected_schedule'])
    next(r for r in rows if r['operation_key'] == '1_2')['worker_segments'] = ((120, 121),)
    with pytest.raises(ResidualEvidenceError):
        validate_residual_output(source, rows, decision_time=source['as_of'])


def test_cross_shift_actual_replay_preserves_whole_operation():
    result = solve_fixture(cross_shift=True)
    rows = {r['operation_key']: r for r in result['acceptance']['selected_schedule']}
    tail = rows['1_2']
    assert sum(b-a for a, b in tail['worker_segments']) == 600
    assert tail['end_seconds'] <= rows['1_3']['start_seconds']
    assert result['acceptance']['replay']['status'] == 'PASS'


def test_evidence_input_is_immutable_and_normalization_repeatable():
    p, e, _ = fixture()
    original = deepcopy(e)
    a, ledger = normalize_problem(p, e, decision_time=p.origin.isoformat())
    b, again = normalize_problem(p, e, decision_time=p.origin.isoformat())
    assert a == b and ledger == again and e == original
    assert p.operation_by_key['1_2'].serial_productive_seconds == 1000


def test_two_partial_operations_compete_without_double_booking():
    result = solve_fixture(two_residuals=True)
    rows = sorted(result['acceptance']['selected_schedule'], key=lambda r:r['start_seconds'])
    assert all(a['end_seconds'] <= b['start_seconds'] for a,b in zip(rows, rows[1:]))
    assert result['second_quantity']['scheduled_remaining_quantity'] == 60
    assert result['quantity']['completed_quantity'] == result['second_quantity']['completed_quantity'] == 40
