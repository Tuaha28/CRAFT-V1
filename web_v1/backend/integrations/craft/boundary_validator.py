"""Independent source-to-snapshot checks. Does not call the normalizer.

Raw source records must be supplied separately. A self-consistent hash alone
is not execution evidence. Unknown completion boundaries fail admission.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json

from .contracts import CONTRACT, SOURCE_CONTRACT, digest, ordered_source, quantity, timestamp


def validate_snapshot(snapshot, source):
    errors = []

    def reject(code, key=None, detail=None):
        errors.append({"code": code, "operation_key": key, "detail": detail})

    source = ordered_source(source)
    if snapshot.get("contract_version") != CONTRACT:
        reject("UNSUPPORTED_BOUNDARY_CONTRACT")
    if source["dataset"].get("timeline_generation_contract") != SOURCE_CONTRACT:
        reject("UNSUPPORTED_TIMESTAMP_PROVENANCE")
    if snapshot.get("input_hash") != digest(source):
        reject("SOURCE_HASH_MISMATCH")
    if snapshot.get("snapshot_hash") != digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"}):
        reject("SNAPSHOT_HASH_MISMATCH")
    for field in ("reference_time", "factory_id", "dataset", "resources", "orders"):
        if snapshot.get(field) != source.get(field):
            reject("SOURCE_CONTEXT_CHANGED", detail=field)
    if snapshot.get("wip_evidence") != source.get("wip"):
        reject("WIP_EVIDENCE_CHANGED")
    reference = timestamp(source["reference_time"])
    source_ops = Counter(f"{r['production_order_id']}_{r['operation_id']}" for r in source["operations"])
    actual_ops = Counter(op.get("operation_key") for op in snapshot.get("operations", []))
    if actual_ops != source_ops or any(n != 1 for n in source_ops.values()):
        reject("OPERATION_IDENTITY_MISMATCH")
    order_ids = [str(r["production_order_id"]) for r in source["orders"]]
    if len(order_ids) != len(set(order_ids)):
        reject("DUPLICATE_ORDER")
    orders = {str(r["production_order_id"]): r for r in source["orders"]}
    machines = {r["asset_id"]: r for r in source["resources"].get("machines", [])}
    workers = {r["employee_id"]: r for r in source["resources"].get("workers", [])}
    lines = {r["line_id"]: r for r in source["resources"].get("lines", [])}
    groups = defaultdict(list)
    row_ids, physical_lot_ids = [], []
    for row in source["lots"]:
        key = f"{row['production_order_id']}_{row['operation_id']}"
        groups[key].append(row)
        row_ids.append(str(row.get("schedule_item_id")))
        physical_lot_ids.append((key, row.get("lot_index")))
        if key not in source_ops:
            reject("ORPHAN_LOT", key)
        if row.get("schedule_item_id") is None or row.get("lot_index") is None:
            reject("MISSING_LOT_IDENTITY", key)
    if len(set(row_ids)) != len(row_ids) or len(set(physical_lot_ids)) != len(physical_lot_ids):
        reject("DUPLICATE_LOT_IDENTITY")
    boundaries = {r["operation_key"]: r for r in snapshot.get("boundary_evidence", [])}
    if len(boundaries) != len(snapshot.get("boundary_evidence", [])):
        reject("DUPLICATE_EXECUTION_BOUNDARY")
    if not set(boundaries).issubset(source_ops):
        reject("ORPHAN_EXECUTION_BOUNDARY")
    normalized_ops = {op["operation_key"]: op for op in snapshot.get("operations", [])}
    raw_ops = {f"{r['production_order_id']}_{r['operation_id']}": r for r in source["operations"]}
    expected_states = {}
    completed_ends = {}
    for key, raw_op in raw_ops.items():
        op = normalized_ops.get(key)
        if op is None:
            continue
        rows = groups[key]
        order = orders.get(str(raw_op["production_order_id"]))
        if order is None or order["factory_id"] != source["factory_id"] or op.get("factory_id") != source["factory_id"]:
            reject("FACTORY_OR_ORDER_OWNERSHIP", key)
            continue
        if (op.get("order_id") != str(raw_op["production_order_id"]) or
            op.get("operation_id") != str(raw_op["operation_id"]) or
            op.get("routing_sequence") != raw_op["operation_sequence_number"]):
            reject("OPERATION_ROUTING_IDENTITY_CHANGED", key)
        evidence_states, amounts, ends = {}, {}, []
        for row in rows:
            lot_id = str(row["schedule_item_id"])
            try:
                amounts[lot_id] = quantity(row.get("lot_quantity"))
                start, end = timestamp(row.get("actual_start_time"), reference), timestamp(row.get("actual_end_time"), reference)
                if start is None or (end is not None and end < start):
                    raise ValueError("Invalid start/end")
                state = ("COMPLETED" if end is not None and end <= reference else
                         "IN_FLIGHT" if start <= reference else "NOT_STARTED")
                evidence_states[lot_id] = state
                if state == "COMPLETED":
                    ends.append(end.isoformat())
                if timestamp(source["dataset"].get("reference_time"), reference) == reference:
                    expected_statuses = {"COMPLETED": {"Done"}, "IN_FLIGHT": {"Running", "Delayed"}, "NOT_STARTED": {"Scheduled"}}
                    if row.get("execution_status") not in expected_statuses[state]:
                        reject("STATUS_TIMESTAMP_CONTRADICTION", key, lot_id)
                if state == "IN_FLIGHT":
                    for resource_id, index, code in ((row.get("asset_id"), machines, "MACHINE"),
                                                      (row.get("employee_id"), workers, "WORKER"),
                                                      (row.get("line_id"), lines, "LINE")):
                        if resource_id is None or resource_id not in index:
                            reject(f"MISSING_{code}_BOUNDARY_RESOURCE", key, lot_id)
                        elif index[resource_id]["factory_id"] != source["factory_id"]:
                            reject(f"FOREIGN_{code}_BOUNDARY_RESOURCE", key, lot_id)
                    machine = machines.get(row.get("asset_id"))
                    if machine and machine.get("line_id") != row.get("line_id"):
                        reject("MACHINE_LINE_MISMATCH", key, lot_id)
                    matches = [h for h in op.get("resource_holds", []) if h.get("lot_id") == lot_id]
                    if len(matches) != 1:
                        reject("IN_FLIGHT_RESOURCE_HOLD_MISSING_OR_DUPLICATE", key, lot_id)
                    else:
                        hold = matches[0]
                        for field, expected in (("machine_id", row.get("asset_id")), ("worker_id", row.get("employee_id")),
                                                ("line_id", row.get("line_id")), ("started_at", start.isoformat()),
                                                ("release_at", None), ("hold_from", reference.isoformat())):
                            if hold.get(field) != expected:
                                reject("RESOURCE_HOLD_CHANGED", key, {"lot": lot_id, "field": field})
            except (ValueError, TypeError, ArithmeticError):
                evidence_states[lot_id] = "UNRESOLVED"
                reject("UNRESOLVED_SOURCE_LOT", key, lot_id)
        normalized_lots = {lot["lot_id"]: lot for lot in op.get("lots", [])}
        if set(normalized_lots) != set(evidence_states) or len(op.get("lots", [])) != len(rows):
            reject("LOT_IDENTITY_MISMATCH", key)
        for lot_id, state in evidence_states.items():
            lot = normalized_lots.get(lot_id, {})
            raw = next(r for r in rows if str(r["schedule_item_id"]) == lot_id)
            if lot.get("state") != state or lot.get("quantity") != amounts.get(lot_id):
                reject("LOT_EVIDENCE_MISMATCH", key, lot_id)
            for field, source_field in (("machine_id", "asset_id"), ("worker_id", "employee_id"),
                                        ("line_id", "line_id"), ("source_status", "execution_status")):
                if lot.get(field) != raw.get(source_field):
                    reject("LOT_RESOURCE_OR_STATUS_CHANGED", key, lot_id)
            try:
                start = timestamp(raw.get("actual_start_time"), reference)
                end = timestamp(raw.get("actual_end_time"), reference)
                observed_start = start.isoformat() if start and start <= reference else None
                observed_end = end.isoformat() if state == "COMPLETED" and end else None
                if lot.get("authoritative_start") != observed_start or lot.get("observed_completion") != observed_end:
                    reject("OBSERVED_EXECUTION_CHANGED", key, lot_id)
                raw_segments = raw.get("actual_work_segments") or []
                raw_segments = json.loads(raw_segments) if isinstance(raw_segments, str) else raw_segments
                observed_segments, future_segments, previous = [], [], None
                for left_value, right_value in raw_segments:
                    left, right = timestamp(left_value, reference), timestamp(right_value, reference)
                    if not left or not right or not start or left >= right or left < start or (end and right > end) or (previous and left < previous):
                        raise ValueError("Invalid execution segment")
                    previous = right
                    if left < reference:
                        observed_segments.append([left.isoformat(), min(right, reference).isoformat()])
                    if right > reference:
                        future_segments.append([max(left, reference).isoformat(), right.isoformat()])
                if lot.get("observed_productive_segments") != observed_segments or lot.get("projected_segments_not_commitments") != future_segments:
                    reject("EXECUTION_SEGMENTS_CHANGED", key, lot_id)
            except (ValueError, TypeError):
                reject("INVALID_SOURCE_SEGMENTS_OR_CLOCK", key, lot_id)
            if lot.get("errors"):
                reject("NORMALIZED_LOT_ERRORS", key, {"lot": lot_id, "errors": lot["errors"]})
            if lot.get("committed_execution"):
                reject("UNPROVEN_LOT_COMMITMENT", key, lot_id)
        states = list(evidence_states.values())
        if not states or "UNRESOLVED" in states:
            expected_state = "UNRESOLVED_STATE"
        elif all(s == "COMPLETED" for s in states):
            expected_state = "FULLY_COMPLETED"
        elif all(s == "NOT_STARTED" for s in states):
            expected_state = "UNSTARTED"
        else:
            expected_state = "PARTIALLY_EXECUTED"
        expected_states[key] = expected_state
        if op.get("execution_state") != expected_state:
            reject("OPERATION_STATE_MISMATCH", key)
        if op.get("optimizable") and expected_state != "UNSTARTED":
            reject("STARTED_WORK_OPTIMIZABLE", key)
        counts = Counter(states)
        if any(op.get("lot_counts", {}).get(s) != counts[s] for s in ("COMPLETED", "IN_FLIGHT", "NOT_STARTED", "UNRESOLVED")):
            reject("LOT_COUNT_MISMATCH", key)
        try:
            target = quantity(order.get("target_quantity"))
            total = sum(amounts.values())
            completed = sum(amounts[lot] for lot, state in evidence_states.items() if state == "COMPLETED")
            inflight = sum(amounts[lot] for lot, state in evidence_states.items() if state == "IN_FLIGHT")
            unstarted = sum(amounts[lot] for lot, state in evidence_states.items() if state == "NOT_STARTED")
            expected_quantity = {"target": target, "total": total, "completed": completed,
                                 "in_flight": inflight, "not_started": unstarted, "remaining": total - completed}
            if total != target or any(op.get("quantity", {}).get(k) != v for k, v in expected_quantity.items()):
                reject("QUANTITY_ACCOUNTING_MISMATCH", key)
            if op.get("quantity", {}).get("remaining", 0) is not None and op["quantity"]["remaining"] < 0:
                reject("NEGATIVE_REMAINING_QUANTITY", key)
        except (ValueError, TypeError, ArithmeticError, KeyError):
            reject("UNRESOLVED_QUANTITY_ACCOUNTING", key)
        residual = sorted(lot for lot, state in evidence_states.items() if state != "COMPLETED")
        done = sorted(lot for lot, state in evidence_states.items() if state == "COMPLETED")
        if sorted(op.get("residual_lot_ids", [])) != residual or sorted(op.get("completed_lot_ids", [])) != done:
            reject("COMPLETED_OR_RESIDUAL_WORK_LOST", key)
        if op.get("committed_execution"):
            reject("UNPROVEN_OPERATION_COMMITMENT", key)
        if expected_state != "UNSTARTED":
            boundary = boundaries.get(key)
            if not boundary:
                reject("EXECUTION_BOUNDARY_MISSING", key)
            elif (boundary.get("resource_holds") != op.get("resource_holds") or
                  boundary.get("status") != op.get("boundary_status") or
                  sorted(boundary.get("residual_lot_ids", [])) != residual or
                  boundary.get("completion_boundary") != op.get("completion_boundary")):
                reject("EXECUTION_BOUNDARY_MISMATCH", key)
        if expected_state in ("PARTIALLY_EXECUTED", "UNRESOLVED_STATE"):
            if any(container.get(field) is not None
                   for container in (op, op.get("quantity", {}))
                   for field in ("remaining_productive_seconds", "in_flight_completed_units")):
                reject("UNPROVEN_RESIDUAL_PROGRESS", key)
            if op.get("execution_boundary_end") is not None:
                reject("UNPROVEN_RESIDUAL_COMPLETION", key)
            reject("BOUNDARY_UNRESOLVED", key, "No authoritative complete residual commitment plan")
            if op.get("completion_boundary") is not None or op.get("boundary_status") != "BOUNDARY_UNRESOLVED":
                reject("UNPROVEN_COMPLETION_BOUNDARY", key)
        if expected_state == "FULLY_COMPLETED" and ends:
            completed_ends[key] = max(ends)
            if op.get("completion_boundary") != max(ends):
                reject("COMPLETION_BOUNDARY_CHANGED", key)
        if expected_state == "UNSTARTED" and op.get("completion_boundary") is not None:
            reject("UNSTARTED_OPERATION_HAS_COMPLETION", key)
        try:
            release = timestamp(order.get("scheduled_start_date"), reference)
            if release is None or op.get("release_time") != release.isoformat():
                raise ValueError("Missing/changed release")
            if op.get("future_release") != (release > reference):
                reject("FUTURE_RELEASE_CHANGED", key)
        except (ValueError, TypeError):
            reject("INVALID_RELEASE", key)

    routes = defaultdict(list)
    for raw in source["operations"]:
        routes[str(raw["production_order_id"])].append(raw)
    expected_edges = []
    for route in routes.values():
        route.sort(key=lambda r: (r["operation_sequence_number"], str(r["operation_id"])))
        if len({r["operation_sequence_number"] for r in route}) != len(route):
            reject("DUPLICATE_ROUTING_SEQUENCE")
        prior = []
        for index, raw in enumerate(route):
            key = f"{raw['production_order_id']}_{raw['operation_id']}"
            op = normalized_ops.get(key, {})
            pred = prior[-1] if prior else None
            succ = f"{route[index+1]['production_order_id']}_{route[index+1]['operation_id']}" if index+1 < len(route) else None
            if op.get("predecessor") != pred or op.get("successor") != succ:
                reject("PRECEDENCE_IDENTITY_CHANGED", key)
            if pred:
                expected_edges.append({"predecessor": pred, "successor": key})
            unknown = [p for p in prior if expected_states.get(p) in ("PARTIALLY_EXECUTED", "UNRESOLVED_STATE")]
            gate = op.get("precedence_boundary", {})
            if gate.get("unresolved_predecessors") != unknown:
                reject("PREDECESSOR_BOUNDARY_LOST", key)
            bound = gate.get("earliest_ready_lower_bound")
            lower_bounds = [completed_ends[p] for p in prior if p in completed_ends]
            if op.get("release_time"):
                lower_bounds.append(op["release_time"])
            expected_bound = max(lower_bounds) if lower_bounds and not unknown else None
            if bound != expected_bound:
                reject("SUCCESSOR_READINESS_PRECEDES_OR_CHANGES_BOUNDARY", key)
            if gate.get("ready_at_reference") and (unknown or any(expected_states.get(p) != "FULLY_COMPLETED" for p in prior)
                                                    or op.get("future_release") or expected_states.get(key) != "UNSTARTED"):
                reject("UNREADY_SUCCESSOR_ADMITTED", key)
            prior.append(key)
    if sorted(snapshot.get("precedence", []), key=str) != sorted(expected_edges, key=str):
        reject("PRECEDENCE_EDGES_CHANGED")
    return {"status": "VALID" if not errors else "BLOCKED", "valid_for_craft": not errors,
            "errors": errors, "error_counts": dict(sorted(Counter(e["code"] for e in errors).items())),
            "unresolved_operation_keys": sorted({e["operation_key"] for e in errors if e["code"] == "BOUNDARY_UNRESOLVED"}),
            "database_writes": 0}
