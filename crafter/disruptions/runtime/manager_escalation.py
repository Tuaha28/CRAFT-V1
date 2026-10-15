"""Structured manager-required outcomes."""
from __future__ import annotations


def manager_required(event, *, reason, affected_orders=(), affected_operations=(),
                     affected_resources=(), attempted_levels=(),
                     best_available_option=None, predicted_consequences=None) -> dict:
    return {
        "status": "MANAGER_REQUIRED",
        "event": event.as_dict() if hasattr(event, "as_dict") else event,
        "reason": str(reason),
        "affected_orders": sorted({str(x) for x in affected_orders}),
        "affected_operations": sorted({str(x) for x in affected_operations}),
        "affected_resources": list(affected_resources),
        "attempted_recovery_levels": list(attempted_levels),
        "best_available_option": best_available_option or {},
        "predicted_consequences": predicted_consequences or {},
    }
