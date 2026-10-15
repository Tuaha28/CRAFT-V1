"""Schedule freeze-zone decisions."""
from __future__ import annotations

from dataclasses import dataclass

from .execution_state import ExecutionState


@dataclass(frozen=True)
class FreezeConfig:
    near_future_seconds: int = 3600
    source: str = "CRAFT_ENGINEERING_POLICY"
    factory_confirmation_required: bool = True


def freeze_decision(classification: dict, *, explicitly_released: bool = False) -> dict:
    state = classification["state"]
    if state == ExecutionState.COMPLETED.value:
        return {"movable": False, "reason": "COMPLETED_IMMUTABLE", "penalty": "infinite"}
    if state == ExecutionState.IN_PROGRESS.value:
        return {"movable": False, "reason": "IN_PROGRESS_WIP_GUARD", "penalty": "very_high"}
    if state == ExecutionState.FROZEN_NEAR_TERM.value and not explicitly_released:
        return {"movable": False, "reason": "NEAR_TERM_FREEZE", "penalty": "high"}
    return {"movable": True, "reason": "POLICY_RELEASED_FUTURE_WORK", "penalty": "measured"}
