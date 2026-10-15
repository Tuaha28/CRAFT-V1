"""Freeze the validated Factory-1 CRAFT workload without running a scheduler.

This is a read-only artifact builder.  It starts from the previously validated
Factory-1 state partition, removes completed history and order 790, and emits a
deterministic workload contract plus preflight evidence.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine


FACTORY_ID = 1
EXCLUDED_ORDER = "790"
DB_EXPECTED = "garment_fyp_db"
REFERENCE_EXPECTED = "2026-09-10T00:00:00"
AUDIT = Path("outputs/crafter/factory1_schedulable_workload_audit_v1")
OUT = Path("outputs/crafter/factory1_craft_workload_v1")
PROTECTED = {
    "factory2_craft": Path("outputs/crafter/factory2_172_production_v1_workerfix_full"),
    "evaluation_v2": Path("outputs/crafter/evaluation_v2"),
    "fifo": Path("outputs/crafter/baselines/fifo_factory2_v1"),
    "edd": Path("outputs/crafter/baselines/edd_factory2_v1"),
    "spt": Path("outputs/crafter/baselines/spt_factory2_v1"),
    "full_database_eda": Path("outputs/crafter/full_database_eda_v1"),
    "factory1_schedulable_audit": AUDIT,
}


def json_default(value: Any):
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=json_default).encode("utf-8")


def sha_values(values) -> str:
    return hashlib.sha256("\n".join(sorted(str(v) for v in values)).encode("utf-8")).hexdigest()


def sha_payload(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    if path.is_file():
        return file_sha(path)
    for child in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p.relative_to(path))):
        h.update(str(child.relative_to(path)).replace("\\", "/").encode())
        h.update(b"\0")
        h.update(bytes.fromhex(file_sha(child)))
    return h.hexdigest()


def write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=json_default) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def query_df(conn, sql: str) -> pd.DataFrame:
    return pd.read_sql(text(sql), conn)


def main():
    protected_before = {name: tree_hash(path) for name, path in PROTECTED.items()}

    partition = read_json(AUDIT / "factory1_operation_state_partition.json")
    order_partition = read_json(AUDIT / "factory1_order_state_partition.json")
    full_identity = read_json(AUDIT / "factory1_full_identity.json")
    contract = read_json(AUDIT / "factory1_schedulable_workload_contract.json")
    running_doc = read_json(AUDIT / "factory1_running_operations.json")
    released_doc = read_json(AUDIT / "factory1_released_incomplete.json")
    future_doc = read_json(AUDIT / "factory1_future_release.json")

    categories = {name: set(value["operation_keys"]) for name, value in partition["categories"].items()}
    all_historical = set(categories["COMPLETED_HISTORICAL"]) | set(categories["CURRENTLY_RUNNING"]) | set(categories["RELEASED_INCOMPLETE"]) | set(categories["FUTURE_RELEASE"]) | set(categories["UNRESOLVED_STATE"])
    excluded_keys = {key for key in all_historical if key.split("_", 1)[0] == EXCLUDED_ORDER}
    if len(excluded_keys) != 9:
        raise RuntimeError(f"Expected 9 order-790 operations, found {len(excluded_keys)}")

    records_by_key = {}
    for document in (running_doc, released_doc, future_doc):
        for record in document["operations"]:
            records_by_key[record["operation_key"]] = record

    # Read-only identity and reference-time checks against the canonical DB.
    with get_engine().connect() as conn:
        database = str(conn.execute(text("SELECT DATABASE()")).scalar_one())
        calibration_raw = conn.execute(text("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1")).scalar_one()
        calibration = json.loads(calibration_raw)
        routes = query_df(conn, """
            SELECT po.production_order_id, po.product_id,
                   ro.operation_id, ro.operation_sequence_number,
                   ro.operation_name, ro.standard_allowed_minutes,
                   ro.required_machine_type
            FROM Production_Orders po
            JOIN Routing_Operations ro ON ro.product_id=po.product_id
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(po.production_order_id AS CHAR)
            WHERE ofo.factory_id=1
            ORDER BY po.production_order_id, ro.operation_sequence_number, ro.operation_id
        """)
        orders_df = query_df(conn, """
            SELECT po.production_order_id, po.po_id, po.product_id,
                   po.target_quantity, po.scheduled_start_date,
                   po.scheduled_end_date, po.status AS order_status,
                   pu.delivery_deadline, pu.po_status
            FROM Production_Orders po
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(po.production_order_id AS CHAR)
            LEFT JOIN Purchase_Orders pu ON pu.po_id=po.po_id
            WHERE ofo.factory_id=1
            ORDER BY po.production_order_id
        """)
        order_790_running = query_df(conn, """
            SELECT j.line_id, j.asset_id, j.employee_id, j.operation_id,
                   j.schedule_item_id, j.lot_index, j.execution_status
            FROM Job_Shop_Schedules j
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(j.production_order_id AS CHAR)
            WHERE ofo.factory_id=1 AND j.production_order_id=790
              AND j.execution_status='Running'
            ORDER BY j.operation_id, j.schedule_item_id
        """)

    if database != DB_EXPECTED:
        raise RuntimeError(f"Connected database mismatch: {database!r}")
    if len(orders_df) != 336 or orders_df["production_order_id"].nunique() != 336:
        raise RuntimeError("Factory-1 order identity no longer matches validated audit")
    if len(routes) != 2879:
        raise RuntimeError("Factory-1 route identity no longer matches validated audit")

    route_rows = []
    route_by_order = defaultdict(list)
    for row in routes.to_dict(orient="records"):
        row["production_order_id"] = str(row["production_order_id"])
        row["operation_id"] = int(row["operation_id"])
        row["routing_sequence"] = int(row["operation_sequence_number"])
        row["operation_key"] = f"{row['production_order_id']}_{row['operation_id']}"
        route_rows.append(row)
        route_by_order[row["production_order_id"]].append(row)
    for rows in route_by_order.values():
        rows.sort(key=lambda r: (r["routing_sequence"], r["operation_id"]))

    full_route_keys = {r["operation_key"] for r in route_rows}
    if full_route_keys != all_historical:
        raise RuntimeError("Neutral route identity differs from audited operation partition")

    state_keys = (categories["CURRENTLY_RUNNING"] | categories["RELEASED_INCOMPLETE"] | categories["FUTURE_RELEASE"]) - excluded_keys
    optimizable_keys = (categories["RELEASED_INCOMPLETE"] | categories["FUTURE_RELEASE"]) - excluded_keys
    running_keys = categories["CURRENTLY_RUNNING"] - excluded_keys
    future_keys = categories["FUTURE_RELEASE"] - excluded_keys

    route_lookup = {r["operation_key"]: r for r in route_rows}
    predecessor = {}
    successor = {}
    precedence_edges = []
    precedence_blockers = []
    cross_order_edges = []
    for order_id, rows in sorted(route_by_order.items()):
        for index, row in enumerate(rows):
            key = row["operation_key"]
            pred = rows[index - 1]["operation_key"] if index else None
            succ = rows[index + 1]["operation_key"] if index + 1 < len(rows) else None
            predecessor[key] = pred
            successor[key] = succ
            if pred is not None:
                precedence_edges.append({"predecessor": pred, "successor": key, "order_id": order_id})
                if pred.split("_", 1)[0] != order_id:
                    cross_order_edges.append({"predecessor": pred, "successor": key, "order_id": order_id})

    for key in sorted(state_keys):
        pred = predecessor.get(key)
        succ = successor.get(key)
        if pred is not None and pred not in full_route_keys:
            precedence_blockers.append({"operation_key": key, "type": "MISSING_PREDECESSOR", "predecessor": pred})
        if succ is not None and succ not in full_route_keys:
            precedence_blockers.append({"operation_key": key, "type": "MISSING_SUCCESSOR", "successor": succ})
        if pred is not None and pred in excluded_keys:
            precedence_blockers.append({"operation_key": key, "type": "EXCLUDED_PREDECESSOR", "predecessor": pred})
        if succ is not None and succ in excluded_keys:
            precedence_blockers.append({"operation_key": key, "type": "EXCLUDED_SUCCESSOR", "successor": succ})

    resource_records = contract["resource_readiness"]["operation_resource_records"]
    # The validated audit stores this section as an object keyed by operation
    # key (rather than a list); normalize both forms for a stable artifact.
    if isinstance(resource_records, dict):
        resource_by_key = {}
        for key, value in resource_records.items():
            record = dict(value)
            record.setdefault("operation_key", str(key))
            resource_by_key[str(key)] = record
    else:
        resource_by_key = {record["operation_key"]: record for record in resource_records}

    def enrich(key: str) -> dict:
        base = dict(records_by_key[key])
        route = dict(route_lookup[key])
        base.update({
            "predecessor": predecessor.get(key),
            "successor": successor.get(key),
            "operation_name": route.get("operation_name"),
            "required_machine_type": route.get("required_machine_type"),
            "standard_allowed_minutes": route.get("standard_allowed_minutes"),
            "route_metadata": route,
            "resource_eligibility": resource_by_key.get(key),
        })
        return base

    state_records = [enrich(key) for key in sorted(state_keys)]
    optimizable_records = [enrich(key) for key in sorted(optimizable_keys)]
    future_records = [enrich(key) for key in sorted(future_keys)]
    running_records = []
    running_by_key = {record["operation_key"]: record for record in running_doc["operations"]}
    for key in sorted(running_keys):
        record = enrich(key)
        record["running_boundary"] = running_by_key[key]
        running_records.append(record)

    order_records = []
    order_records_by_id = {str(key): value for key, value in order_partition["records"].items()}
    for order_id in sorted(order_records_by_id, key=lambda value: (int(value), value)):
        if order_id == EXCLUDED_ORDER:
            continue
        order_records.append(order_records_by_id[order_id])

    # The reduced in-memory reservation view contains only the seven retained
    # running boundaries.  Order 790's two running reservations are explicitly
    # recorded as removed evidence, never carried into the workload.
    retained_reservations = []
    for record in running_records:
        boundary = record["running_boundary"]
        retained_reservations.append({
            "operation_key": record["operation_key"],
            "line_ids": boundary.get("running_line_ids", []),
            "machine_ids": boundary.get("running_machine_ids", []),
            "worker_ids": boundary.get("running_worker_ids", []),
        })
    removed_reservations = []
    for row in order_790_running.to_dict(orient="records"):
        removed_reservations.append({
            "operation_id": int(row["operation_id"]),
            "line_id": int(row["line_id"]) if pd.notna(row["line_id"]) else None,
            "machine_id": int(row["asset_id"]) if pd.notna(row["asset_id"]) else None,
            "worker_id": str(row["employee_id"]) if pd.notna(row["employee_id"]) else None,
            "schedule_item_id": int(row["schedule_item_id"]) if pd.notna(row["schedule_item_id"]) else None,
        })

    resource_state = {
        "factory_id": FACTORY_ID,
        "reference_time": calibration["reference_time"],
        "machine_semantics": "validated Factory-1 CRAFT reserve semantics",
        "worker_calendar_semantics": "validated three-shift / authoritative Work_Schedules semantics",
        "line_count": contract["resource_readiness"]["line_count"],
        "usable_machines": contract["resource_readiness"]["usable_machines_current_craft_semantics"],
        "assignable_workers": contract["resource_readiness"]["assignable_workers_current_craft_semantics"],
        "operation_resource_records": [resource_by_key[key] for key in sorted(state_keys)],
        "retained_running_reservations": retained_reservations,
        "removed_order_790_running_reservations": removed_reservations,
        "order_790_reservations_carried": False,
    }

    state_set = set(state_keys)
    optimizable_set = set(optimizable_keys)
    running_set = set(running_keys)
    future_set = set(future_keys)
    completed_set = categories["COMPLETED_HISTORICAL"]
    duplicate_checks = {
        "state": len(state_records) == len(state_set),
        "optimizable": len(optimizable_records) == len(optimizable_set),
        "running": len(running_records) == len(running_set),
        "orders": len(order_records) == len({r["order_id"] for r in order_records}),
    }
    zero_machine = [r["operation_key"] for r in resource_state["operation_resource_records"] if not r.get("eligible_machine_ids")]
    zero_worker = [r["operation_key"] for r in resource_state["operation_resource_records"] if not r.get("eligible_worker_ids")]
    completed_in_state = sorted(state_set & completed_set)
    order_790_anywhere = any(EXCLUDED_ORDER == str(r["order_id"]) for r in order_records) or any(EXCLUDED_ORDER == key.split("_", 1)[0] for key in state_set)
    missing_route = sorted(state_set - full_route_keys)
    invalid_calendars = int(contract["resource_readiness"].get("missing_calendar_worker_count", 0))
    missing_durations = list(contract["resource_readiness"].get("missing_duration_operations", []))
    negative_durations = list(contract["resource_readiness"].get("negative_duration_operations", []))
    missing_releases = int(contract["resource_readiness"].get("missing_release_order_count", 0))
    missing_due = int(contract["resource_readiness"].get("missing_due_order_count", 0))
    foreign_refs = contract["resource_readiness"].get("cross_factory_schedule_references", {})
    running_boundary_blockers = [
        {"operation_key": r["operation_key"], "missing": r["running_boundary"].get("missing_runtime_state_information", [])}
        for r in running_records
        if r["running_boundary"].get("missing_runtime_state_information")
    ]

    identity_hashes = {
        "order_ids_sha256": sha_values([r["order_id"] for r in order_records]),
        "state_operation_keys_sha256": sha_values(state_set),
        "optimizable_operation_keys_sha256": sha_values(optimizable_set),
        "running_operation_keys_sha256": sha_values(running_set),
        "future_release_operation_keys_sha256": sha_values(future_set),
        "resource_state_sha256": sha_payload(resource_state),
    }

    workload_contract = {
        "contract_version": "FACTORY1_CRAFT_WORKLOAD_V1",
        "scheduler_run": False,
        "database": database,
        "database_access": "READ_ONLY",
        "database_writes": 0,
        "factory_id": FACTORY_ID,
        "reference_time": calibration["reference_time"],
        "reference_time_source": "Dataset_Calibration.reference_time",
        "exclusion": {
            "completed_historical_operations": len(completed_set),
            "excluded_order_id": EXCLUDED_ORDER,
            "excluded_order_operation_count": len(excluded_keys),
            "excluded_order_reservations_carried": False,
        },
        "counts": {
            "orders": len(order_records),
            "state_operations": len(state_records),
            "optimizable_operations": len(optimizable_records),
            "running_operations": len(running_records),
            "future_release_operations": len(future_records),
        },
        "identity_hashes": identity_hashes,
        "physical_semantics": [
            "routing precedence and completed-prefix boundary",
            "factory/line ownership",
            "machine eligibility and validated machine reserve semantics",
            "worker skills/eligibility and authoritative Work_Schedules calendars",
            "fixed running resources and whole-operation commitment semantics",
            "half-open intervals [start,end)",
            "future-release operations remain release-constrained",
        ],
    }

    precedence_audit = {
        "factory_id": FACTORY_ID,
        "reference_time": calibration["reference_time"],
        "remaining_order_count": len(order_records),
        "state_operation_count": len(state_records),
        "precedence_edge_count_full_remaining_routes": sum(1 for edge in precedence_edges if edge["order_id"] != EXCLUDED_ORDER),
        "remaining_state_edges": sum(1 for edge in precedence_edges if edge["predecessor"] in state_set or edge["successor"] in state_set),
        "completed_boundary_edges": sum(1 for edge in precedence_edges if edge["predecessor"] in completed_set and edge["successor"] in state_set),
        "cross_order_edges": cross_order_edges,
        "missing_route_operations": missing_route,
        "excluded_order_edges_remaining": [e for e in precedence_edges if EXCLUDED_ORDER in (e["predecessor"].split("_", 1)[0], e["successor"].split("_", 1)[0]) and e["order_id"] != EXCLUDED_ORDER],
        "no_broken_precedence": not precedence_blockers,
        "no_orphan_successors": not precedence_blockers,
        "precedence_blockers": precedence_blockers,
    }

    structural_blockers = []
    if len(order_records) != 335: structural_blockers.append("ORDER_COUNT_MISMATCH")
    if len(state_records) != 2699: structural_blockers.append("STATE_OPERATION_COUNT_MISMATCH")
    if len(optimizable_records) != 2692: structural_blockers.append("OPTIMIZABLE_OPERATION_COUNT_MISMATCH")
    if len(running_records) != 7: structural_blockers.append("RUNNING_OPERATION_COUNT_MISMATCH")
    if order_790_anywhere: structural_blockers.append("ORDER_790_PRESENT")
    if not duplicate_checks["state"] or not duplicate_checks["optimizable"] or not duplicate_checks["running"] or not duplicate_checks["orders"]: structural_blockers.append("DUPLICATE_IDENTITY")
    if completed_in_state: structural_blockers.append("COMPLETED_OPERATION_OPTIMIZABLE")
    if missing_route or precedence_blockers or cross_order_edges: structural_blockers.append("PRECEDENCE_INTEGRITY")
    if zero_machine: structural_blockers.append("ZERO_MACHINE_ELIGIBILITY")
    if zero_worker: structural_blockers.append("ZERO_WORKER_ELIGIBILITY")
    if missing_durations or negative_durations: structural_blockers.append("INVALID_DURATION")
    if missing_releases or missing_due: structural_blockers.append("MISSING_RELEASE_OR_DUE")
    if invalid_calendars: structural_blockers.append("INVALID_WORKER_CALENDAR")
    if any(float(foreign_refs.get(k, 0) or 0) for k in ("foreign_line_rows", "foreign_machine_rows", "foreign_worker_rows")): structural_blockers.append("CROSS_FACTORY_RESOURCE_REFERENCE")
    if running_boundary_blockers: structural_blockers.append("RUNNING_BOUNDARY_INFORMATION")

    preflight = {
        "database": database,
        "reference_time": calibration["reference_time"],
        "database_read_only": True,
        "database_writes": 0,
        "scheduler_run": False,
        "counts": workload_contract["counts"],
        "order_790_present": order_790_anywhere,
        "order_790_reservations_present": False,
        "duplicate_checks": duplicate_checks,
        "completed_in_state": completed_in_state,
        "zero_machine_eligibility_operations": zero_machine,
        "zero_worker_eligibility_operations": zero_worker,
        "missing_duration_operations": missing_durations,
        "negative_duration_operations": negative_durations,
        "missing_release_order_count": missing_releases,
        "missing_due_order_count": missing_due,
        "invalid_worker_calendar_count": invalid_calendars,
        "cross_factory_resource_references": foreign_refs,
        "running_boundary_blockers": running_boundary_blockers,
        "precedence_valid": not precedence_blockers and not cross_order_edges and not missing_route,
        "resource_state_valid": not zero_machine and not zero_worker and not running_boundary_blockers,
        "structural_blockers": structural_blockers,
        "ready": not structural_blockers,
    }

    complete_payload = {
        "workload_contract": workload_contract,
        "orders": order_records,
        "state_operations": state_records,
        "optimizable_operations": optimizable_records,
        "running_boundary_operations": running_records,
        "future_release_operations": future_records,
        "resource_state": resource_state,
        "precedence_audit": precedence_audit,
        "preflight_validation": preflight,
    }
    workload_contract["identity_hashes"]["complete_workload_payload_sha256"] = sha_payload(complete_payload)

    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "workload_contract.json", workload_contract)
    write_json(OUT / "workload_identity.json", {"factory_id": FACTORY_ID, "database": database, "reference_time": calibration["reference_time"], "counts": workload_contract["counts"], "hashes": workload_contract["identity_hashes"]})
    write_json(OUT / "orders.json", {"factory_id": FACTORY_ID, "count": len(order_records), "orders": order_records})
    write_json(OUT / "state_operations.json", {"factory_id": FACTORY_ID, "count": len(state_records), "operations": state_records})
    write_json(OUT / "optimizable_operations.json", {"factory_id": FACTORY_ID, "count": len(optimizable_records), "operations": optimizable_records})
    write_json(OUT / "running_boundary_operations.json", {"factory_id": FACTORY_ID, "count": len(running_records), "operations": running_records})
    write_json(OUT / "future_release_operations.json", {"factory_id": FACTORY_ID, "count": len(future_records), "operations": future_records})
    write_json(OUT / "resource_state.json", resource_state)
    write_json(OUT / "precedence_audit.json", precedence_audit)
    write_json(OUT / "preflight_validation.json", preflight)

    output_manifest = {}
    for path in sorted(OUT.glob("*.json")):
        output_manifest[path.name] = file_sha(path)
    manifest = {
        "artifact_version": "FACTORY1_CRAFT_WORKLOAD_V1",
        "database": database,
        "database_writes": 0,
        "scheduler_run": False,
        "protected_artifacts_before": protected_before,
        "output_hashes": output_manifest,
    }
    write_json(OUT / "artifact_hash_manifest.json", manifest)

    protected_after = {name: tree_hash(path) for name, path in PROTECTED.items()}
    output_verified = all(file_sha(OUT / name) == digest for name, digest in output_manifest.items())
    protected_unchanged = protected_before == protected_after
    verification = {
        "all_hashes_verified": bool(output_verified and protected_unchanged),
        "output_hashes_verified": output_verified,
        "protected_artifacts_unchanged": protected_unchanged,
        "database_writes": 0,
        "scheduler_run": False,
        "protected_before": protected_before,
        "protected_after": protected_after,
    }
    write_json(OUT / "hash_verification.json", verification)

    print(json.dumps({
        "status": "FACTORY1_CRAFT_WORKLOAD_READY" if preflight["ready"] and verification["all_hashes_verified"] else "FACTORY1_CRAFT_WORKLOAD_BLOCKED",
        "database": database,
        "reference_time": calibration["reference_time"],
        "counts": workload_contract["counts"],
        "order_790_present": order_790_anywhere,
        "order_790_reservations_present": False,
        "zero_machine_eligibility_operations": zero_machine,
        "zero_worker_eligibility_operations": zero_worker,
        "running_boundary_blockers": running_boundary_blockers,
        "precedence_valid": preflight["precedence_valid"],
        "resource_state_valid": preflight["resource_state_valid"],
        "database_writes": 0,
        "protected_artifacts_unchanged": protected_unchanged,
        "artifact_hashes_verified": verification["all_hashes_verified"],
        "blockers": structural_blockers,
        "output": str(OUT),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
