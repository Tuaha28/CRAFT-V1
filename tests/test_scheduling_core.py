"""
Tests for the scheduling primitives that are done and stable.

Every component covered here has already had a real bug in it, and in each
case the bug was silent -- a wrong number rather than a crash. These are the
assertions that would have caught them.
"""

import math
import random

import pytest

from ga.schedule_utils import (
    DEFAULT_SERIAL_MINS,
    DEFAULT_SETUP_MINS,
    RISK_BUFFER_AT_CERTAINTY,
    RISK_BUFFER_GATE_THRESHOLD,
    apply_risk_buffer,
    calculate_operation_work,
    find_slot_and_insert,
    select_machines_and_start,
    snap_to_shift_window,
)
from ga.operators import repair_precedence
from utils.machine_reserve import usable_cap_for_type


# ---------------------------------------------------------------------------
# Duration formula
# ---------------------------------------------------------------------------

def test_setup_is_charged_once_not_per_machine():
    detail = {'setup_mins': 15.0, 'serial_processing_mins': 600.0}
    assert calculate_operation_work(detail, 1) == 15.0 + 600.0
    assert calculate_operation_work(detail, 10) == 15.0 + 60.0


def test_work_is_divided_by_machines_exactly_once():
    # THE regression test for the 60x bug: total work content divided by the
    # machine count, and by nothing else. If a lot-count division is ever
    # reintroduced anywhere upstream, this stays green but the input shrinks --
    # so it is paired with the explicit magnitude check below.
    detail = {'setup_mins': 0.0, 'serial_processing_mins': 19850.0}
    for machines in (1, 2, 5, 40):
        assert calculate_operation_work(detail, machines) == pytest.approx(19850.0 / machines)


def test_realistic_operation_has_a_realistic_duration():
    # 6,750 units at SAM 2.5 and 85% efficiency is ~19,850 machine-minutes.
    # On six machines that is ~55 hours. The double-division bug produced ~23
    # minutes, which this would have caught immediately.
    serial = (2.5 * 6750) / 0.85
    detail = {'setup_mins': 15.0, 'serial_processing_mins': serial}
    hours = calculate_operation_work(detail, 6) / 60.0
    assert 45 < hours < 65, f"a 6,750-unit operation on 6 machines took {hours:.1f}h"


def test_machine_count_is_floored_at_one():
    detail = {'setup_mins': 10.0, 'serial_processing_mins': 100.0}
    for bad in (0, -3):
        assert calculate_operation_work(detail, bad) == 110.0


def test_missing_fields_fall_back_to_the_shared_defaults():
    assert calculate_operation_work({}, 1) == DEFAULT_SETUP_MINS + DEFAULT_SERIAL_MINS


# ---------------------------------------------------------------------------
# Risk buffer
# ---------------------------------------------------------------------------

def test_risk_buffer_scales_with_probability():
    # Derived from the constant rather than hardcoded, so the shape is
    # asserted independently of whether the cushion is currently enabled.
    assert apply_risk_buffer(100.0, 0.0) == 100.0
    assert apply_risk_buffer(100.0, 1.0) == 100.0 * (1 + RISK_BUFFER_AT_CERTAINTY)
    assert apply_risk_buffer(100.0, 0.5) == pytest.approx(
        100.0 * (1 + 0.5 * RISK_BUFFER_AT_CERTAINTY))


def test_risk_cushion_is_gated_by_confidence():
    # Re-disabled 2026-09-01: the 2026-08-24 single-seed ADOPT decision for
    # RISK_BUFFER_AT_CERTAINTY=0.5 didn't replicate under a 3-seed re-run on
    # the corrected pipeline (ga/schedule_utils.py's own history comment,
    # SESSION_HANDOFF.md Thread 5) -- mean tardiness, the one seed-consistent
    # signal, favoured disabled. RISK_BUFFER_GATE_THRESHOLD stays at its
    # calibrated value regardless -- the gate check still runs, it's just
    # inert while AT_CERTAINTY is 0.0. Documented as a test, same convention
    # as the enabled-era test this replaces, so any future change to either
    # constant is a conscious act with a failing test to update, not
    # something that drifts back silently.
    assert RISK_BUFFER_AT_CERTAINTY == 0.0
    assert RISK_BUFFER_GATE_THRESHOLD == 0.14
    for p in (0.0, 0.05, 0.13):
        assert apply_risk_buffer(500.0, p) == 500.0, f"p={p} is below the gate, should be unpadded"
    for p in (0.14, 0.39, 1.0):
        assert apply_risk_buffer(500.0, p) == pytest.approx(500.0 * (1 + p * RISK_BUFFER_AT_CERTAINTY)), \
            f"p={p} is at/above the gate, should be padded proportionally (currently a no-op at 0.0)"


