"""
THE execution-segmentation model, in one place.

Three components previously disagreed about what a unit of execution is, and
the disagreement was worth 56.6% of the measured worker shortage:

  data_generation/simulation_calendar.py (the SEEDER, and therefore the model
      the GNN's training data was generated under): one Job_Shop_Schedules row
      per LOT; within a lot, work runs in shift-bounded PRODUCTIVE SEGMENTS,
      pausing at every 06/14/22/24 boundary and whenever the assigned worker
      is off duty, and resuming afterwards with THE SAME WORKER -- its module
      docstring says so explicitly: "no implicit handoffs". planned_start/
      planned_end span the whole thing INCLUDING pauses, reserved
      conservatively.

  Historical exporter/evaluator paths disagreed with that model: export
      demanded uninterrupted coverage and fitness used continuous 24/7 work.
      The authoritative phenotype now assigns a fixed deterministic crew,
      walks that crew through this primitive, holds machines across pauses,
      and uses the resulting elapsed end for successors, completion and
      fitness. Export serializes the same phenotype.

This module is the shared execution model. The two-phase seeder, authoritative
fitness decoder and exporter all use it with their own immutable worker roster
inputs rather than maintaining calendar walks that can drift.
"""
from datetime import datetime, time, timedelta
import math

# Shift boundaries: three 8h shifts tiling the day, plus midnight so the walk
# always has a next boundary. Matches the seeder's own list exactly.
SHIFT_BOUNDARY_HOURS = (6, 14, 22, 24)

# Bound on searching unavailable calendar, independent of work duration. Four
# boundaries a day for 28 days: a worker with no shift in four weeks is a data
# problem, not a scheduling one.
MAX_IDLE_BOUNDARIES = 4 * 28


class NoAvailableCalendar(ValueError):
    """The worker has no available shift within the search horizon."""


def _first_unavailable(start, end, is_available, step=timedelta(hours=1)):
    """
    First hourly instant in (start, end) where the worker is NOT available,
    or None if they are available throughout.

    Hourly, plus the last interior instant, matching the resolution the
    superseded continuous check used -- an availability predicate resolving
    shift band and rest day cannot change between hour boundaries, so an
    hourly walk cannot step over a transition. `start` itself is not re-probed:
    the caller has already established availability there.
    """
    probe = start + step
    while probe < end:
        if not is_available(probe):
            return probe
        probe += step
    last_interior = end - timedelta(microseconds=1)
    if last_interior > start and not is_available(last_interior):
        # Availability lapses inside the final partial hour. Fall back to the
        # last whole hour so the segment never covers unavailable time; if
        # that is not strictly after `start`, the segment collapses and the
        # caller advances to the next boundary.
        probe -= step
        return probe if probe > start else start
    return None


def productive_segments(ready, minutes, is_available,
                        max_idle_boundaries=MAX_IDLE_BOUNDARIES,
                        next_availability_change=None):
    """
    Walk the calendar from `ready`, returning the segments in which `minutes`
    of work actually happens.

    is_available(instant) -> bool decides whether the assigned worker is on
    duty at that instant. Injected rather than hard-coded so the seeder and
    the exporter share this walk instead of reimplementing it.

    Returns (start, end, segments). `end - start` spans the pauses too, which
    is what gets reserved; sum(segment lengths) is the productive time.

    Raises NoAvailableCalendar rather than looping forever, and rather than
    returning a short schedule that would silently under-book the work.
    """
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("Work duration must be finite and positive")
    remaining = math.ceil(minutes * 60)
    current = ready.replace(microsecond=0)
    if current < ready:
        current += timedelta(seconds=1)
    segments = []
    idle_boundaries = 0
    while remaining:
        midnight = datetime.combine(current.date(), time.min)
        boundaries = [midnight + timedelta(hours=h) for h in SHIFT_BOUNDARY_HOURS]
        boundary = next(b for b in boundaries if b > current)
        if next_availability_change is not None:
            change = next_availability_change(current)
            if change is not None:
                if change <= current:
                    raise ValueError('next_availability_change must be strictly later than current')
                boundary = min(boundary, change)
        if is_available(current):
            seconds = min(remaining, int((boundary - current).total_seconds()))
            end = current + timedelta(seconds=seconds)
            # Availability is sampled at `current` and the segment then runs to
            # the next SHIFT boundary -- but an injected predicate may change
            # on a finer grid than 06/14/22/24. agent/decision_engine's
            # _available_at resolves shift band AND rest day, and its own
            # superseded comment records that these "change on hour/day
            # boundaries". Filling blindly to the shift boundary would step
            # straight over an hourly transition and book a worker through
            # time they are not on duty.
            #
            # So the chosen segment is re-probed hourly and truncated at the
            # first unavailable instant. When availability is uniform across
            # the shift -- which is exactly the seeder's band/crew case -- no
            # probe fails and the segment stays coalesced, so the seeder's
            # stored planned_work_segments are unchanged.
            truncated = _first_unavailable(current, end, is_available)
            if truncated is not None:
                end = truncated
                seconds = int((end - current).total_seconds())
            if seconds <= 0:
                # Unavailable from this very instant at hourly resolution.
                current = boundary
                idle_boundaries += 1
                if idle_boundaries > max_idle_boundaries:
                    raise NoAvailableCalendar(
                        "Worker has no available shift within the search horizon")
                continue
            segments.append((current, end))
            remaining -= seconds
            current = end
            idle_boundaries = 0
        else:
            # An injected floater schedule may begin on an hour that is not
            # one of the factory band boundaries (06/14/22/24). Jumping
            # straight to the next band boundary skips such a start entirely
            # (e.g. an 08:00 shift queried at 06:00 jumped to 14:00). Advance
            # at the same hourly resolution used by _first_unavailable, while
            # still landing exactly on a nearer band boundary.
            current = min(boundary, current + timedelta(hours=1))
            idle_boundaries += 1
            if idle_boundaries > max_idle_boundaries:
                raise NoAvailableCalendar(
                    "Worker has no available shift within the search horizon")
    return segments[0][0], segments[-1][1], segments


