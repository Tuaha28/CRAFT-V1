"""Deterministic worker allocation for the authoritative schedule phenotype.

Employee identity is a decoding decision, not a chromosome gene.  This module
contains no database access and mutates no caller-owned state.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from itertools import combinations
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from utils.shift_rotation import batch_available_at
from utils.work_calendar import NoAvailableCalendar, productive_segments

EFFICIENCY_WEIGHT_BASE = 0.30
EFFICIENCY_WEIGHT_MAX = 0.60


def reservation_key(employee_id) -> str:
    return str(employee_id)


def operator_available_at(operator_index: Mapping, employee_id, when: datetime) -> bool:
    crew = operator_index.get("crew_of", {}).get(employee_id)
    if crew is not None:
        rotation_offset, rest_day_offset = crew
        return batch_available_at(rotation_offset, rest_day_offset, when)
    band = operator_index.get("shift_of", {}).get(employee_id)
    if band is None:
        return True
    start_h, end_h = band
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def normalize_reservations(reservations: Mapping | None) -> Dict[str, List[Tuple[datetime, datetime]]]:
    normalized: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for employee_id, intervals in (reservations or {}).items():
        key = reservation_key(employee_id)
        cleaned = []
        for start, end in intervals:
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                raise TypeError(f"Worker reservation for {key} must contain datetimes")
            if end <= start:
                raise ValueError(f"Invalid worker reservation for {key}: end <= start")
            cleaned.append((start, end))
        merged: List[Tuple[datetime, datetime]] = []
        for start, end in sorted(cleaned):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        normalized[key] = merged
    return normalized


def reservation_busy_minutes(reservations: Mapping) -> Dict[str, float]:
    return {
        str(employee_id): sum((end - start).total_seconds() / 60.0 for start, end in intervals)
        for employee_id, intervals in reservations.items()
    }


def _overlaps(intervals, proposed) -> bool:
    return any(
        busy_start < work_end and work_start < busy_end
        for work_start, work_end in proposed
        for busy_start, busy_end in intervals
    )


def _free_at(intervals, when) -> bool:
    """Membership in normalized half-open reservations, O(log n)."""
    if not intervals:
        return True
    index = bisect_right(intervals, (when, datetime.max))
    return index == 0 or not (
        intervals[index - 1][0] <= when < intervals[index - 1][1]
    )


def _merge_reserved_segments(existing, additions):
    """Merge additions into one employee's sorted half-open segment list."""
    merged = []
    for segment_start, segment_end in sorted([*existing, *additions]):
        if merged and segment_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], segment_end))
        else:
            merged.append((segment_start, segment_end))
    return merged


def _calendar_key(operator_index: Mapping, employee_id):
    if employee_id in operator_index.get("crew_of", {}):
        return ("crew",) + tuple(operator_index["crew_of"][employee_id])
    if employee_id in operator_index.get("shift_of", {}):
        return ("shift",) + tuple(operator_index["shift_of"][employee_id])
    return ("always",)


def _rank(pool, operator_index, optype, busy_minutes, priority):
    pool = list(dict.fromkeys(pool))
    if not pool:
        return []
    loads = {e: float(busy_minutes.get(reservation_key(e), 0.0)) for e in pool}
    effs = {e: operator_index.get("efficiency_of", {}).get((e, optype)) for e in pool}
    known = [float(v) for v in effs.values() if v is not None]
    max_load = max(loads.values()) if loads else 0.0
    p = min(max(float(priority if priority is not None else 0.0), 0.0), 1.0)
    weight = EFFICIENCY_WEIGHT_BASE + p * (EFFICIENCY_WEIGHT_MAX - EFFICIENCY_WEIGHT_BASE)
    if not known or max(known) == min(known):
        return sorted(pool, key=lambda e: (loads[e], str(e)))
    lo, hi = min(known), max(known)
    mean_eff = sum(known) / len(known)

    def score(e):
        load_term = loads[e] / max_load if max_load > 0 else 0.0
        own = float(effs[e]) if effs[e] is not None else mean_eff
        skill_term = (hi - own) / (hi - lo)
        return ((1.0 - weight) * load_term + weight * skill_term, str(e))

    return sorted(pool, key=score)


