"""Single authoritative runtime/search configuration for CRAFT V1 Fast."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CRAFTFastModeConfig:
    enabled: bool = False
    normal_solve_limit_seconds: float = 60.0
    adaptive_solve_limit_seconds: float = 90.0
    max_adaptive_rounds: int = 2
    fast_evidence: bool = False
    window_safety_ceiling_seconds: float = 180.0


def fast_mode_config() -> CRAFTFastModeConfig:
    return CRAFTFastModeConfig(
        enabled=os.environ.get("CRAFT_FAST_MODE", "0") == "1",
        normal_solve_limit_seconds=float(os.environ.get("CRAFT_NORMAL_SOLVE_LIMIT_SECONDS", "60")),
        adaptive_solve_limit_seconds=float(os.environ.get("CRAFT_ADAPTIVE_SOLVE_LIMIT_SECONDS", "90")),
        max_adaptive_rounds=int(os.environ.get("CRAFT_MAX_ADAPTIVE_ROUNDS", "2")),
        fast_evidence=os.environ.get("CRAFT_FAST_EVIDENCE", "0") == "1",
        window_safety_ceiling_seconds=float(os.environ.get("CRAFT_WINDOW_SAFETY_CEILING_SECONDS", "180")),
    )


def fast_search_plan(*, adaptive: bool = False):
    config = fast_mode_config()
    seconds = config.adaptive_solve_limit_seconds if adaptive else config.normal_solve_limit_seconds
    # Preserve the existing bounded model stages and lexicographic semantics;
    # only their CP search budgets change.
    return ((1, 7, seconds), (2, 7, seconds), (3, 7, seconds))


@dataclass
class OptimizationWindowWatchdog:
    """Monotonic, advisory deadline for one rolling optimization window."""
    deadline: float
    ceiling_seconds: float
    started: float

    @classmethod
    def start(cls, ceiling_seconds: float | None = None):
        ceiling = float(fast_mode_config().window_safety_ceiling_seconds
                        if ceiling_seconds is None else ceiling_seconds)
        now = time.perf_counter()
        return cls(now + max(0.0, ceiling), ceiling, now)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.perf_counter())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


def deadline_stage_budget(deadline: float | None, stage_limit: float) -> float:
    """Return the remaining shared budget, never resetting per stage."""
    if deadline is None:
        return max(0.0, float(stage_limit))
    return max(0.0, min(float(stage_limit), deadline - time.perf_counter()))


def shared_attempt_deadline(state: dict, budget_seconds: float, kind: str,
                            now: float | None = None) -> float:
    """Create/reuse one deadline for a nested canonical attempt."""
    if state.get("_craft_cp_deadline_kind") != str(kind):
        current = time.perf_counter() if now is None else float(now)
        state["_craft_cp_deadline_kind"] = str(kind)
        state["_craft_cp_deadline"] = current + float(budget_seconds)
    return float(state["_craft_cp_deadline"])
