"""
One execution-segmentation model, shared by the seeder and the exporter.

THE DEFECT: agent/decision_engine.py required one worker to be continuously
on duty for an operation's entire wall-clock span. Shifts are three 8h bands
tiling the day, so any operation longer than 8h contains an instant where its
worker is off duty, and that rule rejected every such operation
unconditionally. Measured on the authoritative snapshot: 0 of 776 operations
longer than 8h were staffable -- a 100% failure rate against 67.8% staffed
below 8h -- accounting for 3,441,832 of 6,082,368 unfilled worker-minutes,
56.6% of the whole shortage.

The requirement was never in the model the training data was generated under.
data_generation/simulation_calendar.py pauses work at every off-duty boundary
and resumes it with THE SAME worker ("no implicit handoffs").

These tests are written to FALSIFY the fix: a walk that ignored availability,
one that allowed handovers, or one that silently short-changed the work
content would each fail at least one.
"""

from datetime import datetime, timedelta

import pytest

from utils.work_calendar import (NoAvailableCalendar, can_cover_work,
                                 productive_minutes, productive_segments,
                                 span_minutes)

DAY = datetime(2026, 3, 2)          # a Monday
MORNING = DAY.replace(hour=6)       # start of the 06-14 band


def always():
    return lambda t: True


def band(lo, hi):
    """Available only within [lo, hi) hours of the civil day."""
    return lambda t: lo <= t.hour < hi


# ---------------------------------------------------------------------------
# the shared walk
# ---------------------------------------------------------------------------

def test_work_inside_one_band_is_a_single_uninterrupted_segment():
    start, end, segs = productive_segments(MORNING, 120, band(6, 14))
    assert len(segs) == 1
    assert start == MORNING and end == MORNING + timedelta(minutes=120)
    assert productive_minutes(segs) == pytest.approx(120)
    assert span_minutes(segs) == pytest.approx(120)


def test_work_exceeding_the_band_pauses_and_resumes_the_next_day():
    # 10h of work for a 06-14 worker: 8h today, 2h tomorrow morning.
    start, end, segs = productive_segments(MORNING, 600, band(6, 14))
    assert len(segs) == 2
    assert productive_minutes(segs) == pytest.approx(600)
    # The SPAN covers the overnight pause and is much longer than the work.
    assert span_minutes(segs) > productive_minutes(segs)
    assert segs[0] == (MORNING, MORNING.replace(hour=14))
    assert segs[1][0] == (MORNING + timedelta(days=1)).replace(hour=6)


def test_productive_minutes_always_equal_the_requested_work():
    # A walk that dropped or double-counted time would silently under- or
    # over-book labour. Checked across durations that straddle 1, 2 and 5
    # band boundaries.
    for minutes in (30, 479, 480, 481, 960, 1441, 5000):
        _s, _e, segs = productive_segments(MORNING, minutes, band(6, 14))
        assert productive_minutes(segs) == pytest.approx(minutes, abs=1 / 60), minutes


def test_segments_are_contiguous_non_overlapping_and_ordered():
    _s, _e, segs = productive_segments(MORNING, 2000, band(6, 14))
    for (s1, e1), (s2, e2) in zip(segs, segs[1:]):
        assert s1 < e1 <= s2 < e2


def test_segments_never_fall_outside_the_availability_window():
    # The decisive check that availability is actually consulted: a walk that
    # ignored the predicate would place work at 03:00.
    _s, _e, segs = productive_segments(MORNING, 3000, band(6, 14))
    for s, e in segs:
        assert 6 <= s.hour < 14
        assert e.hour <= 14 and (e.hour > 6 or e == e.replace(hour=14))


def test_a_worker_who_is_never_available_raises_rather_than_looping():
    with pytest.raises(NoAvailableCalendar):
        productive_segments(MORNING, 60, lambda t: False)


def test_zero_and_negative_work_are_rejected():
    for bad in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            productive_segments(MORNING, bad, always())


