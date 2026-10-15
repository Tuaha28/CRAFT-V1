from copy import deepcopy
import pytest

from .test_execution_boundary import source, lot, rehash
from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot
from web_v1.backend.integrations.craft.boundary_validator import validate_snapshot
from web_v1.backend.integrations.craft.residual_tail import assess_residual_tails
from web_v1.backend.integrations.craft.residual_continuation import (
    classify_residual_operation, classify_snapshot, validate_classification,
)


@pytest.mark.parametrize('mutation', ['lose_tail', 'double_tail', 'movable', 'machine', 'worker',
                                    'future_completion', 'future_observed', 'invent_work', 'invent_boundary'])
def test_residual_forgery_rejected(mutation):
    raw = source([lot(amount=4), lot(2, 'Scheduled', '2026-09-11T00:00:00', '2026-09-11T01:00:00', 6)])
    snapshot = normalize_snapshot(raw)
    op = snapshot['operations'][0]
    if mutation == 'lose_tail': op['residual_lot_ids'].pop()
    if mutation == 'double_tail': op['residual_lot_ids'].append(op['residual_lot_ids'][0])
    if mutation == 'movable': op['optimizable'] = True
    if mutation == 'machine': op['resource_holds'][0]['machine_id'] = None
    if mutation == 'worker': op['resource_holds'][0]['worker_id'] = None
    if mutation == 'future_completion': op['completion_boundary'] = '2026-09-11T01:00:00'
    if mutation == 'future_observed': op['lots'][1]['observed_completion'] = '2026-09-11T01:00:00'
    if mutation == 'invent_work': op['quantity']['remaining_productive_seconds'] = 3600
    if mutation == 'invent_boundary': op['execution_boundary_end'] = '2026-09-11T01:00:00'
    rehash(snapshot)
    result = validate_snapshot(snapshot, raw)
    assert not result['valid_for_craft']
    assert set(result['error_counts']) - {'BOUNDARY_UNRESOLVED'}


def test_tail_assessment_does_not_authorize_projected_plan():
    snapshot = normalize_snapshot(source([lot()]))
    before = deepcopy(snapshot)
    result = assess_residual_tails(snapshot)
    assert result == assess_residual_tails(deepcopy(snapshot))
    assert result['boundary_unresolved'] == 1
    assert result['resolved_committed_boundary'] == 0
    assert result['operations'][0]['execution_boundary_end'] is None
    assert snapshot == before


def test_completed_and_unstarted_are_not_partial_continuation_cases():
    completed = normalize_snapshot(source([lot(state='Done', end='2026-09-09T23:30:00', amount=10)]))
    unstarted = normalize_snapshot(source([lot(state='Scheduled', start='2026-09-11T00:00:00', end='2026-09-11T01:00:00', amount=10)]))
    assert classify_snapshot(completed)['total_partial_operations'] == 0
    assert classify_snapshot(unstarted)['total_partial_operations'] == 0


def test_partial_operation_requires_all_residual_lots_and_finite_boundary():
    snapshot = normalize_snapshot(source([lot(1, 'Done', end='2026-09-09T23:30:00', amount=3),
                                          lot(2, 'Scheduled', '2026-09-11T00:00:00', '2026-09-11T01:00:00', 7)]))
    record = classify_residual_operation(snapshot['operations'][0])
    assert record['classification'] == 'BOUNDARY_UNRESOLVED'
    assert 'PROOF_OPERATION_LEVEL_RESIDUAL_IS_SPLITTABLE' in record['reasons']
    assert 'PHYSICAL_COMPLETION_BOUNDARY' in record['reasons']
    assert validate_classification(snapshot, classify_snapshot(snapshot))['valid']


def test_inflight_requires_resource_and_calendar_continuation():
    snapshot = normalize_snapshot(source([lot(amount=10)]))
    record = classify_residual_operation(snapshot['operations'][0])
    assert record['classification'] == 'BOUNDARY_UNRESOLVED'
    assert 'IN_FLIGHT_REMAINING_PRODUCTIVE_WORK' in record['reasons']
    assert 'FIXED_CREW_CONTINUATION_SEGMENTS' in record['reasons']
    assert 'FINITE_MACHINE_OCCUPANCY_BOUNDARY' in record['reasons']


def test_row_order_and_classification_hash_are_deterministic():
    raw = source([lot(1, 'Done', end='2026-09-09T23:30:00', amount=3),
                  lot(2, 'Scheduled', '2026-09-11T00:00:00', '2026-09-11T01:00:00', 7)])
    one = normalize_snapshot(raw)
    other = normalize_snapshot(deepcopy(raw))
    assert classify_snapshot(one)['classification_hash'] == classify_snapshot(other)['classification_hash']
