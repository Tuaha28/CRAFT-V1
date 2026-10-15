"""Deterministic, conservative operation snapshots without scheduler imports."""
from __future__ import annotations

from collections import defaultdict

from .contracts import CONTRACT, BoundaryUnresolved, digest, iso, ordered_source, timestamp
from .execution_state import aggregate_operation, normalize_lot


def normalize_snapshot(source):
    source = ordered_source(source)
    reference = timestamp(source["reference_time"])
    order_index = {str(r["production_order_id"]): r for r in source["orders"]}
    grouped_lots = defaultdict(list)
    for row in source["lots"]:
        grouped_lots[f"{row['production_order_id']}_{row['operation_id']}"].append(row)
    operations = []
    for operation in source["operations"]:
        key = f"{operation['production_order_id']}_{operation['operation_id']}"
        order = order_index.get(str(operation["production_order_id"]), {
            "factory_id": source["factory_id"], "target_quantity": None,
        })
        lots = [normalize_lot(row, reference, source["dataset"]) for row in grouped_lots[key]]
        lots.sort(key=lambda r: (str(r["lot_index"]), r["lot_id"]))
        operations.append(aggregate_operation(operation, order, lots, reference))
    operations.sort(key=lambda r: (r["order_id"], r["routing_sequence"], r["operation_key"]))
    orders = defaultdict(list)
    for operation in operations:
        orders[operation["order_id"]].append(operation)
    edges = []
    for route in orders.values():
        prior = []
        for index, operation in enumerate(route):
            predecessor = route[index - 1] if index else None
            operation["predecessor"] = predecessor["operation_key"] if predecessor else None
            operation["successor"] = route[index + 1]["operation_key"] if index + 1 < len(route) else None
            if predecessor:
                edges.append({"predecessor": predecessor["operation_key"], "successor": operation["operation_key"]})
            blocked = [p["operation_key"] for p in prior if p["boundary_status"] == "BOUNDARY_UNRESOLVED"]
            bounds = [operation["release_time"]] if operation["release_time"] else []
            bounds += [p["completion_boundary"] for p in prior if p["completion_boundary"]]
            operation["precedence_boundary"] = {
                "unresolved_predecessors": blocked,
                "earliest_ready_lower_bound": max(bounds) if bounds and not blocked else None,
                "predecessor_decision_required": any(p["execution_state"] == "UNSTARTED" for p in prior),
                "ready_at_reference": (operation["optimizable"] and not operation["future_release"]
                                       and all(p["execution_state"] == "FULLY_COMPLETED" for p in prior)),
            }
            prior.append(operation)
    snapshot = {
        "contract_version": CONTRACT, "factory_id": source["factory_id"],
        "reference_time": iso(reference), "dataset": source["dataset"],
        "clock_basis": "DATASET_LOCAL_NAIVE" if reference.tzinfo is None else "EXPLICIT_OFFSET",
        "input_hash": digest(source), "orders": source["orders"], "operations": operations,
        "resources": source["resources"], "precedence": edges,
        "boundary_evidence": [{"operation_key": op["operation_key"], "status": op["boundary_status"],
                               "completion_boundary": op["completion_boundary"], "resource_holds": op["resource_holds"],
                               "residual_lot_ids": op["residual_lot_ids"], "committed_execution": op["committed_execution"]}
                              for op in operations if op["execution_state"] != "UNSTARTED"],
        "wip_evidence": source["wip"],
        "notes": ["WIP sums are corroborating evidence, not additional completed quantities.",
                  "Future simulated actuals are retained only as projections, never as commitments.",
                  "Optimizable does not mean ready or safe to prepare; independent validation is mandatory."],
    }
    snapshot["snapshot_hash"] = digest(snapshot)
    return snapshot


def craft_boundary_payload(snapshot, source):
    """Fail-closed handoff in the shape of existing WindowBoundaryState.

    This contract cannot authorize a partially executed residual plan. Current
    legacy input therefore stays blocked until that evidence exists. Do not
    drop unresolved operations to manufacture a smaller passing scope.
    """
    from .boundary_validator import validate_snapshot

    validation = validate_snapshot(snapshot, source)
    if not validation["valid_for_craft"]:
        raise BoundaryUnresolved(validation)
    origin = timestamp(snapshot["reference_time"])
    completed = [op for op in snapshot["operations"] if op["execution_state"] == "FULLY_COMPLETED"]
    return {
        "factory_id": snapshot["factory_id"],
        "committed_operation_keys": tuple(sorted(op["operation_key"] for op in completed)),
        "cross_boundary_operation_keys": (), "machine_commitments": (), "worker_commitments": (),
        "order_completion_state": {},
        "routing_completion_state": {
            op["operation_key"]: {"operation_key": op["operation_key"], "production_order_id": op["order_id"],
                                  "authoritative_end_seconds": int((timestamp(op["completion_boundary"], origin) - origin).total_seconds())}
            for op in completed},
        "source_repair_id": snapshot["snapshot_hash"],
    }
