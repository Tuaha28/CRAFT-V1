"""Safe event-aware fallback records; never labels an event-conflicting plan valid."""
from __future__ import annotations


def build_fallback_decision(event, *, baseline_conflicts, attempted_levels=(),
                            options=()) -> dict:
    conflicts = list(baseline_conflicts or ())
    if conflicts:
        return {
            "status": "MANAGER_REQUIRED" if not options else "POST_EVENT_FALLBACK_REQUIRED",
            "fallback_used": False,
            "reason": "PRE_EVENT_SCHEDULE_CONFLICTS_WITH_EVENT",
            "event_id": event.event_id,
            "attempted_recovery_levels": list(attempted_levels),
            "conflicts": conflicts,
            "options": list(options),
        }
    return {"status": "VALIDATED_PRE_EVENT_FALLBACK", "fallback_used": True,
            "reason": "PRE_EVENT_SCHEDULE_REMAINS_EVENT_SAFE", "event_id": event.event_id,
            "attempted_recovery_levels": list(attempted_levels), "conflicts": []}
