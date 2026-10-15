"""
Weekly shift rotation and staggered rest days for production batches.

A BATCH IS A CREW. It moves between shifts as a unit, one shift per week, and
it takes one rest day a week. Both are derived from two small integers stored
on the batch at creation, so a crew's whole calendar is reproducible from its
row plus a date -- nothing has to be materialised per day.

    shift_index(batch, date) = (weeks_since_epoch(date) + rotation_offset) % 3
    rest_weekday(batch, date) = (weeks_since_epoch(date) + rest_day_offset) % 7

Offsets are assigned round-robin as batches are formed, not randomly, so that
at any point in time the three shifts carry a balanced share of crews and the
rest days spread evenly across all seven weekdays. Roughly 1/7 of batches are
resting on any given day, which means every day runs slightly short-handed
rather than the factory having a shared closure day -- so no code anywhere
needs a "factory closed" special case.

SOURCE OF TRUTH: for an operator who belongs to a batch, these formulas are
the ONLY thing that determines availability. Their personal Work_Schedules row
is ignored -- a batch member follows the crew. Work_Schedules remains
authoritative for floaters (operators in no batch), unchanged.

Shared deliberately: the seeder uses these to staff lots, and the scheduler
uses them to staff operations. Two implementations of a modular-arithmetic
calendar would drift, and the drift would be invisible until a schedule
rostered someone on their rest day.
"""

from datetime import date, datetime, time, timedelta

# The three bands, in rotation order. Index 0/1/2 is what shift_index returns.
SHIFT_BANDS = [
    ('Morning', time(6, 0, 0), time(14, 0, 0)),
    ('Evening', time(14, 0, 0), time(22, 0, 0)),
    ('Night', time(22, 0, 0), time(6, 0, 0)),   # wraps past midnight
]

NUM_SHIFTS = len(SHIFT_BANDS)
DAYS_PER_WEEK = 7

# Fixed reference point for all rotation arithmetic. A Monday, so a "rotation
# week" runs Monday-Sunday and week boundaries line up with weekday() == 0.
# Must never change once a database has been seeded: moving it re-phases every
# crew's calendar.
ROTATION_EPOCH = date(2024, 1, 1)


def _as_date(value):
    """Accepts a date or a datetime; rotation is a whole-day concept."""
    if isinstance(value, datetime):
        return value.date()
    return value


def weeks_since_epoch(value) -> int:
    """
    Whole weeks between ROTATION_EPOCH and the given date.

    Floor division, so dates before the epoch give negative weeks. That is
    intentional and safe: Python's % always returns a non-negative result for
    a positive modulus, so (-1) % 3 == 2 and the rotation stays well-defined
    on both sides of the epoch without a special case.
    """
    return (_as_date(value) - ROTATION_EPOCH).days // DAYS_PER_WEEK


def shift_index_for_batch(rotation_offset: int, value) -> int:
    """Which of the three bands this crew works in the week containing `value`."""
    return (weeks_since_epoch(value) + int(rotation_offset)) % NUM_SHIFTS


def shift_band_for_batch(rotation_offset: int, value):
    """The (name, start_time, end_time) this crew works that week."""
    return SHIFT_BANDS[shift_index_for_batch(rotation_offset, value)]


def shift_name_for(when: datetime) -> str:
    """Which of the three named bands covers this clock time."""
    for band in SHIFT_BANDS:
        if band_covers(band, when):
            return band[0]
    return 'unknown'  # unreachable given the bands partition the day; defensive only


def band_covers(band, when: datetime) -> bool:
    """
    Is this clock time inside the band?

    Handles the night band wrapping past midnight: 22:00-06:00 covers times at
    or after 22:00 OR before 06:00, not the range between them. Getting this
    inverted would silently roster the night crew to daytime work.
    """
    _name, start, end = band
    t = when.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def rest_weekday_for_batch(rest_day_offset: int, value) -> int:
    """
    Which weekday (Monday=0 .. Sunday=6) this crew rests on, in the week
    containing `value`. Advances by one weekday each week, so the rest day
    staggers rather than sitting on a fixed day forever.
    """
    return (weeks_since_epoch(value) + int(rest_day_offset)) % DAYS_PER_WEEK


def is_rest_day(rest_day_offset: int, value) -> bool:
    """Is the given date this crew's rest day?"""
    d = _as_date(value)
    return d.weekday() == rest_weekday_for_batch(rest_day_offset, d)


def batch_available_at(rotation_offset: int, rest_day_offset: int, when: datetime) -> bool:
    """
    THE combined availability check for a batch member.

    A crew is available for work at a moment only if BOTH hold:
      - the week's rotation puts them on the band covering that clock hour, and
      - that date is not their rest day.

    Deliberately one function rather than two separate checks at each call
    site: an eligibility test that applies the shift rule but forgets the rest
    day would roster a resting crew, and the omission would be invisible in
    any output that only shows hours.
    """
    if when is None:
        return True
    if is_rest_day(rest_day_offset, when):
        return False
    return band_covers(shift_band_for_batch(rotation_offset, when), when)


def assign_round_robin(sequence_number: int) -> tuple:
    """
    (rotation_offset, rest_day_offset) for the Nth batch created.

    Round-robin on both, with different moduli, so the two cycles do not lock
    in step: shift coverage stays balanced across crews and rest days spread
    evenly over all seven weekdays. Random assignment would leave both
    lumpy at this batch count.
    """
    return sequence_number % NUM_SHIFTS, sequence_number % DAYS_PER_WEEK
