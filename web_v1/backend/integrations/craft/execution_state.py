"""Pure lot normalization. Generated future actuals are never commitments."""
from __future__ import annotations

import json
from collections import Counter

from .contracts import SOURCE_CONTRACT, iso, quantity, timestamp


def normalize_lot(row, reference_time, dataset):
    reference = timestamp(reference_time)
    errors = []
    start = end = None
    state = "UNRESOLVED"
    try:
        amount = quantity(row.get("lot_quantity"))
    except (ValueError, ArithmeticError):
        amount = None
        errors.append("INVALID_LOT_QUANTITY")
    try:
        start = timestamp(row.get("actual_start_time"), reference)
        end = timestamp(row.get("actual_end_time"), reference)
        if dataset.get("timeline_generation_contract") != SOURCE_CONTRACT:
            errors.append("UNSUPPORTED_TIMESTAMP_PROVENANCE")
        elif start is None:
            errors.append("MISSING_AUTHORITATIVE_START_EVIDENCE")
        elif end is not None and end < start:
            errors.append("END_PRECEDES_START")
        elif end is not None and end <= reference:
            state = "COMPLETED"
        elif start <= reference:
            state = "IN_FLIGHT"
        else:
            state = "NOT_STARTED"
        # Status is a consistency witness at the generation reference only.
        # It is not used to classify another supplied snapshot time.
        calibration_time = timestamp(dataset.get("reference_time"), reference)
        compatible = {"COMPLETED": {"Done"}, "IN_FLIGHT": {"Running", "Delayed"},
                      "NOT_STARTED": {"Scheduled"}}
        if calibration_time == reference and state in compatible:
            if row.get("execution_status") not in compatible[state]:
                errors.append("STATUS_TIMESTAMP_CONTRADICTION_AT_DATASET_REFERENCE")
    except (ValueError, TypeError):
        errors.append("INVALID_OR_MIXED_TIMESTAMP")

    observed_segments, future_segments = [], []
    try:
        raw = row.get("actual_work_segments")
        segments = json.loads(raw) if isinstance(raw, str) else raw or []
        previous_end = None
        for pair in segments:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("Invalid segment")
            left, right = timestamp(pair[0], reference), timestamp(pair[1], reference)
            if left is None or right is None or left >= right or start is None:
                raise ValueError("Invalid segment interval")
            if left < start or (end is not None and right > end) or (previous_end and left < previous_end):
                raise ValueError("Segments overlap or are outside execution bounds")
            previous_end = right
            if left < reference:
                observed_segments.append([iso(left), iso(min(right, reference))])
            if right > reference:
                future_segments.append([iso(max(left, reference)), iso(right)])
    except (ValueError, TypeError):
        errors.append("INVALID_EXECUTION_SEGMENTS")

    return {
        "lot_id": str(row.get("schedule_item_id")), "lot_index": row.get("lot_index"),
        "operation_key": f"{row.get('production_order_id')}_{row.get('operation_id')}",
        "source_status": row.get("execution_status"), "state": state,
        "quantity": amount, "machine_id": row.get("asset_id"),
        "worker_id": row.get("employee_id"), "line_id": row.get("line_id"),
        "batch_id": row.get("batch_id"),
        "authoritative_start": iso(start) if start and start <= reference else None,
        "observed_completion": iso(end) if state == "COMPLETED" else None,
        "projected_start": iso(start) if start and start > reference else None,
        "projected_end": iso(end) if end and end > reference else None,
        "observed_productive_segments": observed_segments,
        "projected_segments_not_commitments": future_segments,
        "committed_execution": [], "commitment_evidence": "NOT_STORED_IN_LEGACY_SCHEMA",
        "errors": sorted(set(errors)),
    }


