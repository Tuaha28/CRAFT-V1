from __future__ import annotations


def urgent_priority_contract(order_id: str) -> dict:
    return {"order_id": str(order_id), "priority": "URGENT",
            "lexicographic_order": ["hard_feasibility", "event_conflict_free",
                                    "urgent_order_tardiness", "total_tardiness", "churn"]}
