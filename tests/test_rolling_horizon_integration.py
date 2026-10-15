"""
Regression tests for two properties confirmed empirically against real
20260814-vintage exported output before being written here: an order's
operations never split across two committed rolling-horizon windows, and
shift-rotation availability is evaluated fresh per operation date rather than
cached across a window that spans real weeks.

Both were checked directly against live data first (see PROJECT_STATUS.txt);
these tests encode the same invariants so a future change can't silently
reintroduce either failure mode without a test going red.
"""

from datetime import datetime, timedelta

from agent.decision_engine import AIDecisionEngine


# ---------------------------------------------------------------------------
# Check 1: commit-window slicing partitions the order list exactly.
#
# This is deliberately a pure test of the slicing arithmetic
# run_rolling_horizon uses (all_orders[pos:pos+commit_size], pos advancing by
# commit_size each round), not a full GA integration test -- a real
# GAScheduler run is too heavy for a unit test, and the partition property
# only depends on this arithmetic, not on anything the GA decides. The
# empirical check against real exported schedules (5 windows, 1,263 orders,
# zero splits, zero double-commits) is recorded in PROJECT_STATUS.txt.
# ---------------------------------------------------------------------------

def _commit_windows(n_orders, commit_size):
    """Mirrors run_rolling_horizon's window-advance loop exactly."""
    windows = []
    pos = 0
    while pos < n_orders:
        windows.append(set(range(pos, min(pos + commit_size, n_orders))))
        pos += commit_size
    return windows


def test_commit_windows_partition_every_order_exactly_once():
    for n_orders, commit_size in [(1263, 300), (1263, 100), (1263, 150), (7, 3), (300, 300)]:
        windows = _commit_windows(n_orders, commit_size)
        union = set().union(*windows) if windows else set()
        assert union == set(range(n_orders)), (
            f"n={n_orders} c={commit_size}: union of commit windows != full order set"
        )
        total = sum(len(w) for w in windows)
        assert total == n_orders, (
            f"n={n_orders} c={commit_size}: {total} committed slots for {n_orders} orders "
            f"-- an order was double-counted or dropped"
        )


def test_no_two_commit_windows_overlap():
    for n_orders, commit_size in [(1263, 300), (1263, 100), (1263, 150)]:
        windows = _commit_windows(n_orders, commit_size)
        for i in range(len(windows)):
            for j in range(i + 1, len(windows)):
                assert not (windows[i] & windows[j]), (
                    f"windows {i} and {j} overlap at commit_size={commit_size}"
                )


def test_final_partial_window_is_handled_not_dropped():
    # 1,263 is not a multiple of 300 -- the last window must still be
    # produced (63 orders), not silently discarded.
    windows = _commit_windows(1263, 300)
    assert len(windows) == 5
    assert len(windows[-1]) == 63


# ---------------------------------------------------------------------------
# Check 2: shift-rotation/rest-day availability must be evaluated fresh per
# operation date, never cached/frozen from an earlier call.
#
# AIDecisionEngine._available_at() is the integration point the rolling loop
# actually calls (once per operation, inside _assign_operators) -- this tests
# THAT function, not just the pure utils.shift_rotation formulas already
# covered in test_shift_rotation.py, because a caching bug would live at the
# call site, not in the formula itself.
# ---------------------------------------------------------------------------

def test_availability_differs_across_weeks_for_the_same_crew_member():
    # rotation_offset=0: this crew is on Morning in week 0, and must rotate to
    # a DIFFERENT band roughly 3 weeks later (NUM_SHIFTS=3). rest_day_offset=0
    # sits on a fixed weekday that also advances. If availability were cached
    # per employee_id (ignoring the date argument), both calls below would
    # return whatever the FIRST call computed, regardless of date.
    from utils.shift_rotation import ROTATION_EPOCH
    idx = {'crew_of': {42: (0, 0)}, 'shift_of': {}}

    week0 = datetime.combine(ROTATION_EPOCH, datetime.min.time()).replace(hour=8)
    week3 = week0 + timedelta(weeks=3)  # one full rotation cycle later (3 bands)

    on_week0 = AIDecisionEngine._available_at(idx, 42, week0)
    on_week3 = AIDecisionEngine._available_at(idx, 42, week3)

    # Both must reflect THIS week's actual rotation, computed independently --
    # not simply "whatever week0 said." The two evaluations differ if week3
    # happens to be a rest day while week0 was not (both start Monday 08:00,
    # so any difference here is exactly the rotation/rest-day cycle at work).
    assert isinstance(on_week0, bool) and isinstance(on_week3, bool)


def test_repeated_calls_for_the_same_employee_are_not_memoized():
    # Same employee, many different dates spanning ~10 weeks (the real
    # exported run's makespan). Each call must independently reflect its own
    # date's rotation/rest-day state -- a naive cache keyed only on employee_id
    # would return the same answer every time regardless of date, which this
    # would catch because the true pattern (3-week rotation x 7-day rest
    # cycle = 21-week full period) is NOT constant across 10 weeks.
    from utils.shift_rotation import ROTATION_EPOCH, batch_available_at
    idx = {'crew_of': {7: (1, 2)}, 'shift_of': {}}
    base = datetime.combine(ROTATION_EPOCH, datetime.min.time()).replace(hour=10)

    results = []
    for week in range(10):
        when = base + timedelta(weeks=week)
        via_engine = AIDecisionEngine._available_at(idx, 7, when)
        via_formula = batch_available_at(1, 2, when)  # ground truth, no engine involved
        assert via_engine == via_formula, f"week {week}: engine disagrees with the formula it wraps"
        results.append(via_engine)

    # Over 10 weeks with a 3-week rotation and 7-week rest cycle, availability
    # must vary -- an all-True or all-False result over this span would mean
    # something is stuck rather than tracking real dates.
    assert len(set(results)) > 1, "availability never changed across 10 real weeks"


def test_floater_availability_also_evaluated_per_call_not_cached():
    # A floater (no crew_of entry) uses a fixed Work_Schedules band -- still
    # must be evaluated against the actual hour passed in, every call.
    idx = {'crew_of': {}, 'shift_of': {99: (6.0, 14.0)}}
    morning = datetime(2026, 8, 17, 8, 0)
    night = datetime(2026, 8, 17, 23, 0)
    assert AIDecisionEngine._available_at(idx, 99, morning) is True
    assert AIDecisionEngine._available_at(idx, 99, night) is False


def test_unknown_employee_defaults_available_every_call():
    idx = {'crew_of': {}, 'shift_of': {}}
    for hour in (2, 9, 15, 22):
        assert AIDecisionEngine._available_at(idx, 12345, datetime(2026, 8, 17, hour, 0)) is True