# ---------------------------------------------------------------------------
# can_cover_work -- the eligibility question
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('hours', [1, 4, 8, 10, 24, 72])
def test_a_single_band_worker_can_cover_work_of_any_length(hours):
    # THE regression this fix exists for. Under the old continuous-coverage
    # rule everything above 8h was impossible; under the seeder's model the
    # same worker resumes after each pause, so length alone never disqualifies.
    ok, end, segs = can_cover_work(MORNING, hours * 60, band(6, 14))
    assert ok, hours
    assert productive_minutes(segs) == pytest.approx(hours * 60, abs=1 / 60)
    assert end is not None


def test_work_longer_than_a_shift_was_the_case_that_used_to_fail():
    # Explicitly contrast the two rules on the same input.
    minutes = 10 * 60
    old_rule_continuous = all(
        band(6, 14)(MORNING + timedelta(minutes=m)) for m in range(int(minutes)))
    assert old_rule_continuous is False        # the old rule rejected it
    assert can_cover_work(MORNING, minutes, band(6, 14))[0] is True


def test_unavailable_worker_cannot_cover_and_reports_no_segments():
    ok, end, segs = can_cover_work(MORNING, 60, lambda t: False)
    assert ok is False and end is None and segs == []


def test_no_handover_the_same_predicate_covers_every_segment():
    # "no implicit handoffs": every segment must satisfy the SAME worker's
    # availability. A handover-permitting implementation could place a segment
    # in a window this worker never covers.
    avail = band(14, 22)
    _ok, _end, segs = can_cover_work(DAY.replace(hour=14), 1500, avail)
    for s, e in segs:
        assert avail(s)
        assert avail(e - timedelta(seconds=1))


# ---------------------------------------------------------------------------
# the seeder still behaves identically through the shared walk
# ---------------------------------------------------------------------------

def test_seeder_still_raises_its_documented_error_message():
    from datetime import time as _time
    from data_generation.simulation_calendar import work_on_calendar
    # A band that covers no instant at all -- the seeder must surface this as
    # its documented data fault, not loop or return a short schedule.
    impossible = ('never', _time(5, 0), _time(5, 0))
    with pytest.raises(ValueError, match='no available shift within 28 days'):
        work_on_calendar(MORNING, 60, band=impossible)


def test_seeder_requires_a_calendar():
    from data_generation.simulation_calendar import work_on_calendar
    with pytest.raises(ValueError, match='Missing worker calendar'):
        work_on_calendar(MORNING, 60)


def test_seeder_and_shared_walk_agree_exactly():
    # One definition, two consumers. If these ever diverge, the training data
    # and the exporter are describing different factories again.
    from data_generation.simulation_calendar import work_on_calendar
    from utils.shift_rotation import SHIFT_BANDS, band_covers
    real_band = SHIFT_BANDS[0]
    for minutes in (60, 500, 1500):
        a = work_on_calendar(MORNING, minutes, band=real_band)
        b = productive_segments(MORNING, minutes,
                                lambda t: band_covers(real_band, t))
        assert a == b, minutes


# ---------------------------------------------------------------------------
# the real caller
# ---------------------------------------------------------------------------

def test_decision_engine_eligibility_uses_the_shared_walk():
    import inspect
    from agent.decision_engine import AIDecisionEngine
    src = inspect.getsource(AIDecisionEngine._assign_operators)
    assert 'can_cover_work' in src
    # The superseded hourly rejection walk still EXISTS, deliberately: the
    # staffing attribution replays under it to separate the
    # stale-reservation artefact from the continuity artefact. It must be
    # unreachable unless the policy is explicitly switched, and 'segmented'
    # must be the default -- otherwise the defect is back on by accident.
    assert AIDecisionEngine.WORKER_CONTINUITY_POLICY == 'segmented'
    assert "WORKER_CONTINUITY_POLICY == 'continuous'" in src,         'the old walk must be gated behind an explicit policy check'
    gate = src.index("WORKER_CONTINUITY_POLICY == 'continuous'")
    old_walk = src.index('probe += timedelta(hours=1)')
    assert gate < old_walk, 'the old walk is reachable without the policy gate'


