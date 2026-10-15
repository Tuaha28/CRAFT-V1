"""Residual-capable preparation boundary, intentionally fail-closed.

This module prepares an auditable residual handoff without changing the CRAFT
solver's operation-level contracts. It is not a scheduler and it never
creates a SchedulingProblem. The admission flag remains false until a future
preparation/materialization/validator contract can prove that residual
quantity, whole-operation precedence and exact replay are represented together.
"""
from __future__ import annotations

from copy import deepcopy

from .contracts import digest
from .residual_adapter import (
    CONTRACT as ADAPTER_CONTRACT,
    ResidualAdapterError,
    build_residual_view,
    validate_residual_view,
)


CONTRACT = "RESIDUAL_PREPARATION_BOUNDARY_V1"
STATUS = "PREPARED_NOT_ADMITTED"


class ResidualPreparationError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("Residual preparation boundary rejected the handoff")


def _prepared_operation(row):
    boundary = row["execution_boundary"]
    residual = row["residual_work"]
    return {
        "original_operation_key": row["original_operation_key"],
        "order_id": row["order_id"],
        "mapping_kind": row["mapping_kind"],
        "immutable_history": {
            "completed_lot_ids": boundary["completed_lot_ids"],
            "completed_quantity": boundary["completed_quantity"],
        },
        "fixed_execution": {
            "in_flight_quantity": boundary["in_flight_quantity"],
            "resource_commitments": deepcopy(row["resource_commitments"]),
            "resource_availability": deepcopy(row["resource_availability"]),
        },
        "remaining_executable_work": {
            "residual_lot_ids": residual["lot_ids"],
            "residual_lots": deepcopy(residual["lots"]),
            "remaining_quantity": residual["remaining_quantity"],
            "productive_seconds": residual["productive_seconds"],
            "source": residual["source"],
            "model_version": residual["model_version"],
            "assumptions": residual["assumptions"],
        },
        "precedence": deepcopy(row["precedence"]),
        "provenance": deepcopy(row["provenance"]),
        "craft_operation_key": row["original_operation_key"],
        "craft_input_allowed": False,
        "rejection_reason": (
            "Current OperationSpec, materialization and independent validators "
            "cannot jointly represent residual quantity plus immutable history."
        ),
    }


def prepare_residual_for_craft(packet):
    """Build a deterministic, non-admitted preparation artifact."""
    try:
        view = build_residual_view(packet)
    except ResidualAdapterError as exc:
        raise ResidualPreparationError(exc.errors) from exc
    validation = validate_residual_view(view)
    if not validation["valid"]:
        raise ResidualPreparationError(
            [{"code": error} for error in validation["errors"]]
        )
    prepared = {
        "contract": CONTRACT,
        "adapter_contract": ADAPTER_CONTRACT,
        "status": STATUS,
        "database": view["database"],
        "factory_id": view["factory_id"],
        "as_of": view["as_of"],
        "source_snapshot_hash": view["source_snapshot_hash"],
        "input_view_hash": view["view_hash"],
        "operations": [_prepared_operation(row) for row in view["operations"]],
        "craft_input_allowed": False,
        "craft_compatibility": "BLOCKED",
        "blocked_capabilities": [
            "RESIDUAL_QUANTITY_IN_OPERATION_SPEC",
            "IMMUTABLE_HISTORY_PLUS_RESIDUAL_MATERIALIZATION",
            "WHOLE_OPERATION_SUCCESSOR_RELEASE_BOUNDARY",
            "RESIDUAL_QUANTITY_INDEPENDENT_PHYSICAL_VALIDATION",
        ],
        "database_writes": 0,
    }
    prepared["preparation_hash"] = digest(prepared)
    return prepared


def validate_preparation(prepared):
    """Independent structural validation for the preparation artifact."""
    errors = []
    if prepared.get("contract") != CONTRACT:
        errors.append("UNSUPPORTED_CONTRACT")
    if prepared.get("status") != STATUS:
        errors.append("INVALID_PREPARATION_STATUS")
    if prepared.get("craft_input_allowed") is not False:
        errors.append("CRAFT_ADMISSION_GATE_OPEN")
    if prepared.get("craft_compatibility") != "BLOCKED":
        errors.append("CRAFT_COMPATIBILITY_NOT_BLOCKED")
    keys = [row.get("original_operation_key") for row in prepared.get("operations", [])]
    if len(keys) != len(set(keys)):
        errors.append("DUPLICATE_OPERATION_IDENTITY")
    for row in prepared.get("operations", []):
        history = row.get("immutable_history") or {}
        fixed = row.get("fixed_execution") or {}
        remaining = row.get("remaining_executable_work") or {}
        completed = history.get("completed_quantity")
        inflight = fixed.get("in_flight_quantity")
        residual_qty = remaining.get("remaining_quantity")
        if not all(isinstance(value, int) and value >= 0 for value in (completed, inflight, residual_qty)):
            errors.append("INVALID_QUANTITY")
        if inflight + sum(lot.get("quantity", 0) for lot in remaining.get("residual_lots", [])) != residual_qty + inflight:
            errors.append("RESIDUAL_LOT_QUANTITY_MISMATCH")
        if set(remaining.get("residual_lot_ids", [])) != {
            lot.get("lot_id") for lot in remaining.get("residual_lots", [])
        }:
            errors.append("RESIDUAL_LOT_IDENTITY_MISMATCH")
        if row.get("craft_operation_key") != row.get("original_operation_key"):
            errors.append("ORIGINAL_OPERATION_MAPPING_LOST")
        if row.get("craft_input_allowed") is not False:
            errors.append("OPERATION_ADMISSION_GATE_OPEN")
        for commitment in fixed.get("resource_commitments", []):
            segments = commitment.get("segments") or []
            if not segments:
                errors.append("EMPTY_FIXED_RESOURCE_SEGMENTS")
    return {
        "status": "VALID" if not errors else "REJECTED",
        "valid": not errors,
        "errors": sorted(set(errors)),
        "database_writes": 0,
    }


def preparation_summary(prepared):
    return {
        "contract": CONTRACT,
        "status": prepared.get("status"),
        "operation_count": len(prepared.get("operations", [])),
        "preparation_hash": prepared.get("preparation_hash"),
        "craft_input_allowed": prepared.get("craft_input_allowed"),
        "craft_compatibility": prepared.get("craft_compatibility"),
    }
