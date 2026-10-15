"""Deterministic disruption event contracts.

Events are immutable descriptions only.  Applying an event is handled by the
shadow-state layer; no event in this package writes to the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Tuple


class EventType(str, Enum):
    MACHINE_BREAKDOWN = "MACHINE_BREAKDOWN"
    WORKER_ABSENCE = "WORKER_ABSENCE"
    URGENT_ORDER = "URGENT_ORDER"
    PREDECESSOR_DELAY = "PREDECESSOR_DELAY"
    RESOURCE_CONGESTION = "RESOURCE_CONGESTION"
    MULTIPLE_MACHINE_BREAKDOWN = "MULTIPLE_MACHINE_BREAKDOWN"
    MULTI_WORKER_ABSENCE = "MULTI_WORKER_ABSENCE"
    COMBINED = "COMBINED"
    MACHINE_RECOVERY = "MACHINE_RECOVERY"
    WORKER_RETURN = "WORKER_RETURN"
    MATERIAL_SHORTAGE = "MATERIAL_SHORTAGE"
    QUALITY_HOLD = "QUALITY_HOLD"
    UTILITY_OUTAGE = "UTILITY_OUTAGE"
    UNKNOWN = "UNKNOWN_OR_UNSUPPORTED_EVENT"


@dataclass(frozen=True)
class DisruptionEvent:
    event_id: str
    event_type: str
    event_time_seconds: int
    factory_id: int
    machine_ids: Tuple[int, ...] = ()
    worker_ids: Tuple[str, ...] = ()
    order_ids: Tuple[str, ...] = ()
    operation_keys: Tuple[str, ...] = ()
    duration_seconds: int = 0
    severity: str = "medium"
    source: str = "offline_benchmark"
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def end_time_seconds(self) -> int:
        return self.event_time_seconds + max(0, int(self.duration_seconds))

    def as_dict(self):
        return {
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "event_time_seconds": self.event_time_seconds,
            "factory_id": self.factory_id,
            "machine_ids": list(self.machine_ids),
            "worker_ids": list(self.worker_ids),
            "order_ids": list(self.order_ids),
            "operation_keys": list(self.operation_keys),
            "duration_seconds": self.duration_seconds,
            "end_time_seconds": self.end_time_seconds,
            "severity": self.severity,
            "source": self.source,
            "metadata": dict(self.metadata),
        }
