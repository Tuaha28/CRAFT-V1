"""Leak-free, plan-aware operation feature schema for GNN v3."""

from __future__ import annotations

import hashlib
import json


PREDICTION_TIME_CONTRACT = (
    "plan-aware pre-execution risk prediction after a deterministic frozen "
    "Phase-A production plan exists and before the predicted Phase-B execution"
)

OPERATION_FEATURE_NAMES_V3 = (
    "sam_norm",
    "qty_norm",
    "machine_parallelism_norm",
    "deadline_budget_norm",
    "routing_step_norm",
    "steps_remaining_norm",
    "pool_occupancy_norm",
)

FORBIDDEN_PHASE_B_FEATURES = frozenset({
    "decisive_lot_breakdown_count_norm",
    "mean_queue_delay_norm",
    "mean_breakdown_count_norm",
    "has_dispatch_context",
})


def operation_schema_v3() -> dict:
    payload = {
        "version": 3,
        "prediction_time_contract": PREDICTION_TIME_CONTRACT,
        "names": list(OPERATION_FEATURE_NAMES_V3),
        "count": len(OPERATION_FEATURE_NAMES_V3),
        "availability": "frozen_phase_a_only",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def validate_schema_v3(schema: dict) -> int:
    expected = operation_schema_v3()
    if schema != expected:
        raise ValueError("Operation feature schema differs from leak-free schema v3")
    if FORBIDDEN_PHASE_B_FEATURES.intersection(schema["names"]):
        raise ValueError("Phase-B dispatch feature present in leak-free schema v3")
    return schema["count"]
