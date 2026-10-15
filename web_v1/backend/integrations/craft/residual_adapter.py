"""Side-effect-free compatibility view for residual execution evidence.

The adapter deliberately does *not* manufacture a ``SchedulingProblem`` and
does not import the CRAFT solver.  Current CRAFT operations are indivisible
intervals with one authoritative productive duration; this view preserves the
evidence needed by a future residual-capable preparation layer while keeping
the existing admission gate closed.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from .contracts import digest
from .evidence_intake import CONTRACT as EVIDENCE_CONTRACT
from .evidence_intake import normalized_packet, validate_evidence


CONTRACT = "RESIDUAL_OPERATION_ADAPTER_V1"
REPRESENTATION = "APPROACH_B_OPERATION_LEVEL_RESIDUAL_VIEW"
MAPPING_KIND = "ORIGINAL_OPERATION_TO_RESIDUAL_VIEW"


class ResidualAdapterError(ValueError):
    """Raised when accepted evidence cannot form a safe compatibility view."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("Residual evidence cannot be represented safely")


def _interval_overlap_errors(commitments):
    seen = defaultdict(list)
    errors = []
    for commitment in commitments:
        token = f"{commitment.get('resource_type')}:{commitment.get('resource_id')}"
        for segment in commitment.get("segments", []):
            start, end = segment.get("start"), segment.get("end")
            for prior in seen[token]:
                if start < prior[1] and prior[0] < end:
                    errors.append({"code": "RESOURCE_OVERLAP", "resource": token})
            seen[token].append((start, end))
    return errors


def _lot_ledger(operation):
    ledger = operation.get("quantity_ledger") or {}
    lots = operation.get("continuation_lots") or []
    state_totals = defaultdict(int)
    for lot in lots:
        state = lot.get("state")
        quantity = lot.get("quantity")
        if not isinstance(quantity, int) or quantity < 0:
            return None, [{"code": "INVALID_RESIDUAL_LOT_QUANTITY", "lot_id": lot.get("lot_id")}]
        state_totals[state] += quantity
    errors = []
    # Evidence intake already checks the operation ledger.  This second check
    # proves that the residual lot records themselves cover the residual work.
    if state_totals["IN_FLIGHT"] != ledger.get("in_flight", 0):
        errors.append({"code": "IN_FLIGHT_LOT_LEDGER_MISMATCH"})
    if state_totals["NOT_STARTED"] != ledger.get("unstarted", 0):
        errors.append({"code": "UNSTARTED_LOT_LEDGER_MISMATCH"})
    if sum(state_totals.values()) != ledger.get("remaining", 0):
        errors.append({"code": "RESIDUAL_LOT_LEDGER_MISMATCH"})
    return state_totals, errors


def _operation_view(operation):
    errors = []
    state_totals, ledger_errors = _lot_ledger(operation)
    errors.extend(ledger_errors)
    residual_ids = list(operation.get("residual_lot_ids") or [])
    continuation = list(operation.get("continuation_lots") or [])
    continuation_ids = [row.get("lot_id") for row in continuation]
    if len(residual_ids) != len(set(residual_ids)):
        errors.append({"code": "DUPLICATE_RESIDUAL_LOT"})
    if set(residual_ids) != set(continuation_ids):
        errors.append({"code": "RESIDUAL_LOT_COVERAGE_FAILED"})
    errors.extend(_interval_overlap_errors(operation.get("resource_commitments") or []))
    if errors:
        return None, errors

    ledger = operation["quantity_ledger"]
    mode = operation["mode"]
    # This is intentionally a view, not a CRAFT operation.  A future
    # residual-capable adapter may turn the residual_work block into a valid
    # operation input after proving whole-operation precedence and replay.
    row = {
        "original_operation_key": operation["operation_key"],
        "order_id": operation["order_id"],
        "mapping_kind": MAPPING_KIND,
        "mode": mode,
        "execution_boundary": {
            "as_of": operation["as_of"],
            "required_quantity": ledger["required"],
            "completed_lot_ids": sorted(operation.get("completed_lot_ids", [])),
            "completed_quantity": ledger["completed"],
            "in_flight_quantity": ledger["in_flight"],
            "unstarted_quantity": ledger["unstarted"],
            "remaining_quantity": ledger["remaining"],
            "immutable_history": True,
        },
        "residual_work": {
            "lot_ids": sorted(residual_ids),
            "lots": sorted(deepcopy(continuation), key=lambda item: str(item.get("lot_id"))),
            "remaining_quantity": ledger["remaining"],
            "productive_seconds": operation["remaining_work"]["productive_seconds"],
            "source": operation["remaining_work"]["source"],
            "model_version": operation["remaining_work"].get("model_version"),
            "assumptions": list(operation["remaining_work"].get("assumptions", [])),
            "residual_optimizable_in_future": mode == "SPLITTABLE_RESIDUAL",
        },
        "resource_commitments": deepcopy(operation.get("resource_commitments") or []),
        "resource_availability": deepcopy(operation.get("resource_availability") or {}),
        "precedence": deepcopy(operation.get("precedence") or {}),
        "provenance": deepcopy(operation.get("provenance") or {}),
        "craft_input_allowed": False,
        "craft_compatibility": "BLOCKED",
        "craft_block_reason": (
            "Current CRAFT operation-level interfaces have no residual quantity/lot "
            "representation with independently validated fixed history."
        ),
    }
    return row, []


