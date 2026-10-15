"""Crafter-safe access to the existing authoritative execution semantics.

The production implementations remain unchanged.  These pure wrappers make
Crafter consume those implementations instead of maintaining another duration
or transition formula.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from ga.fitness import CHANGEOVER_MINUTES
from ga.schedule_utils import split_operation_work
from utils.work_calendar import productive_segments


@dataclass(frozen=True)
class ProjectedSeconds:
    source_seconds: float
    projected_seconds: int
    error_seconds: float
    snapped_to_integer: bool


@dataclass(frozen=True)
class AuthoritativeDuration:
    """The single duration contract consumed by Crafter execution modes.

    ``productive_minutes`` is retained at the decoder's native precision.
    ``productive_seconds`` is the calendar-work requirement: the exact
    ``ceil(minutes * 60)`` projection performed by ``productive_segments``.
    The CR JSON stores the former, not a separately rounded integer; treating
    ``round(productive_minutes * 60)`` as a second contract was the source of
    the one-second incumbent-audit false positives.
    """
    setup_minutes: float
    productive_minutes: float
    productive_seconds: int


def authoritative_duration_contract(op_detail: dict, machine_count: int) -> AuthoritativeDuration:
    setup_minutes, productive_minutes = authoritative_operation_work_minutes(
        op_detail, machine_count)
    return AuthoritativeDuration(
        float(setup_minutes),
        float(productive_minutes),
        int(math.ceil(float(productive_minutes) * 60.0)),
    )


def project_instant_seconds(value: float, *, snap_tolerance: float = 1e-6) -> ProjectedSeconds:
    """Project a relative instant upward to V2.1's integer-second grid.

    Values within floating serialization noise of an integer are snapped to
    that integer.  All other instants use ceil, so a release, commitment end,
    or realized completion is never moved earlier.
    """
    source = float(value)
    if not math.isfinite(source):
        raise ValueError("time must be finite")
    nearest = int(round(source))
    snapped = abs(source-nearest) <= float(snap_tolerance)
    projected = nearest if snapped else int(math.ceil(source))
    return ProjectedSeconds(source, projected, projected-source, snapped)


def project_duration_seconds(value: float) -> ProjectedSeconds:
    """Project positive elapsed/work duration exactly as the calendar walk.

    ``productive_segments`` uses ``math.ceil(minutes * 60)``.  This wrapper
    applies that same upward rule to seconds and is used for setup, productive
    work, and fixed-phenotype duration evidence.
    """
    source = float(value)
    if not math.isfinite(source) or source < 0:
        raise ValueError("duration must be finite and non-negative")
    projected = int(math.ceil(source))
    return ProjectedSeconds(
        source, projected, projected-source,
        abs(source-projected) <= 1e-12,
    )


def authoritative_operation_work_minutes(op_detail: dict, machine_count: int) -> tuple[float, float]:
    """Return authoritative ``(setup elapsed, productive work)`` minutes."""
    return split_operation_work(op_detail, machine_count)


def authoritative_setup_seconds(op_detail: dict) -> int:
    setup_minutes, _productive = authoritative_operation_work_minutes(op_detail, 1)
    return project_duration_seconds(setup_minutes*60.0).projected_seconds


def authoritative_productive_seconds(op_detail: dict, machine_count: int) -> int:
    """Whole seconds consumed by the authoritative worker calendar walk."""
    return authoritative_duration_contract(op_detail, machine_count).productive_seconds


def calendar_minutes_from_integer_seconds(seconds: int) -> float:
    """Encode already-approved integer work for the legacy minute calendar API.

    This is NOT duration rounding: the source execution contract already rounded
    quantity/SMV/efficiency work to integer seconds. The encoding must satisfy
    ceil(minutes * 60) == seconds exactly, avoiding a second floating-point ceil.
    Native-minute legacy durations deliberately do not use this adapter.
    """
    if type(seconds) is not int or not 0 < seconds <= 2**52:
        raise ValueError('INTEGER_WORK_SECONDS_OUT_OF_RANGE')
    minutes = seconds / 60.0
    for _ in range(4):
        projected = math.ceil(minutes * 60.0)
        if projected == seconds:
            return minutes
        minutes = math.nextafter(minutes, -math.inf if projected > seconds else math.inf)
    raise ValueError('INTEGER_WORK_SECONDS_NOT_REPRESENTABLE')


def authoritative_changeover_seconds(previous_order_id, next_order_id,
                                     *, changeover_minutes: float = CHANGEOVER_MINUTES) -> int:
    """Per-machine order-switch penalty used by select_machines_and_start.

    The authoritative selector defaults an unknown prior identity to the next
    order, so first use has no penalty.  Same-order reuse also has no penalty.
    """
    if previous_order_id is None or str(previous_order_id) == str(next_order_id):
        return 0
    return project_duration_seconds(
        float(changeover_minutes)*60.0).projected_seconds


def machine_hold_seconds(start_seconds: int, end_seconds: int) -> tuple[int, int]:
    """Authoritative machines are held continuously across worker pauses."""
    start, end = int(start_seconds), int(end_seconds)
    if end < start:
        raise ValueError("machine hold end precedes start")
    return start, end


def walk_fixed_crew(ready, productive_minutes, is_complete_crew_available,
                    *, next_availability_change=None):
    """Delegate pause/resume to the one authoritative calendar primitive."""
    return productive_segments(
        ready, productive_minutes, is_complete_crew_available,
        next_availability_change=next_availability_change,
    )


SEMANTIC_SOURCE_REFERENCES = {
    "serial_work": "ga/scheduler.py:GAScheduler.__init__ serial_processing_mins",
    "machine_count_scaling": "ga/schedule_utils.py:split_operation_work",
    "calendar_progress": "utils/work_calendar.py:productive_segments",
    "crew_continuity": "ga/worker_allocation.py:allocate_crew",
    "machine_hold": "ga/fitness.py:evaluate_schedule",
    "changeover": "ga/schedule_utils.py:select_machines_and_start",
    "changeover_value": "ga/fitness.py:CHANGEOVER_MINUTES",
}
