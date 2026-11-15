"""Exact integer-second fixed-crew execution for calendar regime generation.

``utils.work_calendar.productive_segments`` walks datetimes with hourly
availability probes; regime generation calls it tens of thousands of times
per window and it dominated CRAFT runtime (~80% with calendar regimes).

``CrewCalendar.execute`` is the same algorithm, step for step, on integer
seconds: the same shift boundaries (06/14/22/24), the same next-reservation
truncation, the same hourly probing and idle-boundary limit, the same
second rounding. Availability is precomputed exactly:

* a batch crew's shift/rest-day availability depends only on the date and on
  whole-hour band edges (utils/shift_rotation), so it is constant within each
  clock hour and memoized per hour slot;
* reservations are half-open ``[left, right)`` intervals, merged once and
  queried by bisection.

Anything outside those preconditions (a worker without a batch crew, a
non-integral timestamp) returns ``None`` and the caller uses the original
primitive. Replay and the independent validators always use the original
primitive, so any divergence would be rejected, never accepted silently.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
import math

from utils.shift_rotation import batch_available_at
from utils.work_calendar import MAX_IDLE_BOUNDARIES, NoAvailableCalendar

_EPOCH = datetime(1970, 1, 1)
_HOUR = 3600
_DAY = 86400
_BOUNDARY_OFFSETS = (6 * _HOUR, 14 * _HOUR, 22 * _HOUR, 24 * _HOUR)


def _abs_seconds(value):
    """Whole seconds since the naive epoch, or None if not integral."""
    delta = value - _EPOCH
    if delta.microseconds:
        return None
    return delta.days * _DAY + delta.seconds


class CrewCalendar:
    """Exact availability of one fixed crew under one reservation map."""

    def __init__(self, operator_index, workers, reservations):
        crews = operator_index.get("crew_of", {})
        self.supported = True
        self._crews = []
        for employee in workers:
            key = int(employee) if str(employee).isdigit() else employee
            crew = crews.get(key)
            if crew is None:
                # Floater bands may change off the hour grid; keep the
                # original primitive for them.
                self.supported = False
                return
            self._crews.append((int(crew[0]), int(crew[1])))
        self._crews = tuple(sorted(set(self._crews)))
        edges, intervals = set(), []
        for employee in workers:
            for left, right in reservations.get(str(employee), ()):
                a, b = _abs_seconds(left), _abs_seconds(right)
                if a is None or b is None:
                    self.supported = False
                    return
                edges.update((a, b))
                if b > a:
                    intervals.append((a, b))
        merged = []
        for a, b in sorted(intervals):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self._starts = [a for a, _b in merged]
        self._ends = [b for _a, b in merged]
        self._edges = sorted(edges)
        self._slots = {}

    def _on_shift(self, t):
        slot = t // _HOUR
        value = self._slots.get(slot)
        if value is None:
            when = _EPOCH + timedelta(seconds=slot * _HOUR)
            value = all(batch_available_at(rotation, rest, when) for rotation, rest in self._crews)
            self._slots[slot] = value
        return value

    def _reserved(self, t):
        index = bisect_right(self._starts, t) - 1
        return index >= 0 and t < self._ends[index]

    def available(self, t):
        return self._on_shift(t) and not self._reserved(t)

    def _next_change(self, t):
        index = bisect_right(self._edges, t)
        return self._edges[index] if index < len(self._edges) else None

    def execute(self, ready, minutes, max_idle_boundaries=MAX_IDLE_BOUNDARIES):
        """(start, end, segments) in absolute seconds, as productive_segments."""
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("Work duration must be finite and positive")
        remaining = math.ceil(minutes * 60)
        current = ready
        segments = []
        idle = 0
        # Hot path (millions of walks per window): available(), _on_shift(),
        # _reserved() and _next_change() are inlined here with identical
        # semantics; tests/test_fast_calendar.py checks against the original.
        slots, crews = self._slots, self._crews
        starts, ends, edges = self._starts, self._ends, self._edges
        edge_count = len(edges)

        def available(t):
            value = slots.get(t // _HOUR)
            if value is None:
                when = _EPOCH + timedelta(seconds=(t // _HOUR) * _HOUR)
                value = all(batch_available_at(rotation, rest, when) for rotation, rest in crews)
                slots[t // _HOUR] = value
            if not value:
                return False
            index = bisect_right(starts, t) - 1
            return not (index >= 0 and t < ends[index])

        while remaining:
            time_of_day = current % _DAY
            # First of the 06/14/22/24 boundaries strictly after current.
            if time_of_day < 6 * _HOUR:
                boundary = current - time_of_day + 6 * _HOUR
            elif time_of_day < 14 * _HOUR:
                boundary = current - time_of_day + 14 * _HOUR
            elif time_of_day < 22 * _HOUR:
                boundary = current - time_of_day + 22 * _HOUR
            else:
                boundary = current - time_of_day + _DAY
            index = bisect_right(edges, current)
            if index < edge_count and edges[index] < boundary:
                boundary = edges[index]
            if available(current):
                seconds = min(remaining, boundary - current)
                end = current + seconds
                truncated = None
                probe = current + _HOUR
                while probe < end:
                    if not available(probe):
                        truncated = probe
                        break
                    probe += _HOUR
                if truncated is None and end > current and not available(end - 1):
                    # productive_segments probes end - 1 microsecond; with
                    # integral edges that instant shares availability with
                    # end - 1 second (same clock hour, same reservations).
                    probe -= _HOUR
                    truncated = probe if probe > current else current
                if truncated is not None:
                    end = truncated
                    seconds = end - current
                if seconds <= 0:
                    current = boundary
                    idle += 1
                    if idle > max_idle_boundaries:
                        raise NoAvailableCalendar(
                            "Worker has no available shift within the search horizon")
                    continue
                segments.append((current, end))
                remaining -= seconds
                current = end
                idle = 0
            else:
                current = min(boundary, current + _HOUR)
                idle += 1
                if idle > max_idle_boundaries:
                    raise NoAvailableCalendar(
                        "Worker has no available shift within the search horizon")
        return segments[0][0], segments[-1][1], segments


def fast_execution(problem, operator_index, workers, ready_seconds, productive_minutes,
                   reservations, crew_cache):
    """(end, segments) relative to problem.origin, or None to use the original."""
    origin = _abs_seconds(problem.origin)
    if origin is None:
        return None
    workers = tuple(str(v) for v in workers)
    calendar = crew_cache.get(workers)
    if calendar is None:
        calendar = CrewCalendar(operator_index, workers, reservations)
        crew_cache[workers] = calendar
    if not calendar.supported:
        return None
    _start, end, segments = calendar.execute(origin + int(ready_seconds), productive_minutes)
    return end - origin, tuple((a - origin, b - origin) for a, b in segments)