def build_residual_view(packet):
    """Build a deterministic compatibility view from *accepted* evidence only."""
    intake = validate_evidence(packet)
    if not intake["valid"]:
        raise ResidualAdapterError(intake["errors"])
    canonical = normalized_packet(packet)
    operations = []
    errors = []
    for operation in canonical.get("operations", []):
        row, row_errors = _operation_view(operation)
        if row is not None:
            operations.append(row)
        errors.extend({"operation_key": operation.get("operation_key"), **error} for error in row_errors)
    if errors:
        raise ResidualAdapterError(errors)
    view = {
        "contract": CONTRACT,
        "evidence_contract": EVIDENCE_CONTRACT,
        "representation": REPRESENTATION,
        "database": canonical.get("database"),
        "factory_id": canonical.get("factory_id"),
        "as_of": canonical.get("as_of"),
        "source_snapshot_hash": canonical.get("source_snapshot_hash"),
        "input_packet_hash": digest(canonical),
        "operations": operations,
        "craft_input_allowed": False,
        "craft_compatibility": "BLOCKED",
        "craft_block_reason": (
            "A residual view is proven for evidence/audit, but current CRAFT "
            "models accept one indivisible operation interval and cannot consume it."
        ),
    }
    view["view_hash"] = digest(view)
    return view


def validate_residual_view(view):
    """Independently validate adapter output without invoking CRAFT."""
    errors = []
    if view.get("contract") != CONTRACT:
        errors.append("UNSUPPORTED_CONTRACT")
    if view.get("craft_input_allowed") is not False or view.get("craft_compatibility") != "BLOCKED":
        errors.append("CRAFT_ADMISSION_GATE_NOT_CLOSED")
    operations = view.get("operations") or []
    keys = [row.get("original_operation_key") for row in operations]
    if len(keys) != len(set(keys)):
        errors.append("DUPLICATE_ORIGINAL_OPERATION")
    for row in operations:
        boundary = row.get("execution_boundary") or {}
        residual = row.get("residual_work") or {}
        required = boundary.get("required_quantity")
        completed = boundary.get("completed_quantity")
        remaining = boundary.get("remaining_quantity")
        inflight = boundary.get("in_flight_quantity")
        unstarted = boundary.get("unstarted_quantity")
        if (required is None or completed is None or remaining is None
                or completed + remaining != required):
            errors.append("INVALID_QUANTITY_BOUNDARY")
        if inflight + unstarted != remaining:
            errors.append("QUANTITY_CONSERVATION_FAILED")
        lot_ids = residual.get("lot_ids") or []
        lots = residual.get("lots") or []
        if len(lot_ids) != len(set(lot_ids)) or set(lot_ids) != {lot.get("lot_id") for lot in lots}:
            errors.append("RESIDUAL_LOT_IDENTITY_FAILED")
        if sum(lot.get("quantity", 0) for lot in lots) != remaining:
            errors.append("RESIDUAL_LOT_QUANTITY_FAILED")
        if boundary.get("immutable_history") is not True:
            errors.append("COMPLETED_HISTORY_NOT_IMMUTABLE")
        errors.extend(error["code"] for error in _interval_overlap_errors(row.get("resource_commitments") or []))
        precedence = row.get("precedence") or {}
        if precedence.get("predecessor_completion") and precedence.get("successor_earliest_ready"):
            if precedence["successor_earliest_ready"] < precedence["predecessor_completion"]:
                errors.append("SUCCESSOR_RELEASE_PRECEDES_PREDECESSOR")
    return {"status": "VALID" if not errors else "REJECTED", "valid": not errors,
            "errors": sorted(set(errors)), "database_writes": 0}


def compatibility_summary(view):
    """Small JSON-safe summary suitable for audit reports."""
    return {
        "contract": CONTRACT,
        "representation": view.get("representation"),
        "operation_count": len(view.get("operations", [])),
        "view_hash": view.get("view_hash"),
        "craft_input_allowed": view.get("craft_input_allowed"),
        "craft_compatibility": view.get("craft_compatibility"),
    }
