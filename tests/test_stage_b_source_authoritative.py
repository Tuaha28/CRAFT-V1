"""Small exhaustive checks for the pinned-source resource-domain seam."""
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product
from types import SimpleNamespace

import pytest

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.execution_contract import OperationExecutionContract, TEST_ASSUMPTION
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.resource_domain_v2 import ResourceDomainPolicyV2, run_adaptive_planning_schedule
from ga.worker_allocation import operator_available_at


def state():
    problem = SchedulingProblem(
        'source-test', datetime(2026, 1, 1), 86400,
        (LineSpec(1, 1), LineSpec(2, 1)),
        (MachineSpec(10, 1, 1, ('sewing',)), MachineSpec(20, 1, 2, ('sewing',))),
        (OrderSpec('o', 1, 0, None),),
        (OperationSpec('o_1', 'o', '1', 1, 'sewing', (1, 2), 6000, 900, 1, 1),), ())
    row = {
        'problem': problem, 'keys': ('o_1',),
        'after_index': SimpleNamespace(by_key={}), 'operator_index': {
            'crew_of': {'w1': (0, 0), 'w2': (1, 0)}, 'shift_of': {},
            'by_factory': {(1, 'sewing'): ('w1', 'w2')}},
        'source_resource_domain': {
            'contract_version': 'SOURCE_RESOURCE_DOMAIN_V1',
            'source_sha256': 'source', 'snapshot_hash': 'normalized', 'scenario_hash': 'scenario',
            'operations': {'o_1': {
                'factory_id': 1, 'crew_size': 1,
                'machine_ids': (10, 20), 'worker_ids': ('w1', 'w2'),
                'worker_efficiency': {'w1': '1', 'w2': '0.5'},
                'base_productive_minutes': '100', 'setup_seconds': 900,
                'machine_pool_complete': True, 'worker_pool_complete': True}}}}
    row['source_resource_domain']['operations']['o_1']['execution_contract'] = \
        OperationExecutionContract(
            operation_key='o_1', factory_id=1,
            required_worker_count=1, required_machine_count=1,
            required_skills=('sewing',), eligible_worker_ids=('w1', 'w2'),
            eligible_machine_ids=(10, 20), eligible_line_ids=(1, 2),
            quantity=10, target_quantity=10, completed_quantity=0,
            in_flight_quantity=0, quantity_basis='UNSTARTED_REMAINING_UNITS',
            smv_minutes_per_unit='10', setup_seconds=900,
            evidence_class=TEST_ASSUMPTION, source_fingerprint='source',
            snapshot_fingerprint='normalized', scenario_fingerprint='scenario',
            worker_efficiency_percent=(('w1', '100'), ('w2', '50')),
            machine_speedup_factor='1',
            pairing_policy='ELIGIBLE_CARTESIAN_BUNDLE',
            worker_calendar_policy='TEST_CALENDAR',
            machine_calendar_policy='TEST_STATIC_MACHINE_STATUS').as_mapping()
    row['source_resource_provenance'] = {
        'source_sha256': 'source', 'snapshot_hash': 'normalized',
        'scenario_hash': 'scenario'}
    return row


def test_source_domain_expands_to_exact_small_oracle_with_worker_duration():
    row = state()
    compact, audit, _ = generate_resource_configurations(
        row, max_machine_sets=1, max_worker_crews=1)
    expanded, complete_audit, _ = generate_resource_configurations(
        row, max_machine_sets=2, max_worker_crews=2)
    assert set(compact['o_1']) <= set(expanded['o_1'])
    assert {(c.machine_ids[0], c.worker_ids[0]) for c in expanded['o_1']} == \
        set(product((10, 20), ('w1', 'w2')))
    assert {c.worker_ids[0]: c.productive_seconds for c in expanded['o_1']} == \
        {'w1': 6000, 'w2': 12000}
    assert {c.machine_ids[0]: c.line_id for c in expanded['o_1']} == {10: 1, 20: 2}
    assert audit[0]['truncated'] is True
    assert complete_audit[0]['truncated'] is False


def test_explicit_worker_machine_pairs_filter_illegal_cartesian_cross_pairs():
    row = state()
    contract = OperationExecutionContract.from_mapping(
        row['source_resource_domain']['operations']['o_1']['execution_contract'])
    contract = replace(contract, pairing_policy='EXPLICIT_PAIRS',
                       allowed_worker_machine_pairs=(('w1', 10), ('w2', 20)))
    row['source_resource_domain']['operations']['o_1']['execution_contract'] = contract.as_mapping()
    configs, audit, _ = generate_resource_configurations(
        row, max_machine_sets=2, max_worker_crews=2)
    assert {(config.worker_ids[0], config.machine_ids[0])
            for config in configs['o_1']} == {('w1', 10), ('w2', 20)}
    assert audit[0]['total_resource_configurations'] == 2