def test_risk_buffer_clamps_out_of_range_input():
    # A raw logit leaking in here instead of a probability must not silently
    # inflate every duration.
    assert apply_risk_buffer(100.0, 7.3) == apply_risk_buffer(100.0, 1.0)
    assert apply_risk_buffer(100.0, -2.0) == 100.0


# ---------------------------------------------------------------------------
# 7% standby reserve -- integer arithmetic
# ---------------------------------------------------------------------------

def test_reserve_and_usable_are_exactly_complementary():
    # The float trap: 0.07 * 600 is 42.000000000000006, so ceil() would reserve
    # 43 while "93% of 600" is 558, and the two would not add up.
    total = 600
    usable = usable_cap_for_type(total)
    assert usable == 558
    assert total - usable == 42


@pytest.mark.parametrize("total", [1, 2, 7, 14, 99, 100, 600, 601, 1000])
def test_reserve_never_loses_or_invents_machines(total):
    usable = usable_cap_for_type(total)
    assert 0 < usable <= total
    assert isinstance(usable, int)


def test_reserve_never_takes_the_last_machine_of_a_type():
    # A type with one machine must stay schedulable.
    assert usable_cap_for_type(1) == 1
    assert usable_cap_for_type(2) >= 1


def test_reserve_holds_back_roughly_the_configured_fraction():
    for total in (100, 600, 1000):
        held = total - usable_cap_for_type(total)
        assert held / total == pytest.approx(0.07, abs=0.01)


def test_zero_or_negative_total_is_handled():
    assert usable_cap_for_type(0) == 0
    assert usable_cap_for_type(-5) == 0


# ---------------------------------------------------------------------------
# Precedence repair
# ---------------------------------------------------------------------------

def _seq_map_from_op_id(keys):
    """For fixtures where op_key's numeric suffix already IS the routing
    sequence -- builds the explicit map repair_precedence now requires,
    without changing what these tests are verifying."""
    return {k: int(k.split('_')[1]) for k in keys}


def test_repair_orders_operations_within_each_order():
    broken = ["100_3", "101_2", "100_1", "101_1", "100_2"]
    fixed = repair_precedence(list(broken), _seq_map_from_op_id(broken))
    for order in ("100", "101"):
        seq = [int(k.split('_')[1]) for k in fixed if k.startswith(order + "_")]
        assert seq == sorted(seq)


def test_repair_preserves_the_multiset_of_operations():
    # It must reorder, never add or drop -- a chromosome that loses an
    # operation would schedule a partial order and still look valid.
    broken = ["7_5", "7_1", "9_2", "9_1", "7_3"]
    fixed = repair_precedence(list(broken), _seq_map_from_op_id(broken))
    assert sorted(fixed) == sorted(broken)


def test_repair_keeps_the_positions_each_order_occupies():
    # Repair sorts within the slots an order already holds; it must not move
    # an order's work into a different position in the global sequence.
    broken = ["100_2", "200_1", "100_1"]
    fixed = repair_precedence(list(broken), _seq_map_from_op_id(broken))
    assert fixed[1] == "200_1"
    assert fixed[0] == "100_1" and fixed[2] == "100_2"


def test_repair_is_idempotent():
    already_good = ["1_1", "1_2", "2_1", "2_2"]
    seq_map = _seq_map_from_op_id(already_good)
    assert repair_precedence(list(already_good), seq_map) == already_good


