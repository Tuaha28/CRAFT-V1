"""Conservative event-relative operation execution classification."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from crafter.lns_probe.analysis import seconds


class ExecutionState(str, Enum):
    COMPLETED = "COMPLETED"
    IN_PROGRESS = "IN_PROGRESS"
    FROZEN_NEAR_TERM = "FROZEN_NEAR_TERM"
    FUTURE_RELEASED = "FUTURE_RELEASED"
    FUTURE_PLANNED = "FUTURE_PLANNED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class FreezeParameters:
    """Configurable engineering policy; not a Needle Craft SOP assertion."""

    near_future_seconds: int = 3600
    source: str = "CRAFT_ENGINEERING_POLICY"
    factory_confirmation_required: bool = True


def classify_operation(row: dict, event_time_seconds: int, *,
                       parameters: FreezeParameters | None = None,
                       blocked: bool = False) -> dict:
    parameters = parameters or FreezeParameters()
    start = seconds(row["start_minutes"])
    end = seconds(row["end_minutes"])
    if blocked:
        state = ExecutionState.BLOCKED
    elif end <= int(event_time_seconds):
        state = ExecutionState.COMPLETED
    elif start < int(event_time_seconds) < end:
        state = ExecutionState.IN_PROGRESS
    elif start <= int(event_time_seconds) + parameters.near_future_seconds:
        state = ExecutionState.FROZEN_NEAR_TERM
    else:
        release = int(row.get("release_seconds", 0) or 0)
        state = (ExecutionState.FUTURE_RELEASED
                 if release <= int(event_time_seconds)
                 else ExecutionState.FUTURE_PLANNED)
    return {
        "operation_key": str(row["operation_key"]),
        "state": state.value,
        "event_time_seconds": int(event_time_seconds),
        "start_seconds": int(start), "end_seconds": int(end),
        "completed": state == ExecutionState.COMPLETED,
        "cp_may_move": state in {ExecutionState.FUTURE_RELEASED, ExecutionState.FUTURE_PLANNED},
        "in_progress_wip_guard": state == ExecutionState.IN_PROGRESS,
        "freeze_policy_source": parameters.source,
    }


def classify_operations(rows: Iterable[dict], event_time_seconds: int, *,
                        parameters: FreezeParameters | None = None,
                        blocked_operations: Iterable[str] = ()) -> list[dict]:
    blocked = {str(x) for x in blocked_operations}
    return [classify_operation(row, event_time_seconds, parameters=parameters,
                               blocked=str(row["operation_key"]) in blocked)
            for row in sorted(rows, key=lambda item: str(item["operation_key"]))]