def test_source_integer_work_survives_worker_dependent_calendar_encoding():
    from math import ceil
    from utils.work_calendar import productive_segments

    row = state()
    spec = row['source_resource_domain']['operations']['o_1']
    contract = OperationExecutionContract.from_mapping(spec['execution_contract'])
    spec['execution_contract'] = replace(
        contract, smv_minutes_per_unit='53.42').as_mapping()
    spec['base_productive_minutes'] = '534.2'
    configs, _, _ = generate_resource_configurations(
        row, max_machine_sets=2, max_worker_crews=2)
    assert len(configs['o_1']) == 4
    for config in configs['o_1']:
        expected = {'w1': 32052, 'w2': 64104}[config.worker_ids[0]]
        assert config.productive_seconds == expected
        assert ceil(float(config.productive_minutes) * 60) == expected
        _, _, segments = productive_segments(
            row['problem'].origin, float(config.productive_minutes), lambda _: True)
        assert sum((end - start).total_seconds() for start, end in segments) == expected


def test_source_domain_rejects_unverified_crew_and_ineligible_machine():
    row = state()
    row['source_resource_domain']['operations']['o_1']['crew_size'] = 2
    with pytest.raises(ValueError, match='UNSUPPORTED_CREW_OR_FACTORY'):
        generate_resource_configurations(row)
    row = state()
    row['source_resource_domain']['operations']['o_1']['machine_ids'] = (10, 999)
    with pytest.raises(ValueError, match='MACHINE_INELIGIBLE'):
        generate_resource_configurations(row)
    row = state()
    row['source_resource_provenance']['scenario_hash'] = 'different'
    with pytest.raises(ValueError, match='PROVENANCE_MISMATCH'):
        generate_resource_configurations(row)
    row = state()
    row['operator_index']['by_factory'][(1, 'sewing')] = ('w1',)
    with pytest.raises(ValueError, match='WORKER_INELIGIBLE'):
        generate_resource_configurations(row)


def test_source_numeric_worker_identity_reaches_calendar():
    # Fixed-crew execution canonicalizes numeric IDs back to their source int
    # before operator_available_at. A string-keyed index would be ignored.
    from crafter.lns_probe.joint_modes import _fixed_crew_execution
    index = {'shift_of': {7: (8, 16)}}
    assert not operator_available_at(index, 7, datetime(2026, 1, 1, 7))
    end, segments = _fixed_crew_execution(SimpleNamespace(), index,
                                           ('7',), datetime(2026, 1, 1, 7), 60, {})
    assert segments == ((datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9)),)
    assert end == datetime(2026, 1, 1, 9)


def test_real_source_calendar_domain_expands_after_blocked_first_worker(monkeypatch):
    row = state()
    origin = row['problem'].origin
    row['operator_index'] = {'crew_of': {}, 'shift_of': {},
                             'by_factory': {(1, 'sewing'): ('w1', 'w2')}}
    row['after_index'].by_key = {'o_1': {'start_minutes': 0}}
    row['after_index'].route_predecessor = {'o_1': None}
    row['_machine_res'] = {}
    row['_worker_res'] = {'w1': ((origin, origin + timedelta(days=1)),)}

    def accept(prepared, **_kwargs):
        assert prepared['regimes']['o_1']
        prepared['acceptance'] = {
            'status': 'ACCEPTED', 'stages': [{'status': 'FEASIBLE'}],
            'winning_stage': {'status': 'FEASIBLE'},
            'selected_schedule': [{'operation_key': 'o_1'}],
            'materialization': {'mismatch_count': 0}}
        return prepared

    monkeypatch.setattr('crafter.repair.resource_domain_v2.run_canonical_schedule', accept)
    result = run_adaptive_planning_schedule(
        row, policy=ResourceDomainPolicyV2(initial_machine_sets=1,
                                           initial_worker_crews=1,
                                           max_expansion_rounds=1))
    rounds = result['resource_domain_v2']['rounds']
    assert len(rounds) == 2
    assert rounds[0]['zero_calendar_domain_keys'] == ['o_1']
    assert rounds[0]['resource_domain_status'] == 'ZERO_CALENDAR_DOMAIN_EXPANDING'
    assert rounds[1]['resource_domain_status'] == 'ACCEPTED'
    assert {c.worker_ids for c in result['resource_configurations']['o_1']} == \
        {('w1',), ('w2',)}
