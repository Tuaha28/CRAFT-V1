"""Offline, source-authoritative admission audit for a legacy factory capture.

This module never opens a database or authorizes scheduling. A successful
boundary validation is necessary, but not sufficient, for resource readiness.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

from .boundary_validator import validate_snapshot
from .snapshot_adapter import normalize_snapshot


def audit_source(source, *, raw_sha256=None):
    snapshot = normalize_snapshot(source)
    boundary = validate_snapshot(snapshot, source)
    source_rows = {
        f"{row['production_order_id']}_{row['operation_id']}": row
        for row in source["operations"]
    }
    if len(source_rows) != len(source["operations"]):
        raise ValueError("Duplicate source operation identity")
    decisions = []
    for op in snapshot["operations"]:
        key = op["operation_key"]
        raw = source_rows[key]
        crew_values = [raw.get(field) for field in ("required_worker_count", "crew_size")
                       if raw.get(field) is not None]
        if len(set(map(str, crew_values))) > 1:
            status, reason = "BLOCKED", "CONFLICTING_CREW_REQUIREMENTS"
            crew_size = None
        elif not crew_values:
            crew_size = None
            status, reason = "REQUIRES_EVIDENCE", "MISSING_SOURCE_CREW_REQUIREMENT"
        else:
            try:
                crew_size = int(crew_values[0])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid crew requirement for {key}") from exc
            if crew_size < 1 or str(crew_values[0]) != str(crew_size):
                status, reason = "BLOCKED", "INVALID_CREW_REQUIREMENT"
            else:
                status, reason = "READY_FOR_PREPARATION", None
        if op["execution_state"] == "FULLY_COMPLETED":
            status, reason = "COMPLETED", None
        elif op["execution_state"] != "UNSTARTED":
            status, reason = "BLOCKED", "UNRESOLVED_EXECUTION_BOUNDARY"
        elif status == "READY_FOR_PREPARATION" and not boundary["valid_for_craft"]:
            status, reason = "BLOCKED", "SOURCE_BOUNDARY_VALIDATION_FAILED"
        decisions.append({"operation_key": key, "order_id": op["order_id"],
                          "execution_state": op["execution_state"],
                          "crew_size": crew_size, "status": status, "reason": reason})
    counts = Counter(row["status"] for row in decisions)
    reasons = Counter(row["reason"] for row in decisions if row["reason"])
    return {
        "contract_version": "CRAFT_SOURCE_READINESS_V1",
        "factory_id": source["factory_id"],
        "dataset": source["dataset"],
        "raw_file_sha256": raw_sha256,
        "normalized_input_hash": snapshot["input_hash"],
        "normalized_snapshot_hash": snapshot["snapshot_hash"],
        "operation_count": len(decisions),
        "counts": dict(sorted(counts.items())),
        "reasons": dict(sorted(reasons.items())),
        "boundary_valid_for_craft": boundary["valid_for_craft"],
        "boundary_error_codes": sorted({error["code"] for error in boundary["errors"]}),
        "crew_requirement_policy": "EXPLICIT_SOURCE_FIELD_ONLY; NO ONE_WORKER_DEFAULT",
        "decisions": decisions,
    }


def audit_capture(path, *, expected_sha256):
    """Verify the sealed raw bytes before parsing an existing offline capture."""
    if not expected_sha256 or len(expected_sha256) != 64:
        raise ValueError("A pinned SHA-256 is required")
    data = Path(path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256.lower():
        raise ValueError("Captured source SHA-256 mismatch")
    return audit_source(json.loads(data), raw_sha256=actual)
