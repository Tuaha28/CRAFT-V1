"""
Tests for the weekly shift rotation and staggered rest-day calendar.

These exist because the whole roster is derived from two integers and a date.
An off-by-one in the week arithmetic would not crash anything -- it would
quietly roster crews to the wrong shift, and the only symptom would be a
schedule that looks plausible.
"""

from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from utils.shift_rotation import (
    DAYS_PER_WEEK,
    NUM_SHIFTS,
    ROTATION_EPOCH,
    SHIFT_BANDS,
    assign_round_robin,
    band_covers,
    batch_available_at,
    is_rest_day,
    rest_weekday_for_batch,
    shift_band_for_batch,
    shift_index_for_batch,
    weeks_since_epoch,
)


# --------------------------------------------------------------------------
# Week arithmetic
# --------------------------------------------------------------------------

def test_epoch_is_a_monday():
    # Week boundaries are assumed to align with weekday() == 0 throughout.
    assert ROTATION_EPOCH.weekday() == 0


def test_weeks_since_epoch_counts_whole_weeks():
    assert weeks_since_epoch(ROTATION_EPOCH) == 0
    assert weeks_since_epoch(ROTATION_EPOCH + timedelta(days=6)) == 0
    assert weeks_since_epoch(ROTATION_EPOCH + timedelta(days=7)) == 1
    assert weeks_since_epoch(ROTATION_EPOCH + timedelta(days=13)) == 1
    assert weeks_since_epoch(ROTATION_EPOCH + timedelta(days=14)) == 2


def test_weeks_before_epoch_are_negative_but_rotation_stays_valid():
    # Floor division makes pre-epoch dates negative; Python's % keeps the
    # index in range, which is why no special case is needed.
    assert weeks_since_epoch(ROTATION_EPOCH - timedelta(days=1)) == -1
    for offset in range(NUM_SHIFTS):
        idx = shift_index_for_batch(offset, ROTATION_EPOCH - timedelta(days=1))
        assert 0 <= idx < NUM_SHIFTS


def test_date_and_datetime_agree():
    d = ROTATION_EPOCH + timedelta(days=100)
    assert weeks_since_epoch(d) == weeks_since_epoch(datetime.combine(d, datetime.min.time()))


def test_year_boundary_does_not_reset_the_cycle():
    # The rotation counts days from the epoch, so a calendar year rollover is
    # not a boundary at all. 2024-12-30 is a Monday; the following Monday is
    # in the next year and must be exactly one rotation step later.
    last_monday_2024 = date(2024, 12, 30)
    next_monday_2025 = date(2025, 1, 6)
    assert last_monday_2024.weekday() == 0
    assert weeks_since_epoch(next_monday_2025) == weeks_since_epoch(last_monday_2024) + 1
    for offset in range(NUM_SHIFTS):
        before = shift_index_for_batch(offset, last_monday_2024)
        after = shift_index_for_batch(offset, next_monday_2025)
        assert after == (before + 1) % NUM_SHIFTS


def test_leap_day_is_just_another_day():
    feb28 = date(2024, 2, 28)
    mar1 = date(2024, 3, 1)   # 2024 is a leap year, so Feb 29 sits between
    assert (mar1 - feb28).days == 2
    assert weeks_since_epoch(mar1) - weeks_since_epoch(feb28) in (0, 1)


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------

def test_shift_advances_by_one_band_each_week():
    for offset in range(NUM_SHIFTS):
        for week in range(10):
            d = ROTATION_EPOCH + timedelta(weeks=week)
            nxt = d + timedelta(weeks=1)
            assert shift_index_for_batch(offset, nxt) == (
                shift_index_for_batch(offset, d) + 1) % NUM_SHIFTS


def test_shift_is_constant_within_a_week():
    for offset in range(NUM_SHIFTS):
        week_start = ROTATION_EPOCH + timedelta(weeks=5)
        indices = {shift_index_for_batch(offset, week_start + timedelta(days=n))
                   for n in range(7)}
        assert len(indices) == 1


def test_operation_at_a_week_transition_gets_the_new_week():
    # Sunday 23:59 and the Monday 00:00 immediately after are in different
    # rotation weeks. This is the boundary most likely to be off by one.
    week_start = ROTATION_EPOCH + timedelta(weeks=3)
    sunday_late = datetime.combine(week_start - timedelta(days=1), datetime.max.time())
    monday_early = datetime.combine(week_start, datetime.min.time())
    for offset in range(NUM_SHIFTS):
        assert shift_index_for_batch(offset, monday_early) == (
            shift_index_for_batch(offset, sunday_late) + 1) % NUM_SHIFTS


def test_the_three_offsets_cover_all_three_shifts_every_week():
    # This is the property that makes a line with three crews fully covered:
    # in any week the three offsets must sit on three different bands.
    for week in range(20):
        d = ROTATION_EPOCH + timedelta(weeks=week)
        assert {shift_index_for_batch(o, d) for o in range(NUM_SHIFTS)} == set(range(NUM_SHIFTS))


# --------------------------------------------------------------------------
# Band coverage, including the midnight wrap
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hour,expected", [
    (6, 'Morning'), (13, 'Morning'),
    (14, 'Evening'), (21, 'Evening'),
    (22, 'Night'), (23, 'Night'), (0, 'Night'), (3, 'Night'), (5, 'Night'),
])
def test_every_hour_belongs_to_exactly_one_band(hour, expected):
    when = datetime(2026, 8, 13, hour, 0)
    covering = [b[0] for b in SHIFT_BANDS if band_covers(b, when)]
    assert covering == [expected]


def test_bands_partition_the_whole_day():
    for hour in range(24):
        when = datetime(2026, 8, 13, hour, 30)
        assert sum(1 for b in SHIFT_BANDS if band_covers(b, when)) == 1


