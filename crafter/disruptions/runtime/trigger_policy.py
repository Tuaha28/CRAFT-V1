"""Configurable hard/soft/manager rescheduling trigger decisions."""
from __future__ import annotations

from dataclasses import dataclass

from ..events import EventType
from ..state_update import event_conflicts


@dataclass(frozen=True)
class TriggerDecision:
    trigger: str
    reason: str
    repair_required: bool
    event_id: str
    evidence: tuple = ()

    def as_dict(self):
        return {"trigger": self.trigger, "reason": self.reason,
                "repair_required": self.repair_required,
                "event_id": self.event_id, "evidence": list(self.evidence)}


def evaluate_trigger(event, phenotype, problem, *, material_risk: bool = False,
                     delivery_risk: bool = False) -> TriggerDecision:
    conflicts = event_conflicts(event, phenotype, problem)
    if event.event_type == EventType.UNKNOWN.value:
        return TriggerDecision("MANAGER_TRIGGER", "UNKNOWN_OR_UNSUPPORTED_EVENT", True,
                               event.event_id)
    if conflicts:
        return TriggerDecision("HARD_TRIGGER", "CURRENT_PLAN_EVENT_CONFLICT", True,
                               event.event_id, tuple(sorted({x["kind"] for x in conflicts})))
    if event.event_type == EventType.URGENT_ORDER.value or event.metadata.get("priority") == "URGENT":
        return TriggerDecision("MANAGER_TRIGGER", "EXPLICIT_URGENT_ORDER", True, event.event_id)
    if material_risk or delivery_risk:
        return TriggerDecision("SOFT_TRIGGER", "MATERIAL_DELIVERY_OR_RESOURCE_RISK", True, event.event_id)
    return TriggerDecision("MONITOR_ONLY", "NO_MATERIAL_FEASIBILITY_OR_DELIVERY_RISK", False, event.event_id)