def aggregate_operation(operation, order, lots, reference_time):
    reference = timestamp(reference_time)
    errors = [code for lot in lots for code in lot["errors"]]
    counts = Counter(lot["state"] for lot in lots)
    try:
        target = quantity(order.get("target_quantity"))
    except (ValueError, ArithmeticError):
        target = None
        errors.append("INVALID_ORDER_QUANTITY")
    if not lots:
        errors.append("MISSING_LOT_EXECUTION_EVIDENCE")
    if any(lot["quantity"] is None for lot in lots):
        total = completed = inflight = not_started = remaining = None
    else:
        total = sum(lot["quantity"] for lot in lots)
        completed = sum(lot["quantity"] for lot in lots if lot["state"] == "COMPLETED")
        inflight = sum(lot["quantity"] for lot in lots if lot["state"] == "IN_FLIGHT")
        not_started = sum(lot["quantity"] for lot in lots if lot["state"] == "NOT_STARTED")
        remaining = total - completed
        if total != target:
            errors.append("LOT_QUANTITY_DOES_NOT_MATCH_ORDER_TARGET")
    if counts["UNRESOLVED"] or errors:
        state = "UNRESOLVED_STATE"
    elif counts["COMPLETED"] == len(lots):
        state = "FULLY_COMPLETED"
    elif counts["NOT_STARTED"] == len(lots):
        state = "UNSTARTED"
    else:
        state = "PARTIALLY_EXECUTED"

    holds = []
    for lot in lots:
        if lot["state"] == "IN_FLIGHT":
            holds.append({
                "lot_id": lot["lot_id"], "operation_key": lot["operation_key"],
                "line_id": lot["line_id"], "machine_id": lot["machine_id"],
                "worker_id": lot["worker_id"], "started_at": lot["authoritative_start"],
                "hold_from": iso(reference), "release_at": None,
                "status": "UNRESOLVED_RELEASE_BOUNDARY",
                "note": "Retain resource association; this open-ended hold cannot be passed to CP-SAT.",
            })
    boundary_status = {"UNSTARTED": "NO_EXECUTION_BOUNDARY", "FULLY_COMPLETED": "HISTORICAL_FIXED"}.get(state, "BOUNDARY_UNRESOLVED")
    reasons = list(errors)
    if boundary_status == "BOUNDARY_UNRESOLVED":
        reasons.append("NO_AUTHORITATIVE_COMMITTED_RESIDUAL_PLAN")
    try:
        release = timestamp(order.get("scheduled_start_date"), reference)
    except (ValueError, TypeError):
        release = None
    if release is None:
        reasons.append("MISSING_OR_INVALID_ORDER_RELEASE")
    projected = [lot["projected_end"] for lot in lots if lot["projected_end"]]
    observed = [lot["observed_completion"] for lot in lots if lot["observed_completion"]]
    return {
        "operation_key": f"{operation['production_order_id']}_{operation['operation_id']}",
        "order_id": str(operation["production_order_id"]), "operation_id": str(operation["operation_id"]),
        "factory_id": int(order["factory_id"]), "routing_sequence": operation["operation_sequence_number"],
        "operation_name": operation.get("operation_name"), "execution_state": state,
        "optimizable": state == "UNSTARTED" and release is not None,
        "reason": sorted(set(reasons)) or ["NO_WORK_STARTED" if state == "UNSTARTED" else "ALL_LOTS_COMPLETED"],
        "release_time": iso(release), "future_release": release is not None and release > reference,
        "lot_counts": {key: counts[key] for key in ("COMPLETED", "IN_FLIGHT", "NOT_STARTED", "UNRESOLVED")},
        "quantity": {"target": target, "total": total, "completed": completed, "in_flight": inflight,
                     "not_started": not_started, "remaining": remaining,
                     "in_flight_completed_units": None, "remaining_productive_seconds": None,
                     "basis": "Whole completed lots; no intra-lot progress is inferred"},
        "lots": lots, "boundary_status": boundary_status,
        "resource_holds": holds, "committed_execution": [],
        "completion_boundary": max(observed) if state == "FULLY_COMPLETED" and observed else None,
        "projected_completion_not_committed": max(projected) if projected else None,
        "residual_lot_ids": [lot["lot_id"] for lot in lots if lot["state"] != "COMPLETED"],
        "completed_lot_ids": [lot["lot_id"] for lot in lots if lot["state"] == "COMPLETED"],
    }