def _candidate_groups(operator_index, candidates, ready, work_minutes):
    """Group workers whose individual productive calendar is identical.

    Selecting a full crew from one group makes the crew-intersection schedule
    exact without assuming shift handover. A mixed-calendar fallback is
    deliberately omitted: it could accept only a subset of the required crew
    at some instants. The production population has large shift-pure pools.
    """
    by_key = {}
    segment_cache = {}
    for employee_id in candidates:
        key = _calendar_key(operator_index, employee_id)
        if key not in segment_cache:
            try:
                _start, end, segments = productive_segments(
                    ready,
                    work_minutes,
                    lambda when, e=employee_id: operator_available_at(
                        operator_index, e, when
                    ),
                )
                segment_cache[key] = (end, tuple(segments))
            except NoAvailableCalendar:
                segment_cache[key] = None
        result = segment_cache[key]
        if result is not None:
            by_key.setdefault(key, []).append(employee_id)
    return by_key, segment_cache


def allocate_crew(
    operator_index: Mapping,
    optype: str,
    line_id,
    headcount: int,
    productive_ready: datetime,
    productive_minutes: float,
    reservations: Mapping | None,
    busy_minutes: Mapping | None = None,
    priority: float | None = None,
    state_is_normalized: bool = False,
):
    """Allocate one fixed crew and its common productive segments.

    Returns a dictionary with employees, common segments, elapsed completion,
    sourcing tier, updated reservation/load state, or a structured shortage.
    """
    headcount = int(headcount)
    productive_minutes = float(productive_minutes)
    if state_is_normalized:
        current_res = reservations if reservations is not None else {}
        current_busy = busy_minutes if busy_minutes is not None else reservation_busy_minutes(current_res)
    else:
        current_res = normalize_reservations(reservations)
        current_busy = dict(busy_minutes or reservation_busy_minutes(current_res))
    if headcount <= 0:
        return {
            "employees": [], "segments": (), "end": productive_ready,
            "tier": "unfilled", "shortage": None,
            "reservations": current_res, "busy_minutes": current_busy,
        }
    if not optype:
        return _shortage(headcount, productive_minutes, line_id, None,
                         "unresolved-machine-type", current_res, current_busy)

    optype = str(optype).strip().lower()
    line_id = int(line_id)
    line_pool = operator_index.get("by_line", {}).get((line_id, optype), [])
    factory_id = operator_index.get("line_factory", {}).get(line_id)
    factory_pool = (
        operator_index.get("by_factory", {}).get((factory_id, optype), [])
        if factory_id is not None else []
    )
    tiers = [("batch", line_pool)]
    if set(factory_pool) != set(line_pool):
        tiers.append(("factory", factory_pool))

    def success(selected, common_segments, tier):
        # This state belongs to this decoder run: normalize_reservations()
        # created fresh containers at evaluation entry. Update only the
        # selected employees instead of copying the whole workforce map for
        # every operation.
        for employee_id in selected:
            key = reservation_key(employee_id)
            current_res[key] = _merge_reserved_segments(
                current_res.get(key, ()), common_segments
            )
            current_busy[key] = current_busy.get(key, 0.0) + productive_minutes
        return {
            "employees": list(selected),
            "segments": tuple(common_segments),
            "end": common_segments[-1][1],
            "tier": tier,
            "shortage": None,
            "reservations": current_res,
            "busy_minutes": current_busy,
        }

    best_available = 0
    calendar_pool_cache = operator_index.setdefault(
        '_calendar_pool_cache', {}) if isinstance(operator_index, dict) else {}
    for tier, raw_pool in tiers:
        cache_key = (tier, line_id, factory_id, optype)
        immutable_groups = calendar_pool_cache.get(cache_key)
        if immutable_groups is None:
            immutable_groups = {}
            for employee_id in dict.fromkeys(raw_pool):
                immutable_groups.setdefault(
                    _calendar_key(operator_index, employee_id), []
                ).append(employee_id)
            calendar_pool_cache[cache_key] = immutable_groups

        def try_fixed_crew(pool):
            """Choose one crew, then walk its exact common availability.

            Existing reservations are temporary calendar exclusions. They
            must not split employees with the same recurring roster into
            separate cohorts or cause a false shortage: the operation waits
            until every selected member is simultaneously available.
            """
            nonlocal best_available
            unique = list(dict.fromkeys(pool))
            best_available = max(best_available, len(unique))
            if len(unique) < headcount:
                return None
            selected = _rank(
                unique, operator_index, optype, current_busy, priority
            )[:headcount]
            # Roster availability is identical for employees with the same
            # immutable calendar key. Probe it once per distinct roster and
            # probe reservations per employee; this preserves the exact crew
            # intersection while avoiding repeated rotation arithmetic.
            roster_representatives = {}
            for employee in selected:
                roster_representatives.setdefault(
                    _calendar_key(operator_index, employee), employee
                )
            selected_tuple = tuple(selected)
            representative_tuple = tuple(roster_representatives.values())

            def complete_crew_available(when):
                return (
                    all(
                        operator_available_at(operator_index, employee, when)
                        for employee in representative_tuple
                    )
                    and all(
                        _free_at(
                            current_res.get(reservation_key(employee), ()), when
                        )
                        for employee in selected_tuple
                    )
                )

            def next_reservation_change(when):
                boundaries = []
                for employee in selected_tuple:
                    for start, end in current_res.get(reservation_key(employee), ()):
                        if start > when:
                            boundaries.append(start)
                        if end > when:
                            boundaries.append(end)
                return min(boundaries) if boundaries else None

            try:
                _start, _end, segments = productive_segments(
                    productive_ready, productive_minutes,
                    complete_crew_available,
                    next_availability_change=next_reservation_change,
                )
            except NoAvailableCalendar:
                return None
            return selected, tuple(segments)

        # First prefer a crew sharing the exact immutable shift/rest roster.
        # This is the normal production case and is O(number of cohorts),
        # independent of how many prior reservation signatures exist.
        for calendar_key in sorted(immutable_groups, key=str):
            result = try_fixed_crew(immutable_groups[calendar_key])
            if result is not None:
                selected, common_segments = result
                return success(selected, common_segments, tier)

        # Several batch cohorts may share the same rotating shift but differ
        # only in weekly rest day. A fixed crew drawn from those cohorts is
        # valid; the common predicate naturally pauses on every member's rest
        # day. Grow deterministic compatible pools rather than enumerating
        # employee combinations. Opposite shifts are never mixed.
        compatible = {}
        for calendar_key, employees in immutable_groups.items():
            if calendar_key[0] == 'crew':
                compatibility_key = ('crew-rotation', calendar_key[1])
            else:
                compatibility_key = calendar_key
            compatible.setdefault(compatibility_key, []).extend(employees)
        for compatibility_key in sorted(compatible, key=str):
            result = try_fixed_crew(compatible[compatibility_key])
            if result is not None:
                selected, common_segments = result
                return success(selected, common_segments, tier)

        # Final exact fallback for deliberately mixed calendars (for example
        # overlapping 06:00-14:00 and 08:00-16:00 floater shifts). Test
        # deterministic ranked fixed crews; acceptance still requires the
        # complete selected crew to share every productive instant.
        ranked_pool = _rank(
            list(dict.fromkeys(raw_pool)), operator_index, optype,
            current_busy, priority,
        )
        for offset in range(max(0, len(ranked_pool) - headcount + 1)):
            result = try_fixed_crew(ranked_pool[offset:offset + headcount])
            if result is not None:
                selected, common_segments = result
                return success(selected, common_segments, tier)

    skilled = operator_index.get("by_skill", {}).get(optype, [])
    if not skilled:
        reason = "skill"
    elif factory_id is None or not factory_pool:
        reason = "factory"
    elif best_available == 0:
        reason = "calendar-or-reservation"
    else:
        reason = "crew-intersection-or-reservation"
    return _shortage(
        headcount, productive_minutes, line_id, optype, reason,
        current_res, current_busy, available=best_available,
    )


def _shortage(required, minutes, line_id, optype, reason,
              reservations, busy_minutes, available=0):
    return {
        "employees": [],
        "segments": (),
        "end": None,
        "tier": "unfilled",
        "shortage": {
            "required": int(required),
            "available": int(available),
            "shortfall": int(required),
            "duration_minutes": float(minutes),
            "shortfall_worker_minutes": int(required) * float(minutes),
            "limiting_factor": reason,
            "operation_type": optype,
            "line_id": int(line_id),
        },
        "reservations": reservations,
        "busy_minutes": busy_minutes,
    }