def test_band_boundaries_are_half_open():
    morning = SHIFT_BANDS[0]
    assert band_covers(morning, datetime(2026, 8, 13, 6, 0))       # inclusive start
    assert not band_covers(morning, datetime(2026, 8, 13, 14, 0))  # exclusive end


# --------------------------------------------------------------------------
# Rest days
# --------------------------------------------------------------------------

def test_rest_day_advances_one_weekday_per_week():
    for offset in range(DAYS_PER_WEEK):
        d = ROTATION_EPOCH + timedelta(weeks=2)
        assert rest_weekday_for_batch(offset, d + timedelta(weeks=1)) == (
            rest_weekday_for_batch(offset, d) + 1) % DAYS_PER_WEEK


def test_each_batch_rests_exactly_one_day_per_week():
    for offset in range(DAYS_PER_WEEK):
        week_start = ROTATION_EPOCH + timedelta(weeks=7)
        resting = [n for n in range(7) if is_rest_day(offset, week_start + timedelta(days=n))]
        assert len(resting) == 1


def test_rest_days_spread_across_all_seven_weekdays():
    # With seven offsets in play, every weekday must be somebody's rest day in
    # any given week -- that is what makes headcount dip evenly instead of the
    # factory having a shared closure day.
    week_start = ROTATION_EPOCH + timedelta(weeks=11)
    rest_weekdays = {rest_weekday_for_batch(o, week_start) for o in range(DAYS_PER_WEEK)}
    assert rest_weekdays == set(range(DAYS_PER_WEEK))


def test_rest_day_at_a_year_boundary():
    # Same check as the shift rotation: the cycle must step, not reset.
    a = date(2024, 12, 30)
    b = date(2025, 1, 6)
    for offset in range(DAYS_PER_WEEK):
        assert rest_weekday_for_batch(offset, b) == (
            rest_weekday_for_batch(offset, a) + 1) % DAYS_PER_WEEK


# --------------------------------------------------------------------------
# Combined availability
# --------------------------------------------------------------------------

def test_rest_day_beats_the_shift_rule():
    # A crew on the right shift but resting is NOT available. This is the
    # case a shift-only check would get wrong.
    offset_rot, offset_rest = 0, 0
    week_start = ROTATION_EPOCH + timedelta(weeks=4)
    rest_wd = rest_weekday_for_batch(offset_rest, week_start)
    rest_date = week_start + timedelta(days=rest_wd)
    band = shift_band_for_batch(offset_rot, rest_date)
    # Pick an hour squarely inside the band they would otherwise be working.
    hour = 8 if band[0] == 'Morning' else (16 if band[0] == 'Evening' else 23)
    when = datetime.combine(rest_date, datetime.min.time()) + timedelta(hours=hour)
    assert band_covers(band, when)
    assert is_rest_day(offset_rest, when)
    assert batch_available_at(offset_rot, offset_rest, when) is False


def test_available_when_on_shift_and_not_resting():
    offset_rot, offset_rest = 0, 3
    week_start = ROTATION_EPOCH + timedelta(weeks=4)
    for n in range(7):
        d = week_start + timedelta(days=n)
        if is_rest_day(offset_rest, d):
            continue
        band = shift_band_for_batch(offset_rot, d)
        hour = 8 if band[0] == 'Morning' else (16 if band[0] == 'Evening' else 23)
        when = datetime.combine(d, datetime.min.time()) + timedelta(hours=hour)
        assert batch_available_at(offset_rot, offset_rest, when) is True
        return
    pytest.fail("every day of the week was a rest day, which is impossible")


def test_unavailable_outside_the_rotated_band():
    offset_rot, offset_rest = 1, 5
    d = ROTATION_EPOCH + timedelta(weeks=6)
    while is_rest_day(offset_rest, d):
        d += timedelta(days=1)
    band = shift_band_for_batch(offset_rot, d)
    for hour in range(24):
        when = datetime.combine(d, datetime.min.time()) + timedelta(hours=hour)
        assert batch_available_at(offset_rot, offset_rest, when) == band_covers(band, when)


# --------------------------------------------------------------------------
# Round-robin offset assignment
# --------------------------------------------------------------------------

def test_round_robin_distributes_both_offsets_evenly():
    n = 210  # a common multiple of 3 and 7, so a perfect split is expected
    rots = Counter()
    rests = Counter()
    for i in range(n):
        r, rd = assign_round_robin(i)
        rots[r] += 1
        rests[rd] += 1
    assert set(rots) == set(range(NUM_SHIFTS))
    assert set(rests) == set(range(DAYS_PER_WEEK))
    assert len(set(rots.values())) == 1, f"rotation offsets uneven: {dict(rots)}"
    assert len(set(rests.values())) == 1, f"rest-day offsets uneven: {dict(rests)}"


def test_round_robin_stays_near_even_at_awkward_counts():
    # Real batch counts will not be multiples of 21.
    for n in (99, 100, 297, 1000):
        rots = Counter()
        rests = Counter()
        for i in range(n):
            r, rd = assign_round_robin(i)
            rots[r] += 1
            rests[rd] += 1
        assert max(rots.values()) - min(rots.values()) <= 1
        assert max(rests.values()) - min(rests.values()) <= 1


def test_offsets_do_not_lock_in_step():
    # 3 and 7 are coprime, so the pair (rotation, rest) should cycle through
    # all 21 combinations rather than repeating a short pattern -- otherwise
    # some crews would always rest on the same shift.
    pairs = {assign_round_robin(i) for i in range(21)}
    assert len(pairs) == NUM_SHIFTS * DAYS_PER_WEEK