def test_repair_follows_routing_sequence_not_operation_id():
    # Operation IDs deliberately out of routing-step order: order 500's
    # routing step 1 is operation_id 30, step 2 is operation_id 10, step 3
    # is operation_id 20. Sorting by the op_key's numeric suffix (the old
    # behaviour) would produce 500_10, 500_20, 500_30 -- routing steps
    # 2, 3, 1 -- an invalid, unexecutable sequence. Sorting by the real
    # operation_sequence_number must produce 500_30, 500_10, 500_20.
    broken = ["500_10", "500_30", "500_20"]
    op_sequence_map = {"500_30": 1, "500_10": 2, "500_20": 3}
    fixed = repair_precedence(list(broken), op_sequence_map)
    assert fixed == ["500_30", "500_10", "500_20"]
    # And the old, wrong assumption really would have gotten this backwards.
    assert sorted(broken, key=lambda k: int(k.split('_')[1])) != fixed


# ---------------------------------------------------------------------------
# Machine selection
# ---------------------------------------------------------------------------

def test_picks_the_least_loaded_machines():
    free = {1: 500.0, 2: 0.0, 3: 100.0}
    start, chosen = select_machines_and_start(
        [1, 2, 3], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0)
    assert sorted(chosen) == [2, 3]
    # Cannot start until the LAST of the chosen machines is free.
    assert start == 100.0


def test_start_respects_the_order_ready_floor():
    free = {1: 0.0, 2: 0.0}
    start, _ = select_machines_and_start(
        [1, 2], free, {}, order_id='A', machine_count=2,
        ready_floor=900.0, changeover_mins=0.0)
    assert start == 900.0


def test_changeover_is_charged_only_when_the_order_changes():
    free = {1: 0.0}
    same, _ = select_machines_and_start(
        [1], free, {1: 'A'}, order_id='A', machine_count=1,
        ready_floor=0.0, changeover_mins=25.0)
    switched, _ = select_machines_and_start(
        [1], free, {1: 'B'}, order_id='A', machine_count=1,
        ready_floor=0.0, changeover_mins=25.0)
    assert same == 0.0
    assert switched == 25.0


def test_changeover_makes_a_same_order_machine_preferable():
    # Machine 1 is free earlier but would need re-rigging; machine 2 is
    # already running this order. The cheaper real start should win.
    free = {1: 0.0, 2: 10.0}
    start, chosen = select_machines_and_start(
        [1, 2], free, {1: 'OTHER', 2: 'A'}, order_id='A', machine_count=1,
        ready_floor=0.0, changeover_mins=25.0)
    assert chosen == [2]
    assert start == 10.0


def test_machine_count_is_capped_by_the_pool():
    free = {1: 0.0, 2: 0.0}
    _start, chosen = select_machines_and_start(
        [1, 2], free, {}, order_id='A', machine_count=10,
        ready_floor=0.0, changeover_mins=0.0)
    assert len(chosen) == 2


def test_empty_pool_returns_the_ready_floor_and_no_machines():
    start, chosen = select_machines_and_start(
        [], {}, {}, order_id='A', machine_count=3,
        ready_floor=42.0, changeover_mins=0.0)
    assert start == 42.0 and chosen == []


# ---------------------------------------------------------------------------
# Bottleneck-protected machine selection (PROJECT_STATUS.txt Section 0H.8)
# ---------------------------------------------------------------------------

def test_unprotected_selection_is_unchanged_regardless_of_is_protected_type_default():
    # is_protected_type defaults to False -- every existing caller (and
    # every test above this one) keeps getting strict least-loaded-first,
    # unchanged by this feature's addition.
    free = {1: 500.0, 2: 0.0, 3: 100.0}
    start, chosen = select_machines_and_start(
        [1, 2, 3], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0)
    assert sorted(chosen) == [2, 3]
    assert start == 100.0


