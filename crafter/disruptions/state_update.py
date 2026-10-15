"""In-memory disruption state updates.

Only future resource eligibility is changed here.  The authoritative source
problem and the database remain untouched.  Historical assignments are kept
for audit/replay; event-specific legality is checked separately.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Iterable

from crafter.contracts import SchedulingProblem

from .events import DisruptionEvent, EventType


@dataclass(frozen=True)
class ShadowState:
    problem: SchedulingProblem
    allocation_index: dict
    blocked_machine_ids: tuple[int, ...] = ()
    absent_worker_ids: tuple[str, ...] = ()
    database_writes: int = 0


def _filter_worker_pools(operator_index: dict, absent: Iterable[str]) -> dict:
    """Remove absent workers from allocator pools but retain calendar maps.

    ``operator_available_at`` uses ``crew_of``/``shift_of``.  Keeping those
    maps lets already completed incumbent modes replay, while removing the
    workers from by-skill/by-factory/by-line pools prevents new allocation.
    """
    absent = {str(x) for x in absent}
    output = deepcopy(operator_index)
    for field in ("by_skill", "by_factory", "by_line"):
        mapping = output.get(field, {})
        output[field] = {
            key: [x for x in values if str(x) not in absent]
            for key, values in mapping.items()
        }
    return output


def apply_event(problem: SchedulingProblem, operator_index: dict,
                event: DisruptionEvent) -> ShadowState:
    blocked = set(event.machine_ids)
    absent = set(str(x) for x in event.worker_ids)
    if event.event_type in (EventType.MACHINE_BREAKDOWN.value,
                            EventType.MULTIPLE_MACHINE_BREAKDOWN.value,
                            EventType.RESOURCE_CONGESTION.value,
                            EventType.COMBINED.value):
        machines = tuple(
            replace(machine, is_usable=False) if machine.machine_id in blocked else machine
            for machine in problem.machines)
        shadow_problem = replace(problem, machines=machines)
    else:
        shadow_problem = problem
    shadow_index = _filter_worker_pools(operator_index, absent) if absent else deepcopy(operator_index)
    return ShadowState(shadow_problem, shadow_index, tuple(sorted(blocked)),
                       tuple(sorted(absent)), 0)


def event_conflicts(event: DisruptionEvent, phenotype, problem) -> list[dict]:
    """Return event-specific hard conflicts in a reconstructed phenotype."""
    conflicts = []
    for item in phenotype.operations:
        assignment = item.assignment
        start, end = int(assignment.start_seconds), int(assignment.end_seconds)
        overlaps = start < event.end_time_seconds and end > event.event_time_seconds
        if not overlaps:
            continue
        if event.machine_ids and set(assignment.machine_ids) & set(event.machine_ids):
            conflicts.append({"operation_key": assignment.operation_key,
                              "kind": "MACHINE_UNAVAILABLE",
                              "machine_ids": sorted(set(assignment.machine_ids) & set(event.machine_ids))})
        if event.worker_ids and set(item.employee_ids) & set(event.worker_ids):
            blocked = set(item.employee_ids) & set(event.worker_ids)
            # Workers are occupied on authoritative productive segments, not
            # continuously through machine-held calendar pauses.
            segment_map = {str(employee): segments for employee, segments in item.worker_segments}
            segment_conflict = False
            for employee in blocked:
                segments = segment_map.get(str(employee), ())
                if not segments:
                    segment_conflict = True
                for left, right in segments:
                    left_seconds = int(round((left - problem.origin).total_seconds()))
                    right_seconds = int(round((right - problem.origin).total_seconds()))
                    if left_seconds < event.end_time_seconds and right_seconds > event.event_time_seconds:
                        segment_conflict = True
            if segment_conflict:
                conflicts.append({"operation_key": assignment.operation_key,
                                  "kind": "WORKER_UNAVAILABLE",
                                  "worker_ids": sorted(blocked)})
    if event.event_type == EventType.PREDECESSOR_DELAY.value:
        delayed = str(event.metadata.get("predecessor_operation_key", ""))
        delayed_end = int(event.metadata.get("delayed_end_seconds", 0))
        by_key = {x.assignment.operation_key: x for x in phenotype.operations}
        predecessor = by_key.get(delayed)
        if predecessor is not None:
            for item in phenotype.operations:
                if item.assignment.order_id == predecessor.assignment.order_id:
                    if int(item.assignment.start_seconds) < delayed_end and item.assignment.operation_key != delayed:
                        # Sequence filtering is performed by the caller; this
                        # conservative record is refined there when possible.
                        conflicts.append({"operation_key": item.assignment.operation_key,
                                          "kind": "PREDECESSOR_DELAY"})
    return conflicts
