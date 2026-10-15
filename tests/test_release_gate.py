"""
Regression tests for ga/release_gate.py::ReleaseGate
(WLC_RELEASE_GATE_PROPOSAL.md), verified in isolation -- synthetic
DataFrames, no DB, no GA run. Covers the three guards required to ship
with the gate from the start (feasibility+report, starvation override,
manager bypass) and the explicit instruction to confirm the norm draws
from LIVE usable_cap_by_type (utils/machine_reserve.py Section 9), not a
static pre-breakdown baseline.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ga.infeasibility import ScheduleInfeasibleError
from ga.release_gate import ReleaseGate, MINS_PER_DAY
from utils.machine_reserve import usable_cap_for_type

ORIGIN = datetime(2026, 1, 1)


def _lines(n=1):
    return pd.DataFrame([
        {'line_id': i, 'is_active': 1, 'target_efficiency_percentage': 85.0}
        for i in range(1, n + 1)
    ])


def _machines(n, optype='body assembly', line_id=1, statuses=None, id_offset=0):
    # Plain integer asset_ids, matching the real Machinery_Assets schema
    # (a global integer primary key, not type-prefixed) -- build_reserve
    # sorts asset_ids numerically-as-given, so a string-prefixed id here
    # would sort lexicographically instead ('bo10' < 'bo9') and silently
    # pick a different machine as "reserved" than intended.
    statuses = statuses or {}
    return pd.DataFrame([
        {'asset_id': id_offset + i, 'line_id': line_id, 'operation_type': optype,
         'current_status': statuses.get(i, 'Active')}
        for i in range(1, n + 1)
    ])


def _order_row(order_id, release_days, deadline_days, sam=60.0, quantity=1,
                machine_type='Single-Needle Lockstitch', seq=1, op_id=None):
    return {
        'production_order_id': order_id,
        'operation_id': op_id or f'{order_id}_op{seq}',
        'operation_sequence_number': seq,
        'sam': sam,
        'required_machine_type': machine_type,
        'target_quantity': quantity,
        'remaining_frac': 1.0,
        'order_deadline': ORIGIN + timedelta(days=deadline_days) if deadline_days is not None else pd.NaT,
        'release_date': ORIGIN + timedelta(days=release_days),
    }


def _routings(rows):
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Basic norm-respecting admission
# ---------------------------------------------------------------------------

def test_orders_within_norm_admit_immediately():
    df_lines = _lines()
    df_machines = _machines(10)  # policy cap ~9 (93% of 10, floored), all Active
    routings = _routings([_order_row('1', 0, 60), _order_row('2', 0, 60)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert set(admitted) == {'1', '2'}
    assert not gate.has_pending()


def test_order_exceeding_norm_stays_pooled():
    df_lines = _lines()
    df_machines = _machines(10)
    # norm_days tiny -> norm_minutes very small -> even one order's load
    # exceeds it.
    routings = _routings([_order_row('1', 0, 60, sam=600.0, quantity=100)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=0.001, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert admitted == []
    assert gate.has_pending()


def test_smaller_later_order_can_admit_ahead_of_a_blocked_larger_one():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([
        _order_row('big', 0, 60, sam=600.0, quantity=200),   # huge load, release day 0
        _order_row('small', 0, 60, sam=10.0, quantity=1),    # tiny load, release day 0
    ])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=0.05, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert 'small' in admitted
    assert 'big' not in admitted
    assert gate.has_pending()


def test_outstanding_load_blocks_further_admission_until_norm_frees_up():
    df_lines = _lines()
    df_machines = _machines(10)
    avg_efficiency = 0.85  # matches _lines()'s target_efficiency_percentage=85.0
    usable = usable_cap_for_type(10)  # 9
    single_order_mins = (100.0 * 50.0) / avg_efficiency  # sam=100, quantity=50
    # norm sized to comfortably fit ONE such order but not two.
    norm_days = (1.5 * single_order_mins) / (MINS_PER_DAY * usable)

    routings = _routings([
        _order_row('1', 0, 60, sam=100.0, quantity=50),
        _order_row('2', 0, 60, sam=100.0, quantity=50),
    ])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=norm_days, legacy_mode='legacy_company_wide')
    first = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert first == ['1']  # order 1's own load already consumes most of the norm
    # Mark order 1 complete -> frees the pool for order 2.
    gate.mark_completions({'1': 500.0})
    second = gate.admit_next_batch(max_batch=10, current_time_mins=500.0)
    assert second == ['2']


# ---------------------------------------------------------------------------
# Orders not yet released are never admitted early just because they fit
# ---------------------------------------------------------------------------

def test_future_released_order_is_not_admitted_before_its_release_date():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('1', 30, 90)])  # releases in 30 days
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert admitted == []
    assert gate.has_pending()
    # Once current_time_mins reaches its release day, it becomes eligible.
    admitted_later = gate.admit_next_batch(max_batch=10, current_time_mins=30 * MINS_PER_DAY)
    assert admitted_later == ['1']


def test_next_release_mins_lets_a_caller_fast_forward():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('1', 45, 90)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    assert gate.next_release_mins() == pytest.approx(45 * MINS_PER_DAY)


# ---------------------------------------------------------------------------
# Manager bypass (required from the start)
# ---------------------------------------------------------------------------

def test_manager_forced_order_bypasses_the_norm():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('rush', 0, 60, sam=600.0, quantity=200)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=0.001, manager_forced_ids=['rush'], legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert admitted == ['rush']


def test_manager_forced_order_bypasses_the_not_yet_released_check_too():
    # Safe to allow (ga/fitness.py's ready-time floor still independently
    # prevents it from actually STARTING before its real release date) --
    # confirms the bypass has real teeth for a genuine rush order.
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('rush', 90, 120)])  # releases in 90 days
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=100.0, manager_forced_ids=['rush'], legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert admitted == ['rush']


def test_non_forced_orders_unaffected_by_an_unrelated_bypass():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('rush', 0, 60), _order_row('normal', 0, 60)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=100.0, manager_forced_ids=['rush'], legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert set(admitted) == {'rush', 'normal'}


# ---------------------------------------------------------------------------
# Starvation guard (required from the start)
# ---------------------------------------------------------------------------

def test_starved_order_force_admits_past_the_threshold():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('stuck', 0, 60, sam=600.0, quantity=200)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=0.001, starvation_days=10.0, legacy_mode='legacy_company_wide')
    # Before the starvation threshold: still blocked by norm.
    still_blocked = gate.admit_next_batch(max_batch=10, current_time_mins=5 * MINS_PER_DAY)
    assert still_blocked == []
    # Past the starvation threshold: force-admitted despite never fitting.
    force_admitted = gate.admit_next_batch(max_batch=10, current_time_mins=11 * MINS_PER_DAY)
    assert force_admitted == ['stuck']


def test_starvation_never_fires_before_an_order_is_even_released():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('future', 90, 120)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=100.0, starvation_days=1.0, legacy_mode='legacy_company_wide')
    # current_time_mins=0 is 90 days before release -- must not be treated
    # as 90 days of starvation.
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert admitted == []


# ---------------------------------------------------------------------------
# Feasibility check + honest reporting (required from the start)
# ---------------------------------------------------------------------------

def test_infeasible_order_is_reported_not_silently_absorbed():
    df_lines = _lines()
    df_machines = _machines(10)
    # Deadline (5 days) earlier than release (10 days) -- structurally
    # infeasible regardless of any scheduling decision.
    routings = _routings([_order_row('doomed', 10, 5)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    assert len(gate.infeasible_orders) == 1
    assert gate.infeasible_orders[0]['order_id'] == 'doomed'
    assert gate.infeasible_orders[0]['shortfall_days'] > 0


def test_infeasible_order_is_still_pooled_and_still_schedulable():
    # Reporting is not the same as dropping -- an infeasible order must
    # still go through the normal admission path (best-effort scheduling
    # is a business call downstream, not this gate's to make).
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('doomed', 10, 5)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=10 * MINS_PER_DAY)
    assert admitted == ['doomed']


def test_feasible_order_is_not_reported():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('fine', 0, 60)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    assert gate.infeasible_orders == []


def test_order_with_no_deadline_is_never_flagged_infeasible():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('no_deadline', 0, None)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    assert gate.infeasible_orders == []


# ---------------------------------------------------------------------------
# Explicit confirmation: the norm draws from LIVE usable_cap_by_type
# (utils/machine_reserve.py Section 9), not a static pre-breakdown baseline
# ---------------------------------------------------------------------------

def test_norm_uses_live_capacity_not_static_baseline():
    df_lines = _lines()
    routings = _routings([_order_row('1', 0, 60)])

    df_healthy = _machines(100)  # policy cap = 93, none broken
    gate_healthy = ReleaseGate(routings, df_healthy, df_lines, ORIGIN, norm_days=1.0, legacy_mode='legacy_company_wide')

    # Break 50 of the 93 usable (non-reserved) machines.
    broken_statuses = {i: 'Breakdown' for i in range(1, 51)}
    df_degraded = _machines(100, statuses=broken_statuses)
    gate_degraded = ReleaseGate(routings, df_degraded, df_lines, ORIGIN, norm_days=1.0, legacy_mode='legacy_company_wide')

    norm_healthy = gate_healthy.norm_minutes_by_pool['body assembly']
    norm_degraded = gate_degraded.norm_minutes_by_pool['body assembly']

    assert norm_degraded < norm_healthy
    # Exact: norm = norm_days * MINS_PER_DAY * live_usable_cap. Confirms
    # this is really reading the live count, not approximately reactive.
    assert norm_healthy == pytest.approx(1.0 * MINS_PER_DAY * gate_healthy.factory.usable_cap_by_type['body assembly'])
    assert norm_degraded == pytest.approx(1.0 * MINS_PER_DAY * gate_degraded.factory.usable_cap_by_type['body assembly'])
    assert gate_degraded.factory.usable_cap_by_type['body assembly'] < gate_healthy.factory.usable_cap_by_type['body assembly']


def test_full_type_outage_is_structured_infeasibility_not_a_zero_capacity_pool():
    df_lines = _lines()
    routings = _routings([_order_row('1', 0, 60)])
    # Break every non-reserved machine (asset_ids 1-9; asset_id 10 is the
    # single reserved one for total=10, policy cap 9 -- see build_reserve's
    # highest-ID-first reserve selection).
    df_all_broken = _machines(10, statuses={i: 'Breakdown' for i in range(1, 10)})
    # ChromosomeFactory is the release gate's physical eligibility source.
    # It must reject positive work with no schedulable identity instead of
    # constructing a zero-capacity pool that starvation could later admit.
    with pytest.raises(ScheduleInfeasibleError) as exc:
        ReleaseGate(
            routings, df_all_broken, df_lines, ORIGIN, norm_days=1.0,
            legacy_mode='legacy_company_wide',
        )
    assert exc.value.result == {
        'code': 'missing_machine_capacity',
        'production_order_id': '1',
        'operation_key': '1_1_op1',
        'required_capability': 'body assembly',
        'line_id': None,
    }


# ---------------------------------------------------------------------------
# Pool bookkeeping
# ---------------------------------------------------------------------------

def test_multiple_pools_tracked_independently():
    df_lines = _lines()
    df_machines = pd.concat([
        _machines(10, optype='body assembly', id_offset=0),
        _machines(10, optype='bar-tack pocketing', id_offset=100),
    ], ignore_index=True)
    routings = _routings([
        _order_row('a', 0, 60, sam=600.0, quantity=200, machine_type='Single-Needle Lockstitch'),
        _order_row('b', 0, 60, machine_type='Automated Pattern Tacker'),
    ])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=0.05, legacy_mode='legacy_company_wide')
    admitted = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    # 'a' overloads body assembly's norm; 'b' (a different pool) is
    # unaffected by that and admits normally.
    assert 'b' in admitted
    assert 'a' not in admitted


def test_max_batch_caps_admission_per_call():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row(str(i), 0, 60) for i in range(5)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    first = gate.admit_next_batch(max_batch=2, current_time_mins=0.0)
    assert len(first) == 2
    assert gate.has_pending()
    rest = gate.admit_next_batch(max_batch=10, current_time_mins=0.0)
    assert len(rest) == 3
    assert not gate.has_pending()


def test_pool_size_reflects_only_unadmitted_orders():
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row('1', 0, 60), _order_row('2', 0, 60)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    assert gate.pool_size() == 2
    gate.admit_next_batch(max_batch=1, current_time_mins=0.0)
    assert gate.pool_size() == 1


# ---------------------------------------------------------------------------
# Loop-termination integration: mirrors ga/rolling_horizon.py's exact
# gated admission/fast-forward loop (same pattern
# tests/test_rolling_horizon_integration.py already uses for the
# non-gated commit-window arithmetic -- a real GAScheduler run is too
# heavy for a unit test; the real end-to-end exercise of this wiring is
# the planned gated-buffer x release-gate ablation run against real data).
# Confirms the loop this module drives cannot spin forever and never
# loses an order, using a REAL ReleaseGate, not a mock.
# ---------------------------------------------------------------------------

def _drive_gate_to_completion(gate, commit_size, max_rounds=10_000):
    """Replicates ga/rolling_horizon.py's while-loop exactly: advance
    gate_time_mins, call admit_next_batch, fast-forward on an empty
    round, and immediately mark every admitted order complete (a stand-in
    for evaluate_schedule actually resolving them) so the next round's
    outstanding-load check reflects it. Returns (all_admitted_ids_in_order,
    rounds_used)."""
    gate_time_mins = 0.0
    admitted_order = []
    rounds = 0
    while gate.has_pending():
        rounds += 1
        assert rounds <= max_rounds, "loop did not terminate -- possible deadlock"
        admitted = gate.admit_next_batch(max_batch=commit_size, current_time_mins=gate_time_mins)
        if not admitted:
            next_release = gate.next_release_mins()
            if next_release is not None and next_release > gate_time_mins:
                gate_time_mins = next_release
            else:
                gate_time_mins += gate.starvation_mins + 1.0
            continue
        admitted_order.extend(admitted)
        # Stand-in for evaluate_schedule: everything admitted this round
        # "completes" immediately, freeing the pool for the next round.
        gate.mark_completions({oid: gate_time_mins for oid in admitted})
    return admitted_order, rounds


def test_gated_loop_terminates_and_admits_every_order_normal_norm():
    df_lines = _lines()
    df_machines = _machines(100)
    routings = _routings([_order_row(str(i), i % 5, 90) for i in range(20)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=5.0, legacy_mode='legacy_company_wide')
    admitted, rounds = _drive_gate_to_completion(gate, commit_size=5)
    assert sorted(admitted, key=int) == [str(i) for i in range(20)]
    assert not gate.has_pending()
    assert rounds < 100


def test_gated_loop_terminates_even_under_an_impossibly_tight_norm():
    # norm so tight nothing would EVER fit on merit -- only the starvation
    # guard can move these orders. This is the deadlock-risk case the
    # fast-forward logic exists to prevent.
    df_lines = _lines()
    df_machines = _machines(10)
    routings = _routings([_order_row(str(i), 0, 9999, sam=100.0, quantity=100) for i in range(5)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN,
                        norm_days=1e-6, starvation_days=3.0, legacy_mode='legacy_company_wide')
    admitted, rounds = _drive_gate_to_completion(gate, commit_size=5)
    assert sorted(admitted, key=int) == [str(i) for i in range(5)]
    assert not gate.has_pending()
    assert rounds < 100


def test_gated_loop_handles_orders_released_far_apart_in_time():
    df_lines = _lines()
    df_machines = _machines(50)
    routings = _routings([
        _order_row('early', 0, 60),
        _order_row('late', 200, 260),
    ])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=50.0, legacy_mode='legacy_company_wide')
    admitted, rounds = _drive_gate_to_completion(gate, commit_size=5)
    assert sorted(admitted) == ['early', 'late']
    assert rounds < 100


# ---------------------------------------------------------------------------
# Accumulation (ga/rolling_horizon.py's efficiency fix, 2026-08-25): batch
# multiple admit_next_batch calls -- each with its own internal fast-
# forward -- into one GA-round-worth of admissions, up to commit_size,
# instead of one (often much smaller) round per call. Real-data check
# (scratch/release_gate_real_data_check.py) found later calls admitting as
# few as 5-9 orders once the day-0 backlog clears; without accumulation
# that would mean hundreds of full GA rounds (each paying a fixed
# ChromosomeFactory-rebuild cost) to clear the real order book.
# ---------------------------------------------------------------------------

def _drive_gate_with_accumulation(gate, commit_size, max_rounds=10_000, max_inner=10_000):
    """Mirrors ga/rolling_horizon.py's UPDATED (two-clock) gated loop
    exactly: an outer loop representing one GA round per iteration, with
    an inner loop that keeps calling admit_next_batch -- scanning ahead
    with its own scan_time_mins to find more release-eligible orders --
    until commit_size orders are accumulated or the pool is exhausted.
    real_now_mins (the starvation reference) stays fixed for the whole
    accumulation UNLESS the inner loop is genuinely stuck (nothing new to
    scan to, nothing currently eligible fits), in which case it is the
    one that advances, not scan_time_mins alone -- this is exactly the
    fix for the false-starvation-cascade bug
    test_accumulation_still_respects_the_norm_across_the_whole_batch
    exists to catch.
    Returns (all_admitted_ids_in_order, outer_rounds, batch_sizes)."""
    real_now_mins = 0.0
    admitted_order = []
    batch_sizes = []
    outer_rounds = 0
    while gate.has_pending():
        outer_rounds += 1
        assert outer_rounds <= max_rounds, "outer loop did not terminate"
        batch = []
        scan_time_mins = real_now_mins
        inner = 0
        while len(batch) < commit_size and gate.has_pending():
            inner += 1
            assert inner <= max_inner, "inner accumulation loop did not terminate"
            got = gate.admit_next_batch(
                max_batch=commit_size - len(batch),
                current_time_mins=scan_time_mins,
                starvation_reference_mins=real_now_mins)
            if got:
                batch.extend(got)
                continue
            next_release = gate.next_release_mins()
            if next_release is not None and next_release > scan_time_mins:
                scan_time_mins = next_release
                continue
            real_now_mins += gate.starvation_mins + 1.0
            scan_time_mins = max(scan_time_mins, real_now_mins)
        if not batch:
            continue
        admitted_order.extend(batch)
        batch_sizes.append(len(batch))
        gate.mark_completions({oid: real_now_mins for oid in batch})
    return admitted_order, outer_rounds, batch_sizes


def test_accumulation_terminates_and_admits_every_order():
    df_lines = _lines()
    df_machines = _machines(100)
    routings = _routings([_order_row(str(i), i, 90) for i in range(30)])  # one release/day
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    admitted, rounds, batch_sizes = _drive_gate_with_accumulation(gate, commit_size=10)
    assert sorted(admitted, key=int) == [str(i) for i in range(30)]
    assert not gate.has_pending()
    assert rounds < 100
    assert sum(batch_sizes) == 30


def test_accumulation_produces_far_fewer_rounds_than_one_call_per_round():
    # A sparse release distribution (one order every 3 days) is exactly the
    # shape that made the pre-fix one-call-per-round loop expensive: many
    # small per-call admissions. Accumulation must batch them up instead of
    # spending a full GA round on each.
    df_lines = _lines()
    df_machines = _machines(100)
    routings = _routings([_order_row(str(i), i * 3, 9999) for i in range(50)])
    gate_a = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')
    gate_b = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=100.0, legacy_mode='legacy_company_wide')

    _admitted_a, rounds_no_accum = _drive_gate_to_completion(gate_a, commit_size=50)
    _admitted_b, rounds_accum, _sizes = _drive_gate_with_accumulation(gate_b, commit_size=50)

    assert rounds_accum < rounds_no_accum
    # The whole point: accumulation should collapse this down to very few
    # real GA rounds regardless of how many individual release dates exist.
    assert rounds_accum <= 3


def test_accumulation_admits_the_norm_blocked_remainder_via_starvation_not_a_silent_norm_bypass():
    # 3 orders, same release day, norm room for only 2. Correct behaviour,
    # confirmed precisely (not just a total count): orders 1-2 admit
    # because they fit the norm; order 3 -- which does NOT fit -- still
    # ends up admitted in the SAME accumulation round, but only because
    # accumulation's "genuinely stuck" fallback forces real_now_mins past
    # the starvation threshold (there is nothing else in the pool to
    # legitimately wait for, so the deadlock-breaking guarantee that
    # applies across outer rounds correctly applies within one
    # accumulating round too -- this is intended, not a norm violation:
    # the norm did its job for 1-2, starvation resolved the deadlock for
    # 3, exactly as designed). What this test actually guards against: a
    # regression where the norm check gets silently skipped/miscounted
    # during accumulation (order 3 admitted as if it simply "fit",
    # without ever being flagged as an override) -- verified via the
    # gate's own log output, not just the final count.
    log_lines = []
    df_lines = _lines()
    df_machines = _machines(100)
    single_order_mins = (100.0 * 50.0) / 0.85
    usable = usable_cap_for_type(100)
    norm_days = (2.5 * single_order_mins) / (MINS_PER_DAY * usable)  # room for 2, not 3
    routings = _routings([_order_row(str(i), 0, 60, sam=100.0, quantity=50) for i in range(3)])
    gate = ReleaseGate(routings, df_machines, df_lines, ORIGIN, norm_days=norm_days,
                        log_fn=log_lines.append, legacy_mode='legacy_company_wide')
    admitted, _rounds, batch_sizes = _drive_gate_with_accumulation(gate, commit_size=10)

    assert sorted(admitted, key=int) == ['0', '1', '2']
    assert batch_sizes[0] == 3  # all three end up in the same accumulating round
    force_admit_lines = [l for l in log_lines if 'force-admitted' in l]
    assert len(force_admit_lines) == 1, "exactly one order should need the starvation override, not all three"
    assert 'order 2' in force_admit_lines[0]  # the third-in-order one, correctly the blocked one