def test_protected_selection_can_pick_outside_strict_least_loaded():
    # 5 candidates, need 2. Strict greedy would always pick {1, 2} (the two
    # least-loaded). With protection on, a seed exists where a machine
    # other than {1, 2} gets picked instead -- proving real randomisation
    # happens, not a silent fallback to greedy.
    #
    # Bounded, not unbounded: each of the `take` slots is filled from a
    # top_k band of whatever's STILL left (a sliding window, not a single
    # fixed band over the original ranking) -- so slot 2 CAN legitimately
    # reach machine 4 (rank 4) if slot 1 already consumed one of the
    # original top-3. What it can never do, with top_k=3 and 5 candidates,
    # is reach machine 5 -- the single globally-worst candidate is always
    # the one excluded from whatever's left, at every slot.
    free = {1: 0.0, 2: 10.0, 3: 20.0, 4: 500.0, 5: 600.0}
    seen_non_greedy = False
    for seed in range(50):
        _start, chosen = select_machines_and_start(
            [1, 2, 3, 4, 5], free, {}, order_id='A', machine_count=2,
            ready_floor=0.0, changeover_mins=0.0,
            is_protected_type=True, top_k=3, rng=random.Random(seed))
        if sorted(chosen) != [1, 2]:
            seen_non_greedy = True
        assert 5 not in chosen, "the globally worst candidate must never be reachable here"
    assert seen_non_greedy, "protected selection never deviated from strict greedy across 50 seeds"


def test_protected_selection_is_reproducible_for_the_same_seed():
    free = {1: 0.0, 2: 10.0, 3: 20.0, 4: 30.0}
    r1 = select_machines_and_start(
        [1, 2, 3, 4], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0,
        is_protected_type=True, rng=random.Random(7))
    r2 = select_machines_and_start(
        [1, 2, 3, 4], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0,
        is_protected_type=True, rng=random.Random(7))
    assert r1 == r2


def test_protected_selection_falls_back_to_greedy_when_pool_has_no_slack():
    # Only exactly `take` machines available -- nothing to be selective
    # about, protected or not.
    free = {1: 0.0, 2: 10.0}
    result_protected = select_machines_and_start(
        [1, 2], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0,
        is_protected_type=True, rng=random.Random(1))
    result_plain = select_machines_and_start(
        [1, 2], free, {}, order_id='A', machine_count=2,
        ready_floor=0.0, changeover_mins=0.0, is_protected_type=False)
    assert sorted(result_protected[1]) == sorted(result_plain[1]) == [1, 2]


def test_protected_selection_never_picks_a_machine_twice():
    free = {i: float(i) for i in range(1, 8)}
    for seed in range(20):
        _start, chosen = select_machines_and_start(
            list(range(1, 8)), free, {}, order_id='A', machine_count=4,
            ready_floor=0.0, changeover_mins=0.0,
            is_protected_type=True, top_k=4, rng=random.Random(seed))
        assert len(chosen) == len(set(chosen)) == 4


# ---------------------------------------------------------------------------
# Shift snapping (now a no-op under 24h operation)
# ---------------------------------------------------------------------------

def test_snapping_is_a_noop_when_the_day_is_fully_covered():
    from datetime import datetime
    for hour in (0, 3, 13, 23):
        dt = datetime(2026, 8, 13, hour, 30)
        assert snap_to_shift_window(dt, 0, 24) == dt


def test_snapping_still_works_if_a_real_window_is_configured():
    # The behaviour must come from the configured hours, not be hardcoded off,
    # so reinstating a 06:00-22:00 factory is a config change.
    from datetime import datetime
    before = datetime(2026, 8, 13, 3, 0)
    assert snap_to_shift_window(before, 6, 22).hour == 6
    inside = datetime(2026, 8, 13, 10, 0)
    assert snap_to_shift_window(inside, 6, 22) == inside
    after = datetime(2026, 8, 13, 23, 0)
    snapped = snap_to_shift_window(after, 6, 22)
    assert snapped.hour == 6 and snapped.day == 14


# ---------------------------------------------------------------------------
# Gap-finding
# ---------------------------------------------------------------------------

def test_find_slot_uses_an_existing_gap():
    intervals = [(0.0, 100.0), (300.0, 400.0)]
    start, updated = find_slot_and_insert(intervals, ready_time=0.0, duration=50.0)
    assert start == 100.0
    assert (100.0, 150.0) in updated


def test_find_slot_appends_when_no_gap_is_large_enough():
    intervals = [(0.0, 100.0), (110.0, 200.0)]
    start, _ = find_slot_and_insert(intervals, ready_time=0.0, duration=50.0)
    assert start == 200.0


def test_find_slot_never_starts_before_ready_time():
    intervals = [(0.0, 10.0)]
    start, _ = find_slot_and_insert(intervals, ready_time=500.0, duration=25.0)
    assert start >= 500.0
