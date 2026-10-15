"""Offline transaction envelope for event repair outcomes."""
from __future__ import annotations

from dataclasses import dataclass

from .manager_escalation import manager_required


@dataclass(frozen=True)
class RepairTransaction:
    event_id: str
    trigger: dict
    attempted_levels: tuple = ()
    status: str = "UNSET"
    database_writes: int = 0

    def manager_outcome(self, **kwargs):
        return manager_required(**kwargs)

    def as_dict(self):
        return {"event_id": self.event_id, "trigger": self.trigger,
                "attempted_levels": list(self.attempted_levels),
                "status": self.status, "database_writes": self.database_writes}
