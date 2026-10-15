"""
Regression tests for the 2026-08-24 live-capacity fix to
utils/machine_reserve.py::build_reserve (WLC_RELEASE_GATE_PROPOSAL.md
Section 8.4 — a pre-existing gap found while stress-testing the WLC
release-gate proposal, fixed on its own merits ahead of and independent of
that gate).

Before this fix, usable_cap_by_type was a pure function of TOTAL asset
count per type — identical whether every machine was healthy or half were
down, because current_status was never read. These tests confirm the live
count now reacts to Breakdown/Under_Maintenance status while RESERVE
MEMBERSHIP (which specific machines are held back) stays exactly the same
deterministic, status-blind set it always was.
"""

import pandas as pd
import pytest

from ga.chromosome import ChromosomeFactory
from utils.machine_reserve import build_reserve, usable_cap_for_type


def _machines(n, optype='body assembly', statuses=None):
    """n machines of one type, asset_ids 1..n. statuses: optional
    {asset_id: status} override, default 'Active' for all."""
    statuses = statuses or {}
    return pd.DataFrame([
        {'asset_id': i, 'line_id': 1, 'operation_type': optype,
         'current_status': statuses.get(i, 'Active')}
        for i in range(1, n + 1)
    ])


def test_all_active_matches_the_old_policy_cap_exactly():
    # No regression for the common case: nothing broken -> live cap ==
    # policy cap, exactly what build_reserve always returned before.
    df = _machines(100)
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(100)


def test_broken_non_reserved_machine_reduces_live_capacity():
    # 100 machines, policy cap 93 (7% reserved = asset_ids 93-100, the
    # highest 7 IDs per build_reserve's own selection rule). Break a
    # non-reserved machine (asset_id 1) -> live cap drops by exactly 1.
    df = _machines(100, statuses={1: 'Breakdown'})
    reserved, caps = build_reserve(df)
    policy_cap = usable_cap_for_type(100)
    assert 1 not in reserved  # sanity: asset 1 is not one of the reserved (highest-ID) machines
    assert caps['body assembly'] == policy_cap - 1


def test_a_reserved_machine_breaking_does_not_cost_usable_capacity():
    # The whole point of the reserve: a broken RESERVED machine is exactly
    # what it exists to absorb, not an additional capacity loss.
    df = _machines(100)
    reserved, _caps = build_reserve(df)
    some_reserved_id = next(iter(reserved))

    df_broken = _machines(100, statuses={some_reserved_id: 'Breakdown'})
    reserved2, caps2 = build_reserve(df_broken)
    assert reserved2 == reserved  # reserve membership itself is unaffected
    assert caps2['body assembly'] == usable_cap_for_type(100)  # no capacity lost


def test_reserve_membership_is_unchanged_by_status_status_blind():
    # RESERVE MEMBERSHIP must stay a pure function of asset_id, never of
    # current_status -- the exact invariant the module's docstring commits
    # to (a differing reserve between the GA run and a breakdown
    # assessment would be a real, separate bug class).
    df_healthy = _machines(50)
    df_half_broken = _machines(50, statuses={i: 'Breakdown' for i in range(1, 26)})
    reserved_healthy, _ = build_reserve(df_healthy)
    reserved_broken, _ = build_reserve(df_half_broken)
    assert reserved_healthy == reserved_broken


def test_live_capacity_floors_at_zero_not_negative():
    # Every non-reserved machine of a type down at once -> 0, not negative.
    df = _machines(10)
    reserved, _ = build_reserve(df)
    all_broken_statuses = {i: 'Breakdown' for i in range(1, 11) if i not in reserved}
    df_all_broken = _machines(10, statuses=all_broken_statuses)
    _reserved2, caps = build_reserve(df_all_broken)
    assert caps['body assembly'] == 0


def test_under_maintenance_counts_the_same_as_breakdown():
    df = _machines(100, statuses={1: 'Under_Maintenance'})
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(100) - 1


def test_idle_status_does_not_reduce_capacity():
    # Idle is a normal, momentarily-unassigned state, not a breakdown --
    # must not be treated as unusable.
    df = _machines(100, statuses={1: 'Idle'})
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(100)


def test_missing_current_status_column_reproduces_old_behaviour():
    # Synthetic test fixtures elsewhere in this suite (e.g.
    # tests/test_mc_protection.py) build machine DataFrames with no
    # current_status column at all -- must not crash, and must behave
    # exactly as before this fix (every machine treated as Active).
    df = pd.DataFrame([
        {'asset_id': i, 'line_id': 1, 'operation_type': 'body assembly'}
        for i in range(1, 101)
    ])
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(100)


def test_duplicate_capability_rows_per_asset_do_not_double_count():
    # fetch_machine_state() LEFT JOINs Machine_Capabilities, so a machine
    # capable of more than one operation type can appear as more than one
    # row for the SAME asset_id within a group. A broken machine appearing
    # twice must still only cost 1 unit of capacity, not 2.
    rows = [{'asset_id': 1, 'line_id': 1, 'operation_type': 'body assembly',
              'current_status': 'Breakdown'}] * 2
    rows += [{'asset_id': i, 'line_id': 1, 'operation_type': 'body assembly',
               'current_status': 'Active'} for i in range(2, 101)]
    df = pd.DataFrame(rows)
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(100) - 1


def test_multiple_types_tracked_independently():
    df = pd.concat([
        _machines(50, optype='body assembly', statuses={1: 'Breakdown'}),
        _machines(20, optype='bar-tack pocketing'),
    ], ignore_index=True)
    _reserved, caps = build_reserve(df)
    assert caps['body assembly'] == usable_cap_for_type(50) - 1
    assert caps['bar-tack pocketing'] == usable_cap_for_type(20)


def test_decoder_identity_pools_exclude_down_assets_but_keep_idle_assets():
    """The aggregate live-cap fix must reach the actual selectable IDs."""
    machines = _machines(
        100,
        statuses={1: 'Breakdown', 2: 'Under_Maintenance', 3: 'Idle'},
    )
    machines['factory_id'] = 1
    routings = pd.DataFrame([{
        'production_order_id': 1,
        'operation_id': 10,
        'operation_sequence_number': 1,
        'required_machine_type': 'single-needle lockstitch',
    }])
    lines = pd.DataFrame([{'line_id': 1, 'is_active': 1}])

    factory = ChromosomeFactory(
        routings, machines, lines, legacy_mode='legacy_company_wide')
    selectable = set(factory.machine_ids_per_line_map['1_10'][1])

    assert 1 not in selectable
    assert 2 not in selectable
    assert 3 in selectable
    assert selectable == factory.usable_asset_ids
