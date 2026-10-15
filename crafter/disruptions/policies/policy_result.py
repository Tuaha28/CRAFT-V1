from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

from .recovery_levels import RecoveryLevel


@dataclass(frozen=True)
class RecoveryCandidate:
    operation_key: str
    recovery_level: RecoveryLevel
    reason: str
    line_id: int | None = None
    k: int | None = None
    machine_ids: Tuple[int, ...] = ()
    worker_ids: Tuple[str, ...] = ()
    start_lower_bound: int | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "operation_key": self.operation_key,
            "recovery_level": self.recovery_level.name,
            "recovery_level_value": int(self.recovery_level),
            "reason": self.reason,
            "line_id": self.line_id,
            "k": self.k,
            "machine_ids": list(self.machine_ids),
            "worker_ids": list(self.worker_ids),
            "start_lower_bound": self.start_lower_bound,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class RecoveryDecision:
    operation_key: str
    event_id: str
    candidates: Tuple[RecoveryCandidate, ...]
    failure: str | None = None

    @property
    def preferred(self) -> RecoveryCandidate | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict:
        return {
            "operation_key": self.operation_key,
            "event_id": self.event_id,
            "candidates": [x.as_dict() for x in self.candidates],
            "preferred": self.preferred.as_dict() if self.preferred else None,
            "failure": self.failure,
        }
