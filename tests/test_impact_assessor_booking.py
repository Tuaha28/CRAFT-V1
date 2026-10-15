"""
Regression tests for agent/impact_assessor.py's _book_tentative_slot /
_release_tentative_slot -- the fix for committed_by_asset never being
updated within a single assess_machine_breakdown call (GA_AGENT_POSTMORTEM.md
Section 5c: confirmed for real, the identical candidate_asset_id was
recommended for 5 different affected operations in one real assessment,
before this fix). Pure in-memory DataFrame functions, no DB needed.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from agent.impact_assessor import _book_tentative_slot, _release_tentative_slot, _find_slot

EMPTY = pd.DataFrame(columns=['production_order_id', 'operation_id', 'planned_start_time', 'planned_end_time'])
T0 = datetime(2026, 1, 1, 8, 0, 0)


def test_booking_a_slot_makes_it_visible_to_find_slot():
    committed_by_asset = {}
    start = T0
    end = T0 + timedelta(hours=2)
    _book_tentative_slot(committed_by_asset, EMPTY, asset_id=1, order_id='A', op_id='1',
                          start=start, end=end)

    committed = committed_by_asset[1]
    assert len(committed) == 1

    # A second operation checking the SAME asset for the SAME window must
    # now see it as blocked, not idle -- this is the exact scenario that
    # produced the identical candidate_asset_id 5 times in the real trace.
    candidate_start, blocking_row = _find_slot(committed, start, timedelta(hours=1))
    assert blocking_row is not None
    assert candidate_start >= end  # pushed past the booked slot


def test_two_operations_booking_the_same_asset_get_different_slots():
    committed_by_asset = {}
    empty = EMPTY

    # Operation 1 claims asset 1 starting at T0 for 2 hours.
    committed = committed_by_asset.get(1, empty)
    start1, blocking1 = _find_slot(committed, T0, timedelta(hours=2))
    assert blocking1 is None  # genuinely idle for op 1
    _book_tentative_slot(committed_by_asset, empty, 1, 'orderA', 'op1', start1, start1 + timedelta(hours=2))

    # Operation 2, checking the SAME asset for the SAME ready time, must be
    # pushed to AFTER operation 1's now-booked slot -- not independently
    # "discover" the same idle gap (the pre-fix bug).
    committed = committed_by_asset.get(1, empty)
    start2, blocking2 = _find_slot(committed, T0, timedelta(hours=1))
    assert blocking2 is not None
    assert start2 >= start1 + timedelta(hours=2)
    assert start2 != start1


def test_release_removes_only_the_matching_row():
    committed_by_asset = {}
    empty = EMPTY
    _book_tentative_slot(committed_by_asset, empty, 1, 'orderA', 'op1', T0, T0 + timedelta(hours=1))
    _book_tentative_slot(committed_by_asset, empty, 1, 'orderB', 'op2', T0 + timedelta(hours=2), T0 + timedelta(hours=3))

    _release_tentative_slot(committed_by_asset, 1, 'orderA', 'op1')

    remaining = committed_by_asset[1]
    assert len(remaining) == 1
    assert remaining.iloc[0]['production_order_id'] == 'orderB'


def test_release_on_an_asset_with_no_bookings_is_a_safe_no_op():
    committed_by_asset = {}
    _release_tentative_slot(committed_by_asset, 999, 'orderA', 'op1')  # must not raise
    assert 999 not in committed_by_asset


def test_donor_preemption_release_then_rebook_reflects_the_bump():
    # Mirrors the real donor-preemption sequence: the donor's OLD slot is
    # released, its NEW (bumped) slot is booked, and the preempting
    # operation's own slot is booked at the freed-up old time.
    committed_by_asset = {}
    empty = EMPTY
    old_start, old_end = T0, T0 + timedelta(hours=1)
    _book_tentative_slot(committed_by_asset, empty, 1, 'donor_order', 'donor_op', old_start, old_end)

    new_start, new_end = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    _release_tentative_slot(committed_by_asset, 1, 'donor_order', 'donor_op')
    _book_tentative_slot(committed_by_asset, empty, 1, 'donor_order', 'donor_op', new_start, new_end)
    _book_tentative_slot(committed_by_asset, empty, 1, 'preempting_order', 'preempting_op', old_start, old_end)

    committed = committed_by_asset[1].sort_values('planned_start_time').reset_index(drop=True)
    assert len(committed) == 2
    assert committed.iloc[0]['production_order_id'] == 'preempting_order'
    assert committed.iloc[0]['planned_start_time'] == old_start
    assert committed.iloc[1]['production_order_id'] == 'donor_order'
    assert committed.iloc[1]['planned_start_time'] == new_start
