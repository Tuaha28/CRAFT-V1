"""Residual Continuation Contract V1.

This module evaluates evidence; it never invents a continuation schedule. The
operation-level CRAFT adapter may admit a partial operation only after a
trusted continuation producer supplies the required residual plan.
"""
from __future__ import annotations

from collections import Counter

from .contracts import digest

CONTRACT = "RESIDUAL_CONTINUATION_V1"
CLASSIFICATIONS = ("RESOLVED_COMMITTED_BOUNDARY", "RESOLVED_SPLITTABLE_RESIDUAL", "BOUNDARY_UNRESOLVED")


def _missing_for_partial(op):
    quantity = op.get("quantity") or {}
    missing = []
    if quantity.get("remaining") is None or quantity.get("total") is None:
        missing.append("RECONCILED_QUANTITY_LEDGER")
    if quantity.get("remaining") != (quantity.get("in_flight", 0) or 0) + (quantity.get("not_started", 0) or 0):
        missing.append("QUANTITY_CONSERVATION")
    if quantity.get("in_flight", 0):
        missing.extend(("IN_FLIGHT_REMAINING_PRODUCTIVE_WORK",
                        "FIXED_CREW_CONTINUATION_SEGMENTS",
                        "FINITE_MACHINE_OCCUPANCY_BOUNDARY"))
    if not op.get("committed_execution"):
        missing.append("AUTHORITATIVE_RESIDUAL_COMMITMENT_PLAN")
    if not op.get("completion_boundary"):
        missing.append("PHYSICAL_COMPLETION_BOUNDARY")
    residual = set(op.get("residual_lot_ids", []))
    committed = {row.get("lot_id") for row in op.get("committed_execution", [])}
    if residual and not residual.issubset(committed):
        missing.append("ALL_RESIDUAL_LOTS_COVERED")
    if op.get("boundary_status") != "COMMITTED_EXECUTION_BOUNDARY":
        missing.append("VALIDATED_BOUNDARY_STATUS")
    if op.get("quantity", {}).get("not_started", 0) and not op.get("splittable_residual_evidence"):
        missing.append("PROOF_OPERATION_LEVEL_RESIDUAL_IS_SPLITTABLE")
    return sorted(set(missing))


def classify_residual_operation(op):
    """Return a checkable classification for one normalized operation."""
    state = op.get("execution_state")
    base = {
        "contract": CONTRACT,
        "operation_key": op.get("operation_key"),
        "execution_state": state,
        "optimizable": False,
        "quantity": op.get("quantity"),
        "residual_lot_ids": sorted(op.get("residual_lot_ids", [])),
        "residual_lot_resource_evidence": [
            {key: lot.get(key) for key in ("lot_id", "state", "quantity", "line_id", "machine_id", "worker_id", "batch_id")}
            for lot in op.get("lots", []) if lot.get("state") != "COMPLETED"
        ],
        "resource_holds": op.get("resource_holds", []),
        "completion_boundary": op.get("completion_boundary"),
    }
    if state != "PARTIALLY_EXECUTED":
        base.update({"classification": "NOT_APPLICABLE", "reasons": []})
        return base

    missing = _missing_for_partial(op)
    # No current source operation can satisfy these gates: this branch is
    # intentionally explicit so a future continuation producer can be tested
    # without changing the normalized execution-state classifier.
    if not missing:
        base.update({"classification": "RESOLVED_COMMITTED_BOUNDARY", "reasons": [], "optimizable": False})
    elif (not op.get("resource_holds") and op.get("quantity", {}).get("in_flight", 0) == 0
          and op.get("splittable_residual_evidence") and
          op.get("splittable_residual_evidence", {}).get("validated") is True):
        base.update({"classification": "RESOLVED_SPLITTABLE_RESIDUAL", "reasons": []})
    else:
        base.update({"classification": "BOUNDARY_UNRESOLVED", "reasons": missing})
    return base


def classify_snapshot(snapshot):
    records = [classify_residual_operation(op) for op in snapshot.get("operations", [])]
    partial = [row for row in records if row["execution_state"] == "PARTIALLY_EXECUTED"]
    counts = Counter(row["classification"] for row in partial)
    result = {
        "contract": CONTRACT,
        "snapshot_hash": snapshot.get("snapshot_hash"),
        "total_partial_operations": len(partial),
        "resolved_committed_boundary": counts["RESOLVED_COMMITTED_BOUNDARY"],
        "resolved_splittable_residual": counts["RESOLVED_SPLITTABLE_RESIDUAL"],
        "boundary_unresolved": counts["BOUNDARY_UNRESOLVED"],
        "records": partial,
    }
    result["classification_hash"] = digest(result)
    return result


def validate_classification(snapshot, classification):
    """Independent structural gate for the classification artifact."""
    expected = {op["operation_key"] for op in snapshot.get("operations", [])
                if op.get("execution_state") == "PARTIALLY_EXECUTED"}
    actual = {row.get("operation_key") for row in classification.get("records", [])}
    errors = []
    if expected != actual:
        errors.append("PARTIAL_OPERATION_IDENTITY_MISMATCH")
    if classification.get("total_partial_operations") != len(expected):
        errors.append("PARTIAL_COUNT_MISMATCH")
    for row in classification.get("records", []):
        if row.get("classification") not in CLASSIFICATIONS:
            errors.append("INVALID_CLASSIFICATION")
        if row.get("classification") != "BOUNDARY_UNRESOLVED" and not row.get("completion_boundary"):
            errors.append("RESOLVED_WITHOUT_COMPLETION_BOUNDARY")
        if row.get("classification") == "BOUNDARY_UNRESOLVED" and row.get("optimizable"):
            errors.append("UNRESOLVED_OPERATION_OPTIMIZABLE")
    return {"status": "VALID" if not errors else "BLOCKED", "valid": not errors,
            "errors": sorted(set(errors)), "database_writes": 0}
