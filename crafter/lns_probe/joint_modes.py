"""Authoritative, immutable executable modes for CRAFT E1.7."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from bisect import bisect_right
import math

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds, authoritative_operation_work_minutes,
    authoritative_duration_contract, authoritative_setup_seconds, walk_fixed_crew,
)
from utils.work_calendar import NoAvailableCalendar
from ga.worker_allocation import allocate_crew, normalize_reservations, operator_available_at

from .anchored_model import outside_machine_reservations, outside_worker_reservations
from .line_k_model import _available_machine_pool, _candidate_starts
from .transaction import _machine_accepts


@dataclass(frozen=True)
class JointExecutableMode:
    mode_id: str
    operation_key: str
    factory_id: int
    line_id: int
    machine_count: int
    machine_ids: tuple[int, ...]
    worker_ids: tuple[str, ...]
    start_seconds: int
    productive_start_seconds: int
    setup_seconds: int
    productive_seconds: int
    productive_minutes: float
    end_seconds: int
    elapsed_seconds: int
    pause_seconds: int
    worker_segments: tuple
    incoming_boundary: tuple
    outgoing_boundary: tuple
    provenance: tuple
    incumbent: bool = False
    machine_occupancy_start_seconds: int = 0
    machine_occupancy_end_seconds: int = 0
    recovery_level: str = "INCUMBENT_PRESERVATION"
    recovery_reason: str = "INCUMBENT_FALLBACK"
    source_event_id: str | None = None

    def as_dict(self):
        return asdict(self)


def _segments_from_row(problem, row):
    workers = tuple(str(v) for v in row.get("employee_ids", ()))
    mapping = row.get("worker_segments_by_employee", {})
    if workers and mapping:
        rep = mapping.get(workers[0], mapping.get(str(workers[0]), ()))
        out = []
        for item in rep:
            if isinstance(item, dict):
                out.append((datetime.fromisoformat(str(item["start"])),
                            datetime.fromisoformat(str(item["end"]))))
            else:
                out.append((datetime.fromisoformat(str(item[0])),
                            datetime.fromisoformat(str(item[1]))))
        return tuple(out)
    return ()


def incumbent_prefix_worker_reservations(problem, index, operation_key):
    """Return the CR decoder's reservation state immediately before ``operation_key``.

    The validated CR phenotype was produced by the sequential authoritative
    decoder.  Its fixed crew walk saw reservations from operations already
    decoded, plus fixed commitments; it did not see future operations.  A
    neighborhood's *outside* reservation view is correct for alternatives,
    but it is insufficient to reconstruct an incumbent operation whose
    predecessor operations are inside the neighborhood.  Keeping this helper
    local to E1.7 makes that distinction explicit without changing the
    production allocator or the frozen phenotype.
    """
    from collections import defaultdict

    result = defaultdict(list)
    for row in index.rows:
        if str(row["operation_key"]) == str(operation_key):
            break
        for employee, segments in row.get("worker_segments_by_employee", {}).items():
            for pair in segments:
                start, end = pair if not isinstance(pair, dict) else (
                    pair["start"], pair["end"])
                result[str(employee)].append((
                    datetime.fromisoformat(str(start)),
                    datetime.fromisoformat(str(end))))
    for fixed in problem.resources.fixed_worker_commitments:
        result[str(fixed.employee_id)].append((
            problem.origin + timedelta(seconds=int(fixed.start_seconds)),
            problem.origin + timedelta(seconds=int(fixed.end_seconds))))
    for intervals in result.values():
        intervals.sort()
    return result


def _fixed_crew_execution(problem, operator_index, workers, ready, productive,
                          reservations, *, availability_cache=None):
    workers = tuple(str(v) for v in workers)
    def available(when):
        key = ('available', workers, when)
        if availability_cache is not None and key in availability_cache:
            return availability_cache[key]
        value = all(operator_available_at(
            operator_index, int(employee) if str(employee).isdigit() else employee,
            when) and all(not (left <= when < right)
                          for left, right in reservations.get(str(employee), ()))
                    for employee in workers)
        if availability_cache is not None:
            if len(availability_cache) >= 65536:
                availability_cache.clear()  # eviction only; never truncates work
            availability_cache[key] = value
        return value
    def next_change(when):
        # The authoritative walk asks for the next reservation transition at
        # every shift/probe. Build the exact transition set once per crew and
        # immutable reservation signature (the caller owns cache invalidation),
        # then find the first strictly future endpoint. This changes neither
        # the half-open reservation rule nor the calendar walk itself.
        key = ('change_points', workers)
        points = availability_cache.get(key) if availability_cache is not None else None
        if points is None:
            points = tuple(sorted({edge for employee in workers
                                   for interval in reservations.get(str(employee), ())
                                   for edge in interval}))
            if availability_cache is not None:
                if len(availability_cache) >= 65536:
                    availability_cache.clear()
                availability_cache[key] = points
        index = bisect_right(points, when)
        return points[index] if index < len(points) else None
    _start, end, segments = walk_fixed_crew(
        ready, productive, available, next_availability_change=next_change)
    return end, tuple(segments)


def _event_recovery_metadata(event, *, incumbent, incumbent_line, incumbent_k,
                             incumbent_machines, incumbent_workers, line_id, k,
                             machine_ids, workers, start):
    if event is None:
        return ("INCUMBENT_PRESERVATION" if incumbent else "BOUNDED_CRAFT_REPAIR",
                "INCUMBENT_FALLBACK" if incumbent else "EXECUTABLE_ALTERNATIVE")
    if incumbent:
        return "INCUMBENT_PRESERVATION", "INCUMBENT_FALLBACK"
    if getattr(event, "machine_ids", ()):
        if line_id != incumbent_line:
            return "CROSS_LINE_OPERATION_MOVE", "MACHINE_BREAKDOWN_CROSS_LINE"
        if k < incumbent_k:
            return "SAME_LINE_CAPACITY_ADJUSTMENT", "MACHINE_BREAKDOWN_K_REDUCTION"
        if tuple(machine_ids) != tuple(incumbent_machines):
            return "SAME_LINE_RESOURCE_SUBSTITUTION", "MACHINE_BREAKDOWN_MACHINE_SUBSTITUTION"
        if start >= int(getattr(event, "end_time_seconds", 0)):
            return "TEMPORAL_SHIFT", "MACHINE_BREAKDOWN_WAIT_FOR_REPAIR"
    if getattr(event, "worker_ids", ()):
        if k < incumbent_k:
            return "SAME_LINE_CAPACITY_ADJUSTMENT", "WORKER_ABSENCE_K_REDUCTION"
        if tuple(workers) != tuple(incumbent_workers):
            return "SAME_LINE_RESOURCE_SUBSTITUTION", "WORKER_ABSENCE_WORKER_SUBSTITUTION"
        if start >= int(getattr(event, "end_time_seconds", 0)):
            return "TEMPORAL_SHIFT", "WORKER_ABSENCE_WAIT_FOR_RETURN"
    if getattr(event, "metadata", {}).get("predecessor_operation_key"):
        return "TEMPORAL_SHIFT", "PREDECESSOR_DELAY_RIGHT_SHIFT"
    return "BOUNDED_CRAFT_REPAIR", "EVENT_EXECUTABLE_ALTERNATIVE"


def generate_joint_modes(problem, op_details, operator_index, index,
                         neighborhood, released_keys, candidate_domains,
                         exact_machine_sets, *, max_starts=10, event=None):
    """Generate only individually executable modes against frozen state."""
    mutable = set(neighborhood.operation_keys)
    released = set(released_keys)
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = normalize_reservations(outside_worker_reservations(problem, index, mutable))
    if event is not None and getattr(event, "worker_ids", ()):
        # Treat absence as an event-time unavailability interval during
        # alternative crew allocation.  Otherwise the allocator can select
        # the absent worker and every candidate is rejected only after the
        # calendar walk.  Incumbent prefix replay remains unchanged; the
        # event guard still excludes an event-invalid incumbent mode.
        event_start = problem.origin + timedelta(seconds=int(getattr(event, "event_time_seconds", 0)))
        event_end = problem.origin + timedelta(seconds=int(getattr(event, "end_time_seconds", getattr(event, "event_time_seconds", 0))))
        for employee in getattr(event, "worker_ids", ()):
            worker_res.setdefault(str(employee), []).append((event_start, event_end))
        for intervals in worker_res.values():
            intervals.sort()
    modes_by_key, failures = {}, {}
    raw_counts = {}
    event_mode_end_bounds = {}
    event_resource_end_bounds = {}
    event_route_keys = set()
    if event is not None:
        delayed_key = str(getattr(event, "metadata", {}).get("predecessor_operation_key", ""))
        cursor = delayed_key
        while cursor and cursor in mutable and cursor not in event_route_keys:
            event_route_keys.add(cursor)
            cursor = index.route_successor.get(cursor)
    if event is not None:
        # Generate route predecessors before successors so an event-driven
        # resource substitution/wait can publish its actual executable end
        # as the successor's lower bound.  This is essential when a worker
        # absence causes calendar pauses and therefore extends the envelope
        # beyond the event's repair time.
        ordered_keys = sorted(mutable, key=lambda value: (
            str(index.by_key[value].get("production_order_id", "")),
            int(index.by_key[value].get("sequence", 0)), str(value)))
    else:
        ordered_keys = sorted(mutable)
    for key in ordered_keys:
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        factory_id = int(index.operation_features[key]["factory_id"])
        incumbent_line = int(row["line_id"])
        incumbent_machines = tuple(sorted(int(v) for v in row["machine_ids"]))
        incumbent_k = len(incumbent_machines)
        if key in released:
            mode_pairs = []
            for item in candidate_domains.get(key, ()):
                pair = (int(item["line_id"]), int(item["k"]))
                if pair not in mode_pairs: mode_pairs.append(pair)
        else:
            mode_pairs = [(incumbent_line, incumbent_k)]
        generated = []
        rejected = []
        for line_id, k in mode_pairs:
            if key in released:
                sets = exact_machine_sets.get(key, {}).get((line_id, k), ())
            else:
                sets = (incumbent_machines,)
            if not sets:
                rejected.append({"reason": "NO_MACHINE_SET", "line_id": line_id, "k": k}); continue
            for set_rank, machine_ids in enumerate(sets):
                machine_ids = tuple(sorted(int(v) for v in machine_ids))
                if len(machine_ids) != k:
                    rejected.append({"reason": "NO_MACHINE_SET", "line_id": line_id, "k": k}); continue
                pool = tuple(sorted(set(machine_ids)))
                starts = _candidate_starts(problem, index, op_details, key, mutable,
                                           pool, machine_res, worker_res, maximum=max_starts)
                if event is not None:
                    event_resource = bool(set(machine_ids).intersection(getattr(event, "machine_ids", ()))) or bool(getattr(event, "worker_ids", ()))
                    delayed_end = int(getattr(event, "metadata", {}).get("delayed_end_seconds", 0))
                    if event_resource and int(getattr(event, "end_time_seconds", 0)) > 0:
                        starts = tuple(sorted(set(starts) | {int(event.end_time_seconds)}))
                    if delayed_end and key != str(getattr(event, "metadata", {}).get("predecessor_operation_key", "")):
                        starts = tuple(sorted(set(starts) | {delayed_end}))
                    predecessor_key = index.route_predecessor.get(key)
                    if predecessor_key in event_mode_end_bounds:
                        starts = tuple(sorted(set(starts) | {int(event_mode_end_bounds[predecessor_key])}))
                    # A delayed route chain can collide with an unrelated
                    # operation that shares its physical machine/worker.  Add
                    # the actual chain envelope endpoints as candidate starts
                    # for that resource-conflicting operation so local
                    # resequencing is represented before CP, rather than being
                    # frozen out by the old incumbent boundary.
                    for resource_id in pool:
                        starts = tuple(sorted(set(starts) | set(event_resource_end_bounds.get(("machine", int(resource_id)), ()))))
                    # Worker occupancy is represented by authoritative
                    # productive segments, not continuous machine-held
                    # intervals.  Publish route worker end envelopes so a
                    # concrete worker-conflicting future operation can be
                    # right-shifted without changing worker semantics.
                    for worker_id in row.get("employee_ids", ()):
                        starts = tuple(sorted(set(starts) | set(
                            event_resource_end_bounds.get(("worker", str(worker_id)), ()))))
                    explicit_bounds = getattr(event, "metadata", {}).get("successor_start_bounds", {})
                    if str(key) in explicit_bounds:
                        starts = tuple(sorted(set(starts) | {int(explicit_bounds[str(key)])}))
                setup = authoritative_setup_seconds(op_details[key])
                duration = authoritative_duration_contract(op_details[key], k)
                productive_minutes = duration.productive_minutes
                productive_seconds = duration.productive_seconds
                for start in starts:
                    productive_start = int(start + setup)
                    ready = problem.origin + timedelta(seconds=productive_start)
                    is_incumbent = (line_id == incumbent_line and k == incumbent_k and
                                    machine_ids == incumbent_machines and
                                    start == int(round(float(row["start_minutes"])*60.0)))
                    # Keep the frozen external reservation universe immutable.
                    # allocate_crew mutates its normalized state by adding the
                    # selected crew; that temporary state must not be fed back
                    # into the authoritative replay walk.
                    # Alternatives are screened against frozen external
                    # reservations.  The incumbent, however, must reproduce
                    # the already validated CR execution exactly: CR's
                    # sequential decoder saw its prefix reservation state,
                    # including earlier operations that may be inside this
                    # local neighborhood.  Do not replace this with a copy of
                    # the stored segments; replay the authoritative walk.
                    replay_reservations = (
                        incumbent_prefix_worker_reservations(problem, index, key)
                        if is_incumbent else worker_res)
                    reservations = normalize_reservations({
                        str(e): list(v) for e, v in replay_reservations.items()})
                    if is_incumbent:
                        workers = tuple(str(v) for v in row.get("employee_ids", ()))
                    elif key in released:
                        allocation_reservations = normalize_reservations({str(e): list(v) for e,v in worker_res.items()})
                        result = allocate_crew(operator_index, operation.required_capability,
                                               line_id, k, ready, productive_minutes,
                                               allocation_reservations, None, priority=0.5,
                                               state_is_normalized=True)
                        if result.get("shortage") is not None:
                            rejected.append({"reason": "NO_CREW", "line_id": line_id, "k": k,
                                             "start_seconds": start, "detail": result.get("shortage")}); continue
                        workers = tuple(str(v) for v in result["employees"])
                    else:
                        workers = tuple(str(v) for v in row.get("employee_ids", ()))
                        if len(workers) != k:
                            rejected.append({"reason": "NO_CREW", "line_id": line_id, "k": k}); continue
                    try:
                        end_dt, segments = _fixed_crew_execution(
                            problem, operator_index, workers, ready, productive_minutes, reservations)
                    except NoAvailableCalendar as exc:
                        rejected.append({"reason": "CALENDAR_INFEASIBLE", "detail": repr(exc)}); continue
                    end = int(round((end_dt-problem.origin).total_seconds()))
                    if event is not None:
                        event_start = int(getattr(event, "event_time_seconds", 0))
                        event_end = int(getattr(event, "end_time_seconds", event_start))
                        interval_overlap = start < event_end and end > event_start
                        blocked_machines = set(getattr(event, "machine_ids", ()))
                        if interval_overlap and blocked_machines.intersection(machine_ids):
                            rejected.append({"reason": "EVENT_MACHINE_UNAVAILABLE", "line_id": line_id,
                                             "k": k, "start_seconds": start, "end_seconds": end})
                            continue
                        blocked_workers = {str(x) for x in getattr(event, "worker_ids", ())}
                        if blocked_workers.intersection(str(x) for x in workers):
                            if any(a.timestamp() < problem.origin.timestamp() + event_end and
                                   b.timestamp() > problem.origin.timestamp() + event_start
                                   for a, b in segments):
                                rejected.append({"reason": "EVENT_WORKER_UNAVAILABLE", "line_id": line_id,
                                                 "k": k, "start_seconds": start, "end_seconds": end})
                                continue
                        delayed_end = int(getattr(event, "metadata", {}).get("delayed_end_seconds", 0))
                        delayed_key = str(getattr(event, "metadata", {}).get("predecessor_operation_key", ""))
                        if delayed_end and key != delayed_key:
                            predecessor_of_key = index.route_predecessor.get(key)
                            if delayed_key in (predecessor_of_key,):
                                if start < delayed_end:
                                    rejected.append({"reason": "EVENT_PREDECESSOR_DELAY", "start_seconds": start,
                                                     "required_start_seconds": delayed_end,
                                                     "candidate_starts": list(starts)})
                                    continue
                    if not all(_machine_accepts(machine_res.get(mid, ()), start, end, operation.order_id)
                               for mid in machine_ids):
                        rejected.append({"reason": "FROZEN_MACHINE_BOUNDARY", "line_id": line_id,
                                         "k": k, "start_seconds": start}); continue
                    predecessor = index.route_predecessor.get(key)
                    successor = index.route_successor.get(key)
                    # Boundary timestamps in the frozen phenotype are decimal
                    # minutes.  Convert them through the authoritative
                    # second projection (round-to-nearest) rather than a raw
                    # ``int(float * 60)`` truncation.  Values such as
                    # ``2100.7 * 60`` are represented as 126041.999999...
                    # and truncating that value incorrectly rejects the
                    # already validated incumbent at an exact successor
                    # boundary.
                    predecessor_end = (int(round(float(index.by_key[predecessor]["end_minutes"]) * 60.0))
                                        if predecessor else None)
                    successor_start = (int(round(float(index.by_key[successor]["start_minutes"]) * 60.0))
                                       if successor else None)
                    if predecessor and predecessor not in mutable and start < predecessor_end:
                        rejected.append({"reason": "ROUTING_BOUNDARY", "side": "predecessor"}); continue
                    if successor and successor not in mutable and end > successor_start:
                        rejected.append({"reason": "ROUTING_BOUNDARY", "side": "successor"}); continue
                    pause = max(0, end - productive_start - productive_seconds)
                    incumbent = is_incumbent
                    recovery_level, recovery_reason = _event_recovery_metadata(
                        event, incumbent=incumbent, incumbent_line=incumbent_line,
                        incumbent_k=incumbent_k, incumbent_machines=incumbent_machines,
                        incumbent_workers=tuple(str(v) for v in row.get("employee_ids", ())),
                        line_id=line_id, k=k, machine_ids=machine_ids,
                        workers=workers, start=start)
                    mode_id = f"{key}|{line_id}|{k}|{','.join(map(str,machine_ids))}|{start}|{len(generated)}"
                    generated.append(JointExecutableMode(
                        mode_id, key, factory_id, line_id, k, machine_ids, workers,
                        int(start), productive_start, setup, productive_seconds,
                        float(productive_minutes), end, end-int(start), pause,
                        tuple(segments), (), (),
                        ("incumbent" if incumbent else "screened", set_rank), incumbent,
                        int(start), int(end),
                        recovery_level=recovery_level,
                        recovery_reason=recovery_reason,
                        source_event_id=(getattr(event, "event_id", None) if event is not None else None)))
        # Deduplicate complete modes and retain a bounded deterministic set.
        unique = {}
        for mode in generated:
            fingerprint = (mode.line_id, mode.machine_count, mode.machine_ids,
                           mode.worker_ids, mode.start_seconds, mode.end_seconds,
                           tuple((a.isoformat(), b.isoformat()) for a,b in mode.worker_segments))
            unique[fingerprint] = mode
        generated = sorted(unique.values(), key=lambda mode: (not mode.incumbent,
                                                               mode.start_seconds, mode.line_id,
                                                               mode.machine_ids, mode.worker_ids))
        if not generated:
            # Preserve the complete rejection evidence for event diagnostics.
            # Truncating here hid the decisive post-delay boundary rejection
            # behind the first few predecessor-delay candidates.
            raise ValueError(f"E1_7 zero executable modes for {key}: {rejected}")
        modes_by_key[key] = tuple(generated)
        failures[key] = rejected
        raw_counts[key] = {"generated": len(generated)+len(rejected), "accepted": len(generated),
                           "rejected": len(rejected)}
        if event is not None:
            # Only event-affected predecessors publish a dynamic boundary;
            # ordinary frozen routing continues to use its authoritative
            # incumbent/frozen boundary checks.
            event_machine_ids = set(getattr(event, "machine_ids", ()))
            event_worker_ids = {str(value) for value in getattr(event, "worker_ids", ())}
            row_workers = {str(value) for value in row.get("employee_ids", ())}
            row_machines = {int(value) for value in row.get("machine_ids", ())}
            target_key = str(getattr(event, "metadata", {}).get("target_operation_key", ""))
            if (key == target_key or event_machine_ids.intersection(row_machines)
                    or event_worker_ids.intersection(row_workers)
                    or index.route_predecessor.get(key) in event_mode_end_bounds
                    or key in released):
                event_mode_end_bounds[key] = max(int(mode.end_seconds) for mode in generated)
                delayed_key = str(getattr(event, "metadata", {}).get("predecessor_operation_key", ""))
                if key == delayed_key:
                    event_mode_end_bounds[key] = max(event_mode_end_bounds[key],
                                                     int(getattr(event, "metadata", {}).get("delayed_end_seconds", 0)))
            if key in event_route_keys:
                for mode in generated:
                    for resource_id in mode.machine_ids:
                        event_resource_end_bounds.setdefault(("machine", int(resource_id)), set()).add(int(mode.end_seconds))
                    for worker_id in mode.worker_ids:
                        event_resource_end_bounds.setdefault(("worker", str(worker_id)), set()).add(int(mode.end_seconds))
    return modes_by_key, failures, raw_counts