def can_cover_work(ready, minutes, is_available,
                   max_idle_boundaries=MAX_IDLE_BOUNDARIES):
    """
    Can this worker complete `minutes` of work starting from `ready`, allowing
    the pauses the seeder's model allows?

    THIS REPLACES "is the worker continuously on duty for the whole span",
    which is a requirement the seeder never modelled and which no operation
    longer than one 8h shift can ever satisfy. Under the seeder's model the
    same worker resumes after each off-duty pause, so the question is whether
    their recurring availability can absorb the work content -- not whether
    they can do it without stopping.

    Returns (True, end, segments) or (False, None, []).
    """
    try:
        start, end, segments = productive_segments(
            ready, minutes, is_available, max_idle_boundaries)
    except NoAvailableCalendar:
        return False, None, []
    return True, end, segments


def productive_minutes(segments):
    return sum((e - s).total_seconds() / 60.0 for s, e in segments)


def span_minutes(segments):
    if not segments:
        return 0.0
    return (segments[-1][1] - segments[0][0]).total_seconds() / 60.0


def completion_after_productive_minutes(segments, minutes):
    """Return when the requested productive work accrues in existing segments.

    This does not walk a calendar again. It projects base-work completion
    onto already-selected worker evidence, so base and cushioned completion
    cannot disagree about pauses.
    """
    minutes = float(minutes)
    if not math.isfinite(minutes) or minutes < 0:
        raise ValueError("Productive minutes must be finite and non-negative")
    if minutes == 0:
        if not segments:
            raise ValueError("Zero-work completion still needs a start segment")
        return segments[0][0]
    remaining_seconds = minutes * 60.0
    for start, end in segments:
        available = (end - start).total_seconds()
        if remaining_seconds <= available + 1e-9:
            return start + timedelta(seconds=remaining_seconds)
        remaining_seconds -= available
    raise ValueError("Worker segments do not contain the required productive minutes")


# ---------------------------------------------------------------------------
# Bridge: the GA evaluator works in RELATIVE MINUTES from a scheduling origin,
# not datetimes. This is the only place that converts between the two.
# ---------------------------------------------------------------------------

def elapsed_completion_minutes(origin, start_minutes, productive_minutes,
                               is_available=None):
    """
    Productive work content -> ELAPSED completion, in minutes from `origin`.

    This is the primitive ga/fitness.py uses instead of `start + duration`.
    There is deliberately no second calendar implementation in GA code: the
    walk is productive_segments() above, the same one the seeder uses.

    is_available=None means FACTORY-LEVEL availability -- every instant is
    workable. The factory runs three 8h bands that tile all 24 hours
    (utils/shift_rotation.SHIFT_BANDS; utils/config sets SHIFT_START_HOUR=0 /
    SHIFT_END_HOUR=24 precisely because "there is no hour at which a machine
    is unavailable"), so at factory level there is nothing to pause for and
    the walk provably inserts no pauses. That case short-circuits to exact
    float arithmetic rather than round-tripping through datetimes, because
    productive_segments() quantises to whole seconds (math.ceil(minutes*60))
    and the round trip would introduce sub-second drift into every span --
    drift that would make this alignment step falsely appear to change the
    schedule. test_always_available_predicate_is_exactly_shift_blind pins
    the equality, and a separate test walks the same input THROUGH
    productive_segments to confirm the two agree, so the short-circuit is not
    hiding a defect in the walk.

    PREDICATE CONTRACT. Maintained production evaluation supplies the
    assigned fixed crew's complete-availability predicate through the
    authoritative worker allocator. is_available=None remains only an
    explicit worker-blind compatibility mode for isolated formula tests; it
    is not the production scheduling protocol.

    MEASURED ROUNDING PROPERTY. productive_segments quantises to whole
    seconds (math.ceil(minutes * 60)), so a walked span is up to one second
    LONGER per operation than exact float arithmetic. Measured on 2,579 real
    operations: routing an EDF chromosome through the walk with an
    always-available predicate moved makespan by 0.050 minutes in total and
    fitness by 0.002 -- on-time rate and late count identical, mean/max
    tardiness differing only in trailing decimals. That is why the
    factory-level case short-circuits instead of walking: without it, this
    semantics-neutral alignment step would appear to shift every metric
    slightly. When a real predicate is enabled the rounding applies and is
    upward-biased; it is negligible against pause lengths measured in hours,
    but it is not zero and should not be described as such.

    Raises when a predicate is supplied without an origin: a walk needs real
    timestamps, and silently ignoring the predicate would report shift-aware
    semantics while computing shift-blind ones.
    """
    start_minutes = float(start_minutes)
    productive_minutes = float(productive_minutes)
    if productive_minutes <= 0:
        return start_minutes
    if is_available is None:
        return start_minutes + productive_minutes
    if origin is None:
        raise ValueError(
            'A worker-availability predicate needs a scheduling origin: '
            'pass base_time so productive minutes can be walked on a calendar')
    start_dt = origin + timedelta(minutes=start_minutes)
    _start, end_dt, _segments = productive_segments(
        start_dt, productive_minutes, is_available)
    return (end_dt - origin).total_seconds() / 60.0
