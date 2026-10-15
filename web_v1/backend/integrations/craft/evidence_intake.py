"""Residual execution evidence intake and validation.

This is an evidence contract, not a continuation scheduler. A valid evidence
packet can establish what a factory has authorized and measured, but admission
to the current operation-level CRAFT model remains a separate capability gate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from copy import deepcopy

from .contracts import canonical_json, digest

CONTRACT = "RESIDUAL_EVIDENCE_INTAKE_V1"
MODES = {"COMMITTED_CONTINUATION", "SPLITTABLE_RESIDUAL"}
WORK_SOURCES = {"OBSERVED_REMAINING", "STANDARD_WORK_MODEL", "AUTHORIZED_ESTIMATE"}
RESOURCE_TYPES = {"machine", "worker", "line"}


def _dt(value, field):
    if value is None:
        raise ValueError(f"{field} is required")
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is not None:
        raise ValueError(f"{field} must use the dataset-local naive clock")
    return result


def _intervals(value, field):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must contain at least one interval")
    intervals = []
    for index, pair in enumerate(value):
        if not isinstance(pair, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        start, end = _dt(pair.get("start"), f"{field}[{index}].start"), _dt(pair.get("end"), f"{field}[{index}].end")
        if start >= end:
            raise ValueError(f"{field}[{index}] is not half-open positive")
        intervals.append((start, end))
    return intervals


def _covered(segment, availability):
    return any(left <= segment[0] and segment[1] <= right for left, right in availability)


def _quantity(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _validate_operation(op, as_of, errors):
    key = op.get("operation_key")
    prefix = f"operation[{key}]"
    for field in ("operation_key", "order_id"):
        if op.get(field) in (None, ""):
            errors.append({"operation_key": key, "code": f"OPERATION_{field.upper()}_MISSING"})
    mode = op.get("mode")
    if mode not in MODES:
        errors.append({"operation_key": key, "code": "INVALID_MODE"})
        return
    try:
        if _dt(op.get("as_of"), f"{prefix}.as_of") != as_of:
            errors.append({"operation_key": key, "code": "AS_OF_MISMATCH"})
    except ValueError as exc:
        errors.append({"operation_key": key, "code": "INVALID_AS_OF", "detail": str(exc)})
    ledger = op.get("quantity_ledger") or {}
    try:
        total = _quantity(ledger.get("required"), f"{prefix}.quantity.required")
        completed = _quantity(ledger.get("completed"), f"{prefix}.quantity.completed")
        inflight = _quantity(ledger.get("in_flight"), f"{prefix}.quantity.in_flight")
        unstarted = _quantity(ledger.get("unstarted"), f"{prefix}.quantity.unstarted")
        remaining = _quantity(ledger.get("remaining"), f"{prefix}.quantity.remaining")
        if completed + remaining != total or inflight + unstarted != remaining:
            errors.append({"operation_key": key, "code": "QUANTITY_CONSERVATION_FAILED"})
    except ValueError as exc:
        errors.append({"operation_key": key, "code": "INVALID_QUANTITY_LEDGER", "detail": str(exc)})
        total = completed = inflight = unstarted = remaining = None

    residual = op.get("residual_lot_ids") or []
    continuation_lots = op.get("continuation_lots") or []
    if len(residual) != len(set(residual)):
        errors.append({"operation_key": key, "code": "DUPLICATE_RESIDUAL_LOT"})
    if set(residual) != {row.get("lot_id") for row in continuation_lots}:
        errors.append({"operation_key": key, "code": "RESIDUAL_LOT_COVERAGE_FAILED"})
    if mode == "COMMITTED_CONTINUATION" and inflight and not op.get("resource_commitments"):
        errors.append({"operation_key": key, "code": "IN_FLIGHT_RESOURCES_MISSING"})
    if mode == "SPLITTABLE_RESIDUAL" and inflight:
        errors.append({"operation_key": key, "code": "IN_FLIGHT_CANNOT_BE_SPLIT"})

    provenance = op.get("provenance") or {}
    for field in ("evidence_version", "source", "authorized_by", "authorization_id", "captured_at", "valid_until"):
        if not provenance.get(field):
            errors.append({"operation_key": key, "code": f"PROVENANCE_{field.upper()}_MISSING"})
    try:
        captured = _dt(provenance.get("captured_at"), f"{prefix}.provenance.captured_at")
        valid_until = _dt(provenance.get("valid_until"), f"{prefix}.provenance.valid_until")
        # Evidence may be collected after the execution as-of time. It must
        # remain valid for that as-of, but a later capture is not future
        # execution and is therefore allowed.
        if valid_until < as_of:
            errors.append({"operation_key": key, "code": "STALE_OR_FUTURE_PROVENANCE"})
    except ValueError as exc:
        errors.append({"operation_key": key, "code": "INVALID_PROVENANCE_CLOCK", "detail": str(exc)})

    work = op.get("remaining_work") or {}
    if work.get("source") not in WORK_SOURCES:
        errors.append({"operation_key": key, "code": "INVALID_REMAINING_WORK_SOURCE"})
    if work.get("source") == "FUTURE_ACTUAL":
        errors.append({"operation_key": key, "code": "PROJECTED_ACTUAL_NOT_EVIDENCE"})
    if not isinstance(work.get("productive_seconds"), int) or work.get("productive_seconds", 0) <= 0:
        errors.append({"operation_key": key, "code": "REMAINING_WORK_NOT_POSITIVE_INTEGER"})
    if not isinstance(work.get("assumptions"), list) or not work.get("assumptions"):
        errors.append({"operation_key": key, "code": "WORK_ASSUMPTIONS_MISSING"})

    commitments = op.get("resource_commitments") or []
    availability = op.get("resource_availability") or {}
    used = defaultdict(list)
    for index, commitment in enumerate(commitments):
        resource_type, resource_id = commitment.get("resource_type"), commitment.get("resource_id")
        if resource_type not in RESOURCE_TYPES or resource_id in (None, ""):
            errors.append({"operation_key": key, "code": "INVALID_RESOURCE_COMMITMENT", "index": index})
            continue
        try:
            segments = _intervals(commitment.get("segments"), f"{prefix}.resource_commitments[{index}].segments")
        except ValueError as exc:
            errors.append({"operation_key": key, "code": "INVALID_RESOURCE_SEGMENTS", "detail": str(exc)})
            continue
        token = f"{resource_type}:{resource_id}"
        allowed = _intervals(availability.get(token), f"{prefix}.resource_availability.{token}") if token in availability else []
        if not allowed:
            errors.append({"operation_key": key, "code": "RESOURCE_CALENDAR_MISSING", "resource": token})
        for segment in segments:
            if segment[0] < as_of and resource_type in {"machine", "worker"}:
                # Existing execution is allowed to begin before the snapshot;
                # the evidence packet must still identify it explicitly.
                pass
            if allowed and not _covered(segment, allowed):
                errors.append({"operation_key": key, "code": "RESOURCE_CALENDAR_CONFLICT", "resource": token})
            for previous in used[token]:
                if segment[0] < previous[1] and previous[0] < segment[1]:
                    errors.append({"operation_key": key, "code": "RESOURCE_OVERLAP", "resource": token})
            used[token].append(segment)

    precedence = op.get("precedence") or {}
    if precedence.get("predecessor_completion") and precedence.get("successor_earliest_ready"):
        try:
            if _dt(precedence["successor_earliest_ready"], "successor_earliest_ready") < _dt(precedence["predecessor_completion"], "predecessor_completion"):
                errors.append({"operation_key": key, "code": "SUCCESSOR_RELEASE_PRECEDES_PREDECESSOR"})
        except ValueError as exc:
            errors.append({"operation_key": key, "code": "INVALID_PRECEDENCE_CLOCK", "detail": str(exc)})
    if mode == "SPLITTABLE_RESIDUAL":
        proof = op.get("split_proof") or {}
        required = ("operation_level_residual_supported", "no_duplicate_completed_work",
                    "predecessor_boundary_preserved", "resource_reallocation_authorized")
        if not all(proof.get(field) is True for field in required):
            errors.append({"operation_key": key, "code": "SPLIT_PROOF_INCOMPLETE"})


def validate_evidence(packet):
    """Validate an intake packet independently of the live snapshot adapter."""
    errors = []
    if packet.get("contract") != CONTRACT:
        errors.append({"code": "UNSUPPORTED_CONTRACT"})
    if packet.get("database") != "garment_fyp_db":
        errors.append({"code": "UNEXPECTED_DATABASE"})
    for field in ("factory_id", "source_snapshot_hash"):
        if packet.get(field) in (None, ""):
            errors.append({"code": f"PACKET_{field.upper()}_MISSING"})
    try:
        as_of = _dt(packet.get("as_of"), "as_of")
    except ValueError as exc:
        errors.append({"code": "INVALID_PACKET_CLOCK", "detail": str(exc)})
        as_of = None
    operations = packet.get("operations") or []
    keys = [op.get("operation_key") for op in operations]
    if len(keys) != len(set(keys)):
        errors.append({"code": "DUPLICATE_OPERATION"})
    if as_of is not None:
        for operation in operations:
            _validate_operation(operation, as_of, errors)
    return {"status": "VALID" if not errors else "REJECTED", "valid": not errors,
            "errors": errors, "database_writes": 0,
            "craft_compatibility": "BLOCKED",
            "craft_compatibility_reason": "Current operation-level CRAFT interfaces do not represent residual quantity/lot continuation independently of the original operation.",
            "packet_hash": digest(packet) if not errors else None}


def normalized_packet(packet):
    """Canonicalize an accepted packet without adding execution facts."""
    result = deepcopy(packet)
    result["contract"] = CONTRACT
    normalized = []
    for operation in result.get("operations", []):
        operation["completed_lot_ids"] = sorted(operation.get("completed_lot_ids", []))
        operation["residual_lot_ids"] = sorted(operation.get("residual_lot_ids", []))
        operation["continuation_lots"] = sorted(operation.get("continuation_lots", []), key=lambda row: str(row.get("lot_id")))
        for commitment in operation.get("resource_commitments", []):
            commitment["segments"] = sorted(commitment.get("segments", []), key=lambda row: (str(row.get("start")), str(row.get("end"))))
        operation["resource_commitments"] = sorted(
            operation.get("resource_commitments", []),
            key=lambda row: (str(row.get("resource_type")), str(row.get("resource_id"))),
        )
        operation["resource_availability"] = {
            key: sorted(value, key=lambda row: (str(row.get("start")), str(row.get("end"))))
            for key, value in sorted(operation.get("resource_availability", {}).items())
        }
        normalized.append(operation)
    result["operations"] = sorted(normalized, key=lambda row: str(row.get("operation_key")))
    return result