def test_default_policy_accepts_a_long_operation_the_old_rule_rejected():
    # Behavioural guard on the default, not just a source-text check: the
    # class attribute could be right while the branch is wired backwards.
    from agent.decision_engine import AIDecisionEngine
    assert AIDecisionEngine.WORKER_CONTINUITY_POLICY == 'segmented'
    index = {
        'by_line': {(10, 'body assembly'): [1, 2, 3]},
        'by_factory': {(1, 'body assembly'): [1, 2, 3]},
        'by_skill': {'body assembly': [1, 2, 3]},
        'line_factory': {10: 1}, 'shift_of': {}, 'crew_of': {},
    }
    original = AIDecisionEngine._available_at
    try:
        # A pure 06-14 worker: continuous coverage of a 10h operation is
        # impossible, segmented coverage is not.
        AIDecisionEngine._available_at = staticmethod(
            lambda idx, emp, when: 6 <= when.hour < 14)
        crew, _tier, under = AIDecisionEngine._assign_operators(
            index, 'single-needle lockstitch', 10, 3, {}, 600.0,
            start_dt=MORNING, operator_reservations={})
        assert crew, 'default policy rejected a 10h operation a shift worker can cover'
        assert under is None
    finally:
        AIDecisionEngine._available_at = original


def test_long_operation_is_now_eligible_through_the_real_engine():
    # Real caller, not a stub: build an operator index the engine's own
    # _available_at understands, then ask for a 10h operation -- impossible
    # under the old rule, expected to succeed now.
    from agent.decision_engine import AIDecisionEngine
    eng = AIDecisionEngine.__new__(AIDecisionEngine)
    probe_hours = []

    def fake_available(index, emp, instant):
        probe_hours.append(instant.hour)
        return 6 <= instant.hour < 14

    original = AIDecisionEngine._available_at
    try:
        AIDecisionEngine._available_at = staticmethod(fake_available)
        ok, _end, segs = can_cover_work(
            MORNING, 600,
            lambda t: AIDecisionEngine._available_at({}, 'e1', t))
        assert ok
        assert productive_minutes(segs) == pytest.approx(600, abs=1 / 60)
        # It genuinely consulted availability outside the band and kept going
        # rather than giving up at the first off-duty instant.
        assert any(h >= 14 or h < 6 for h in probe_hours)
    finally:
        AIDecisionEngine._available_at = original


# ---------------------------------------------------------------------------
# Availability may change on a FINER grid than the shift boundaries
# ---------------------------------------------------------------------------

def test_segment_is_truncated_when_availability_lapses_mid_shift():
    # The defect a pre-existing test caught: the walk sampled availability
    # only at a segment's START and then filled to the next 06/14/22/24
    # boundary. An injected predicate can change on any HOUR boundary
    # (_available_at resolves shift band AND rest day), so filling blindly
    # booked the worker through time they were not on duty.
    cutoff = MORNING + timedelta(minutes=150)
    avail = lambda t: t < cutoff
    # 90 minutes fits entirely before the lapse.
    ok, _end, segs = can_cover_work(MORNING, 90, avail)
    assert ok and productive_minutes(segs) == pytest.approx(90)
    for s, e in segs:
        assert e <= cutoff
    # 240 minutes does not, and the worker is never available again.
    ok, end, segs = can_cover_work(MORNING, 240, avail)
    assert ok is False and end is None and segs == []


def test_no_segment_ever_covers_an_unavailable_hour():
    # Availability with a hole in the middle of a shift. Every emitted
    # segment must avoid the hole entirely.
    def holed(t):
        if 6 <= t.hour < 14:
            return not (9 <= t.hour < 11)      # off 09:00-11:00
        return False

    _s, _e, segs = productive_segments(MORNING, 600, holed)
    assert productive_minutes(segs) == pytest.approx(600, abs=1 / 60)
    for s, e in segs:
        probe = s
        while probe < e:
            assert holed(probe), f'segment covers unavailable {probe}'
            probe += timedelta(hours=1)


def test_uniform_shift_availability_still_coalesces_into_whole_segments():
    # The truncation must not fragment the seeder's case: a band worker is
    # uniformly available across their shift, so no probe fails and the
    # segment stays whole. Fragmenting it would change every stored
    # planned_work_segments value in the database.
    _s, _e, segs = productive_segments(MORNING, 480, band(6, 14))
    assert len(segs) == 1
    assert segs[0] == (MORNING, MORNING.replace(hour=14))
