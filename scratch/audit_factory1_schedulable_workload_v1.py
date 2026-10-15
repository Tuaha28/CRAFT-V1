"""Read-only Factory-1 current schedulable-workload partition audit.

This script does not import or invoke any scheduler.  It builds the complete
Factory-1 order/routing universe directly from the live canonical database,
then classifies operation state from authoritative operation-level schedule
statuses.  Planned/actual timestamps are retained as evidence only; they are
not used to infer completion.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from utils.machine_reserve import NON_USABLE_STATUSES, build_reserve
from utils.machine_taxonomy import machine_type_to_operation_type
from database.queries import fetch_assignable_operators


FACTORY_ID = 1
OUT = Path("outputs/crafter/factory1_schedulable_workload_audit_v1")
PROTECTED = {
    "factory2_craft": Path("outputs/crafter/factory2_172_production_v1_workerfix_full"),
    "evaluation_v2": Path("outputs/crafter/evaluation_v2"),
    "fifo": Path("outputs/crafter/baselines/fifo_factory2_v1"),
    "edd": Path("outputs/crafter/baselines/edd_factory2_v1"),
    "spt": Path("outputs/crafter/baselines/spt_factory2_v1"),
    "full_database_eda": Path("outputs/crafter/full_database_eda_v1"),
}


def _json_default(value: Any):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha_values(values) -> str:
    return hashlib.sha256("\n".join(sorted(str(v) for v in values)).encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hash(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "file_count": 0, "sha256": None, "files": {}}
    files = {}
    for item in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        files[item.relative_to(path).as_posix()] = sha_file(item)
    aggregate = hashlib.sha256()
    for name, digest in files.items():
        aggregate.update(name.encode("utf-8")); aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii")); aggregate.update(b"\n")
    return {"exists": True, "file_count": len(files), "sha256": aggregate.hexdigest(), "files": files}


def as_records(df: pd.DataFrame):
    return df.where(pd.notna(df), None).to_dict("records")


def stats(values):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None}
    return {"count": len(vals), "min": min(vals), "max": max(vals), "mean": statistics.fmean(vals), "median": statistics.median(vals)}


def _query(conn, sql):
    return pd.read_sql(text(sql), conn)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    protected_before = {name: tree_hash(path) for name, path in PROTECTED.items()}
    engine = get_engine()

    with engine.connect() as conn:
        db_name = str(conn.execute(text("SELECT DATABASE()")).scalar_one())
        calibration_raw = conn.execute(text("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1")).scalar_one()
        calibration = json.loads(calibration_raw)
        origin = datetime.fromisoformat(calibration["reference_time"])

        orders = _query(conn, """
            SELECT po.production_order_id, po.po_id, po.product_id,
                   po.target_quantity, po.scheduled_start_date,
                   po.scheduled_end_date, po.status AS order_status,
                   ofo.factory_id,
                   pu.delivery_deadline, pu.po_status
            FROM Production_Orders po
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(po.production_order_id AS CHAR)
            LEFT JOIN Purchase_Orders pu ON pu.po_id=po.po_id
            WHERE ofo.factory_id=1
            ORDER BY po.production_order_id
        """)
        routes = _query(conn, """
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
        schedules = _query(conn, """
            SELECT j.schedule_item_id, j.production_order_id, j.operation_id,
                   j.line_id, j.asset_id, j.employee_id, j.batch_id,
                   j.lot_index, j.lot_quantity, j.execution_status,
                   j.planned_start_time, j.planned_end_time,
                   j.actual_start_time, j.actual_end_time,
                   j.planned_work_segments, j.actual_work_segments
            FROM Job_Shop_Schedules j
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(j.production_order_id AS CHAR)
            WHERE ofo.factory_id=1
            ORDER BY j.production_order_id, j.operation_id, j.lot_index, j.schedule_item_id
        """)
        wip = _query(conn, """
            SELECT w.production_order_id, w.operation_id,
                   SUM(COALESCE(w.pieces_completed,0)) AS pieces_completed,
                   MAX(w.last_updated_time) AS last_updated_time,
                   COUNT(*) AS wip_rows
            FROM WIP_Tracking w
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(w.production_order_id AS CHAR)
            WHERE ofo.factory_id=1
            GROUP BY w.production_order_id,w.operation_id
        """)
        machines = _query(conn, """
            SELECT ma.asset_id, ma.factory_id, ma.line_id, ma.machine_model,
                   ma.current_status, mc.operation_type,
                   pl.is_active AS line_is_active, d.factory_id AS line_factory_id
            FROM Machinery_Assets ma
            LEFT JOIN Machine_Capabilities mc ON mc.asset_id=ma.asset_id
            LEFT JOIN Production_Lines pl ON pl.line_id=ma.line_id
            LEFT JOIN Departments d ON d.department_id=pl.department_id
            WHERE ma.factory_id=1
            ORDER BY ma.asset_id, mc.operation_type
        """)
        all_machines = _query(conn, """
            SELECT ma.asset_id, ma.factory_id, ma.line_id, ma.machine_model,
                   ma.current_status, mc.operation_type,
                   pl.is_active AS line_is_active, d.factory_id AS line_factory_id
            FROM Machinery_Assets ma
            LEFT JOIN Machine_Capabilities mc ON mc.asset_id=ma.asset_id
            LEFT JOIN Production_Lines pl ON pl.line_id=ma.line_id
            LEFT JOIN Departments d ON d.department_id=pl.department_id
            ORDER BY ma.asset_id, mc.operation_type
        """)
        lines = _query(conn, """
            SELECT pl.line_id, pl.is_active, d.factory_id
            FROM Production_Lines pl JOIN Departments d ON d.department_id=pl.department_id
            WHERE d.factory_id=1 ORDER BY pl.line_id
        """)
        capabilities = _query(conn, """
            SELECT ma.asset_id, ma.factory_id, ma.line_id, ma.current_status,
                   mc.operation_type, pl.is_active AS line_is_active,
                   d.factory_id AS line_factory_id
            FROM Machinery_Assets ma
            JOIN Machine_Capabilities mc ON mc.asset_id=ma.asset_id
            LEFT JOIN Production_Lines pl ON pl.line_id=ma.line_id
            LEFT JOIN Departments d ON d.department_id=pl.department_id
            WHERE ma.factory_id=1
        """)
        skills = _query(conn, """
            SELECT e.employee_id, e.factory_id, e.is_active,
                   osm.operation_type, ws.is_on_leave,
                   ws.shift_date, ws.start_time, ws.end_time,
                   eb.batch_id, eb.line_id AS batch_line_id,
                   eb.rotation_offset, eb.rest_day_offset
            FROM Employees e
            JOIN Operator_Skill_Matrix osm ON osm.employee_id=e.employee_id
            LEFT JOIN Work_Schedules ws ON ws.employee_id=e.employee_id
                AND ws.shift_date=(SELECT MAX(shift_date) FROM Work_Schedules)
            LEFT JOIN Batch_Members bm ON bm.employee_id=e.employee_id
            LEFT JOIN Employee_Batches eb ON eb.batch_id=bm.batch_id AND eb.is_active=1
            WHERE e.factory_id=1 AND e.is_active=1
        """)

    # Identity gate: this is the requested direct Production_Orders x Routing_Operations universe.
    orders["order_key"] = orders["production_order_id"].astype(str)
    routes["order_key"] = routes["production_order_id"].astype(str)
    routes["operation_key"] = routes["production_order_id"].astype(str) + "_" + routes["operation_id"].astype(str)
    if int(orders["order_key"].nunique()) != 336 or len(orders) != 336:
        raise RuntimeError(f"Factory-1 historical order identity mismatch: rows={len(orders)} unique={orders['order_key'].nunique()}")
    if len(routes) != 2879 or routes["operation_key"].nunique() != 2879:
        raise RuntimeError(f"Factory-1 historical operation identity mismatch: rows={len(routes)} unique={routes['operation_key'].nunique()}")
    if orders["order_key"].duplicated().any() or routes["operation_key"].duplicated().any():
        raise RuntimeError("Duplicate Factory-1 identity keys")

    order_map = {str(r.order_key): r._asdict() for r in orders.itertuples(index=False)}
    route_by_order = defaultdict(list)
    for r in routes.itertuples(index=False):
        route_by_order[str(r.order_key)].append(r)
    for key in route_by_order:
        route_by_order[key].sort(key=lambda r: (int(r.operation_sequence_number), int(r.operation_id)))

    # Aggregate operation-level authoritative execution evidence.  Actual/planned
    # timestamps are retained but never used to promote an operation to complete.
    sched_by_key = defaultdict(list)
    for r in schedules.itertuples(index=False):
        sched_by_key[f"{int(r.production_order_id)}_{int(r.operation_id)}"].append(r._asdict())
    wip_by_key = {f"{int(r.production_order_id)}_{int(r.operation_id)}": r._asdict() for r in wip.itertuples(index=False)}

    origin_date = origin.date()
    known_statuses = {"Done", "Running", "Scheduled", "Delayed", "Failed"}
    categories = {k: [] for k in ("COMPLETED_HISTORICAL", "CURRENTLY_RUNNING", "RELEASED_INCOMPLETE", "FUTURE_RELEASE", "UNRESOLVED_STATE")}
    op_records = {}
    for r in routes.itertuples(index=False):
        key = str(r.operation_key)
        order = order_map[str(r.order_key)]
        rows = sched_by_key.get(key, [])
        status_set = {str(x["execution_status"]) for x in rows}
        release = order.get("scheduled_start_date")
        future = release is None or release > origin_date
        if not rows or not status_set or not status_set.issubset(known_statuses):
            state = "UNRESOLVED_STATE"
            ambiguity = "missing operation schedule rows" if not rows else f"unknown execution statuses: {sorted(status_set - known_statuses)}"
        elif status_set == {"Done"}:
            state, ambiguity = "COMPLETED_HISTORICAL", None
        elif "Running" in status_set:
            state, ambiguity = "CURRENTLY_RUNNING", None
        elif future:
            state, ambiguity = "FUTURE_RELEASE", None
        elif status_set & {"Scheduled", "Delayed", "Failed", "Done"}:
            state, ambiguity = "RELEASED_INCOMPLETE", None
        else:
            state, ambiguity = "UNRESOLVED_STATE", f"unhandled status set: {sorted(status_set)}"
        categories[state].append(key)
        running_rows = [x for x in rows if str(x["execution_status"]) == "Running"]
        op_records[key] = {
            "operation_key": key,
            "order_id": str(r.order_key),
            "operation_id": int(r.operation_id),
            "routing_sequence": int(r.operation_sequence_number),
            "order_status": order["order_status"],
            "order_release_date": order["scheduled_start_date"],
            "order_due_date": order["delivery_deadline"],
            "state": state,
            "execution_statuses": sorted(status_set),
            "schedule_row_count": len(rows),
            "lot_status_counts": dict(sorted(Counter(str(x["execution_status"]) for x in rows).items())),
            "actual_start_evidence": min((x["actual_start_time"] for x in rows if x["actual_start_time"] is not None), default=None),
            "actual_end_evidence": max((x["actual_end_time"] for x in rows if x["actual_end_time"] is not None), default=None),
            "planned_start_evidence": min((x["planned_start_time"] for x in rows if x["planned_start_time"] is not None), default=None),
            "planned_end_evidence": max((x["planned_end_time"] for x in rows if x["planned_end_time"] is not None), default=None),
            "wip_evidence": wip_by_key.get(key),
            "classification_ambiguity": ambiguity,
            "classification_evidence": "operation-level Job_Shop_Schedules.execution_status; order scheduled_start_date compared with Dataset_Calibration.reference_time",
        }

    for vals in categories.values():
        vals.sort()
    completed = set(categories["COMPLETED_HISTORICAL"])
    running = set(categories["CURRENTLY_RUNNING"])
    released = set(categories["RELEASED_INCOMPLETE"])
    future = set(categories["FUTURE_RELEASE"])
    unresolved = set(categories["UNRESOLVED_STATE"])
    noncompleted = set(routes["operation_key"]) - completed

    # Routing precedence and current boundary.
    predecessor = {}
    successor = defaultdict(list)
    boundary_rows = []
    completed_prefix_violations = []
    currently_ready = []
    precedence_blocked = []
    for order_key, route in sorted(route_by_order.items()):
        seen_incomplete = False
        for i, r in enumerate(route):
            key = str(r.operation_key)
            pred = str(route[i - 1].operation_key) if i else None
            predecessor[key] = pred
            if pred:
                successor[pred].append(key)
            if key in completed:
                if seen_incomplete:
                    completed_prefix_violations.append(key)
            else:
                seen_incomplete = True
            if key in released:
                if pred is None or pred in completed:
                    currently_ready.append(key)
                else:
                    precedence_blocked.append(key)
                boundary_rows.append({
                    "operation_key": key, "order_id": order_key,
                    "predecessor": pred,
                    "predecessor_state": op_records[pred]["state"] if pred else None,
                    "operation_state": op_records[key]["state"],
                    "ready_at_current_origin": pred is None or pred in completed,
                })

    # Order-level partition derived from complete route state, not only order status.
    order_categories = {k: [] for k in ("FULLY_COMPLETED_HISTORICAL", "PARTIALLY_COMPLETED_WIP", "ACTIVE_NOT_STARTED", "FUTURE_RELEASE", "UNRESOLVED_STATE")}
    order_records = {}
    for order_key, order in sorted(order_map.items()):
        op_keys = [str(r.operation_key) for r in route_by_order[order_key]]
        states = {op_records[k]["state"] for k in op_keys}
        is_future = order["scheduled_start_date"] is None or order["scheduled_start_date"] > origin_date
        if states <= {"COMPLETED_HISTORICAL"}:
            state = "FULLY_COMPLETED_HISTORICAL"
        elif "UNRESOLVED_STATE" in states:
            state = "UNRESOLVED_STATE"
        elif "CURRENTLY_RUNNING" in states or ("COMPLETED_HISTORICAL" in states and states != {"COMPLETED_HISTORICAL"}):
            state = "PARTIALLY_COMPLETED_WIP"
        elif is_future:
            state = "FUTURE_RELEASE"
        else:
            state = "ACTIVE_NOT_STARTED"
        order_categories[state].append(order_key)
        order_records[order_key] = {
            "order_id": order_key,
            "status": order["order_status"],
            "release_date": order["scheduled_start_date"],
            "due_date": order["delivery_deadline"],
            "operation_count": len(op_keys),
            "operation_state_counts": dict(sorted(Counter(op_records[k]["state"] for k in op_keys).items())),
            "state": state,
        }
    for vals in order_categories.values():
        vals.sort()

    # Current CRAFT semantics for machines: deterministic company-wide reserve,
    # non-usable statuses excluded, and operation type mapping required.
    # Reserve membership is defined company-wide by machine capability.  Build
    # it from all assets first, then apply the resulting reserved IDs to F1.
    machine_for_reserve = all_machines[["asset_id", "factory_id", "line_id", "current_status", "operation_type"]].copy()
    machine_for_reserve = machine_for_reserve.dropna(subset=["operation_type"])
    reserved_ids, usable_cap_by_type = build_reserve(machine_for_reserve)
    factory_machine_ids = set(int(x) for x in machines["asset_id"].dropna().unique())
    usable_machine_ids = set()
    for r in machines.drop_duplicates("asset_id").itertuples(index=False):
        if int(r.asset_id) not in reserved_ids and str(r.current_status) not in NON_USABLE_STATUSES:
            usable_machine_ids.add(int(r.asset_id))

    assignable = fetch_assignable_operators()
    assignable = assignable[assignable["factory_id"].astype(int) == FACTORY_ID]
    assignable_worker_ids = set(str(x) for x in assignable["employee_id"].dropna().unique())
    skill_workers = defaultdict(set)
    for r in assignable.itertuples(index=False):
        skill_workers[str(r.operation_type).strip().lower()].add(str(r.employee_id))

    # Machine eligibility is capability + factory + active line + current
    # usable semantics.  Worker eligibility is active, present, skilled F1.
    machine_by_type = defaultdict(set)
    for r in capabilities.itertuples(index=False):
        if int(r.asset_id) not in usable_machine_ids:
            continue
        if r.line_factory_id is not None and int(r.line_factory_id) != FACTORY_ID:
            continue
        if r.line_is_active is not None and not bool(r.line_is_active):
            continue
        machine_by_type[str(r.operation_type).strip().lower()].add(int(r.asset_id))
    resource_records = {}
    zero_machine, zero_worker = [], []
    for key in sorted(noncompleted):
        op = op_records[key]
        route = next(r for r in routes.itertuples(index=False) if str(r.operation_key) == key)
        optype = machine_type_to_operation_type(route.required_machine_type)
        mids = sorted(machine_by_type.get(optype or "", set()))
        wids = sorted(skill_workers.get(optype or "", set()))
        resource_records[key] = {
            "operation_key": key,
            "required_machine_type": route.required_machine_type,
            "canonical_operation_type": optype,
            "eligible_machine_count": len(mids),
            "eligible_worker_count": len(wids),
            "eligible_machine_ids": mids,
            "eligible_worker_ids": wids,
        }
        if not mids: zero_machine.append(key)
        if not wids: zero_worker.append(key)

    # Cross-factory and structural data-quality checks.
    schedule_cross_factory = _query(engine.connect(), "SELECT 1") if False else None
    with engine.connect() as conn:
        cross = _query(conn, """
            SELECT COUNT(*) AS row_count,
                   COUNT(DISTINCT CONCAT(j.production_order_id,'_',j.operation_id)) AS operation_pairs,
                   SUM(CASE WHEN d.factory_id<>1 THEN 1 ELSE 0 END) AS foreign_line_rows,
                   SUM(CASE WHEN ma.factory_id<>1 THEN 1 ELSE 0 END) AS foreign_machine_rows,
                   SUM(CASE WHEN e.factory_id<>1 THEN 1 ELSE 0 END) AS foreign_worker_rows
            FROM Job_Shop_Schedules j
            JOIN Order_Factory_Ownership ofo ON CAST(ofo.production_order_id AS CHAR)=CAST(j.production_order_id AS CHAR)
            LEFT JOIN Production_Lines pl ON pl.line_id=j.line_id
            LEFT JOIN Departments d ON d.department_id=pl.department_id
            LEFT JOIN Machinery_Assets ma ON ma.asset_id=j.asset_id
            LEFT JOIN Employees e ON e.employee_id=j.employee_id
            WHERE ofo.factory_id=1
        """).iloc[0].to_dict()
        missing_calendar = _query(conn, """
            SELECT COUNT(DISTINCT e.employee_id) AS missing_calendar_workers
            FROM Employees e
            WHERE e.factory_id=1 AND e.is_active=1
              AND NOT EXISTS (SELECT 1 FROM Work_Schedules ws WHERE ws.employee_id=e.employee_id)
        """).iloc[0].to_dict()

    op_keys = [str(x) for x in routes["operation_key"]]
    all_hashes = {
        "all_historical_operation_keys_sha256": sha_values(op_keys),
        "completed_operation_keys_sha256": sha_values(completed),
        "running_operation_keys_sha256": sha_values(running),
        "released_incomplete_operation_keys_sha256": sha_values(released),
        "future_release_operation_keys_sha256": sha_values(future),
        "unresolved_operation_keys_sha256": sha_values(unresolved),
        "state_operation_keys_sha256": sha_values(noncompleted),
        "optimizable_operation_keys_sha256": sha_values(released | future),
        "all_historical_order_ids_sha256": sha_values(order_map),
    }

    # Reports intentionally preserve operation-level evidence without dumping
    # all schedule lot rows into the main partition artifact.
    running_records = [op_records[k] | {
        "running_line_ids": sorted({x["line_id"] for x in sched_by_key[k] if x["execution_status"] == "Running" and x["line_id"] is not None}),
        "running_machine_ids": sorted({x["asset_id"] for x in sched_by_key[k] if x["execution_status"] == "Running" and x["asset_id"] is not None}),
        "running_worker_ids": sorted({str(x["employee_id"]) for x in sched_by_key[k] if x["execution_status"] == "Running" and x["employee_id"] is not None}),
        "remaining_work_information": op_records[k].get("wip_evidence"),
        "remaining_work_status": "AVAILABLE_WIP_TRACKING" if op_records[k].get("wip_evidence") else "NOT_AVAILABLE",
        "missing_runtime_state_information": [] if op_records[k].get("wip_evidence") else ["WIP_Tracking row absent for this running operation"],
    } for k in categories["CURRENTLY_RUNNING"]]
    released_records = [op_records[k] | {"predecessor": predecessor.get(k), "precedence_ready": k in currently_ready} for k in categories["RELEASED_INCOMPLETE"]]
    future_records = [op_records[k] | {"predecessor": predecessor.get(k)} for k in categories["FUTURE_RELEASE"]]
    unresolved_records = [op_records[k] for k in categories["UNRESOLVED_STATE"]]

    structural_blockers = []
    if len(orders) != 336 or len(routes) != 2879: structural_blockers.append("IDENTITY_MISMATCH")
    if unresolved: structural_blockers.append("UNRESOLVED_OPERATION_STATE")
    if zero_machine: structural_blockers.append("ZERO_MACHINE_ELIGIBILITY")
    if zero_worker: structural_blockers.append("ZERO_WORKER_ELIGIBILITY")
    if int(missing_calendar["missing_calendar_workers"] or 0): structural_blockers.append("INVALID_WORKER_CALENDAR_COVERAGE")
    if int(cross["foreign_line_rows"] or 0) or int(cross["foreign_machine_rows"] or 0) or int(cross["foreign_worker_rows"] or 0): structural_blockers.append("CROSS_FACTORY_SCHEDULE_RESOURCE_REFERENCE")
    if completed_prefix_violations: structural_blockers.append("COMPLETED_PREFIX_PRECEDENCE_INCONSISTENCY")

    identity = {
        "database": db_name,
        "factory_id": FACTORY_ID,
        "reference_time": origin,
        "reference_time_source": "Dataset_Calibration.reference_time",
        "historical_order_count": len(orders),
        "historical_operation_count": len(routes),
        "operation_key_hashes": all_hashes,
        "order_id_sha256": all_hashes["all_historical_order_ids_sha256"],
        "identity_match_expected_336_2879": len(orders) == 336 and len(routes) == 2879,
        "database_access": "READ_ONLY",
        "database_writes": 0,
    }
    partition = {
        "audit_version": "FACTORY1_SCHEDULABLE_WORKLOAD_AUDIT_V1",
        "database": db_name, "factory_id": FACTORY_ID,
        "reference_time": origin, "reference_time_source": "Dataset_Calibration.reference_time",
        "classification_evidence_precedence": [
            "operation-level Job_Shop_Schedules.execution_status aggregate",
            "Done-only aggregate is completed historical",
            "Running present is currently running",
            "Production_Orders.scheduled_start_date after reference date is future release",
            "otherwise scheduled/delayed/failed non-done is released incomplete",
            "planned_start/end and actual_start/end are evidence only, not completion truth",
        ],
        "categories": {k: {"count": len(v), "operation_keys": v, "sha256": sha_values(v)} for k, v in categories.items()},
        "counts": {
            "FACTORY1_FULL_HISTORICAL_OPERATIONS": len(op_keys),
            "FACTORY1_COMPLETED_HISTORICAL_OPERATIONS": len(completed),
            "FACTORY1_CURRENTLY_RUNNING_OPERATIONS": len(running),
            "FACTORY1_RELEASED_INCOMPLETE_OPERATIONS": len(released),
            "FACTORY1_FUTURE_RELEASE_OPERATIONS": len(future),
            "FACTORY1_UNRESOLVED_OPERATIONS": len(unresolved),
            "FACTORY1_STATE_OPERATION_COUNT": len(noncompleted),
            "FACTORY1_OPTIMIZABLE_OPERATION_COUNT": len(released | future),
            "FACTORY1_CURRENT_SCHEDULABLE_OPERATION_COUNT": len(running | released | future),
            "FACTORY1_CURRENTLY_READY_OPERATIONS": len(currently_ready),
            "FACTORY1_PRECEDENCE_BLOCKED_OPERATIONS": len(precedence_blocked),
        },
        "order_status_source": "derived from operation partitions plus release date; Production_Orders.status retained as evidence",
        "database_access": "READ_ONLY", "database_writes": 0,
    }
    order_partition = {
        "factory_id": FACTORY_ID, "reference_time": origin,
        "categories": {k: {"count": len(v), "order_ids": v, "sha256": sha_values(v)} for k, v in order_categories.items()},
        "records": order_records,
        "counts": {k: len(v) for k, v in order_categories.items()},
    }
    precedence_audit = {
        "factory_id": FACTORY_ID, "reference_time": origin,
        "route_count": len(route_by_order),
        "precedence_edge_count": sum(max(0, len(v)-1) for v in route_by_order.values()),
        "completed_prefix_violations": sorted(completed_prefix_violations),
        "released_precedence_ready_count": len(currently_ready),
        "released_precedence_blocked_count": len(precedence_blocked),
        "boundary_rows": boundary_rows,
        "running_predecessor_count": sum(1 for k in precedence_blocked if predecessor.get(k) in running),
        "routing_inconsistency": bool(completed_prefix_violations),
        "precedence_semantics": "successor is ready only when its route predecessor is COMPLETED_HISTORICAL; running predecessor remains a fixed boundary",
    }
    resource_audit = {
        "factory_id": FACTORY_ID,
        "noncompleted_operation_count": len(noncompleted),
        "usable_machines_current_craft_semantics": len(usable_machine_ids),
        "assignable_workers_current_craft_semantics": len(assignable_worker_ids),
        "line_count": int(lines["line_id"].nunique()),
        "zero_machine_eligibility_operations": sorted(zero_machine),
        "zero_worker_eligibility_operations": sorted(zero_worker),
        "operation_resource_records": resource_records,
        "machine_reserve_count": len(reserved_ids & factory_machine_ids),
        "usable_cap_by_type": usable_cap_by_type,
        "missing_duration_operations": sorted(str(r.operation_key) for r in routes.itertuples(index=False) if r.standard_allowed_minutes is None),
        "negative_duration_operations": sorted(str(r.operation_key) for r in routes.itertuples(index=False) if r.standard_allowed_minutes is not None and float(r.standard_allowed_minutes) < 0),
        "zero_duration_operations": sorted(str(r.operation_key) for r in routes.itertuples(index=False) if r.standard_allowed_minutes is not None and float(r.standard_allowed_minutes) == 0),
        "missing_release_order_count": int(orders["scheduled_start_date"].isna().sum()),
        "missing_due_order_count": int(orders["delivery_deadline"].isna().sum()),
        "missing_calendar_worker_count": int(missing_calendar["missing_calendar_workers"] or 0),
        "cross_factory_schedule_references": cross,
    }
    contract = {
        "contract_version": "FACTORY1_CURRENT_CRAFT_WORKLOAD_V1",
        "database": db_name, "factory_id": FACTORY_ID, "reference_time": origin,
        "full_historical_operation_count": len(op_keys),
        "current_state_operation_count": len(noncompleted),
        "optimizable_operation_count": len(released | future),
        "current_schedulable_operation_count": len(running | released | future),
        "definition": {
            "current_schedulable": "CURRENTLY_RUNNING + RELEASED_INCOMPLETE + FUTURE_RELEASE",
            "state_operations": "all non-completed operations, including running fixed boundary work",
            "optimizable_operations": "RELEASED_INCOMPLETE + FUTURE_RELEASE; CURRENTLY_RUNNING is represented but immutable",
            "completed_operations": "excluded from future optimization; retained only as authoritative precedence/history boundary",
            "future_release": f"order scheduled_start_date > {origin.date().isoformat()} reference date",
        },
        "operation_key_hashes": all_hashes,
        "ready_operation_keys": sorted(currently_ready),
        "precedence_blocked_operation_keys": sorted(precedence_blocked),
        "resource_readiness": resource_audit,
        "structural_blockers": sorted(set(structural_blockers)),
        "safe_to_build_current_craft_workload": not structural_blockers,
        "database_access": "READ_ONLY", "database_writes": 0,
    }

    write_json(OUT / "factory1_full_identity.json", identity)
    write_json(OUT / "factory1_operation_state_partition.json", partition)
    write_json(OUT / "factory1_order_state_partition.json", order_partition)
    write_json(OUT / "factory1_running_operations.json", {"count": len(running_records), "operations": running_records})
    write_json(OUT / "factory1_released_incomplete.json", {"count": len(released_records), "operations": released_records})
    write_json(OUT / "factory1_future_release.json", {"count": len(future_records), "operations": future_records})
    write_json(OUT / "factory1_unresolved_states.json", {"count": len(unresolved_records), "operations": unresolved_records})
    write_json(OUT / "factory1_precedence_boundary_audit.json", precedence_audit)
    write_json(OUT / "factory1_schedulable_workload_contract.json", contract)

    summary = [
        "# Factory 1 schedulable-workload audit",
        "",
        f"Database: `{db_name}` (read-only; writes: 0)",
        f"Reference time: `{origin.isoformat()}` from `Dataset_Calibration.reference_time`.",
        "",
        "## Operation partition",
        "",
        f"- Historical universe: **{len(op_keys)}** operations across **{len(orders)}** orders.",
        f"- Completed historical: **{len(completed)}**.",
        f"- Currently running: **{len(running)}** (represented as fixed boundary state).",
        f"- Released incomplete: **{len(released)}** ({len(currently_ready)} precedence-ready; {len(precedence_blocked)} blocked).",
        f"- Future release: **{len(future)}**.",
        f"- Unresolved: **{len(unresolved)}**.",
        f"- Current state operations: **{len(noncompleted)}**.",
        f"- Optimizable operations: **{len(released | future)}** (running work excluded as immutable).",
        f"- Current schedulable workload contract: **{len(running | released | future)}**.",
        "",
        "## Order partition",
        "",
    ] + [f"- {name}: **{len(values)}** orders." for name, values in order_categories.items()] + [
        "",
        "## Boundary and resource checks",
        "",
        f"- Completed-prefix precedence violations: **{len(completed_prefix_violations)}**.",
        f"- Usable Factory-1 machines under current CRAFT semantics: **{len(usable_machine_ids)}**.",
        f"- Assignable Factory-1 workers: **{len(assignable_worker_ids)}**.",
        f"- Zero-machine operations in non-completed state: **{len(zero_machine)}**.",
        f"- Zero-worker operations in non-completed state: **{len(zero_worker)}**.",
        f"- Structural blockers: **{', '.join(sorted(set(structural_blockers))) if structural_blockers else 'none'}**.",
        "",
        "Planned and synthetic actual timestamps were retained as evidence only; operation state is classified from operation-level execution statuses and calibrated release origin.",
        "",
    ]
    (OUT / "factory1_schedulable_workload_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

    produced = {}
    for p in sorted(OUT.iterdir()):
        if p.is_file() and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"}:
            produced[p.name] = sha_file(p)
    manifest = {"output_directory": str(OUT), "files": produced, "database_writes": 0}
    write_json(OUT / "artifact_hash_manifest.json", manifest)
    protected_after = {name: tree_hash(path) for name, path in PROTECTED.items()}
    verification = {
        "database_writes": 0,
        "protected_artifacts_unchanged": protected_before == protected_after,
        "protected_before_tree_hashes": {k: v["sha256"] for k, v in protected_before.items()},
        "protected_after_tree_hashes": {k: v["sha256"] for k, v in protected_after.items()},
        "output_hashes_verified": all(sha_file(OUT / name) == digest for name, digest in produced.items()),
        "all_hashes_verified": protected_before == protected_after and all(sha_file(OUT / name) == digest for name, digest in produced.items()),
    }
    write_json(OUT / "hash_verification.json", verification)

    print(json.dumps({
        "STATUS": "FACTORY1_SCHEDULABLE_WORKLOAD_AUDIT_COMPLETE",
        "DATABASE": db_name,
        "DATABASE_READ_ONLY": "yes", "DATABASE_WRITES": 0,
        "FACTORY1_HISTORICAL_ORDERS": len(orders),
        "FACTORY1_FULL_HISTORICAL_OPERATIONS": len(routes),
        "FACTORY1_COMPLETED_HISTORICAL_OPERATIONS": len(completed),
        "FACTORY1_CURRENTLY_RUNNING_OPERATIONS": len(running),
        "FACTORY1_RELEASED_INCOMPLETE_OPERATIONS": len(released),
        "FACTORY1_FUTURE_RELEASE_OPERATIONS": len(future),
        "FACTORY1_UNRESOLVED_OPERATIONS": len(unresolved),
        "FACTORY1_STATE_OPERATION_COUNT": len(noncompleted),
        "FACTORY1_OPTIMIZABLE_OPERATION_COUNT": len(released | future),
        "FACTORY1_CURRENT_SCHEDULABLE_OPERATION_COUNT": len(running | released | future),
        "FACTORY1_CURRENTLY_READY_OPERATIONS": len(currently_ready),
        "FACTORY1_PRECEDENCE_BLOCKED_OPERATIONS": len(precedence_blocked),
        "FACTORY1_ORDER_PARTITION": {k: len(v) for k, v in order_categories.items()},
        "ZERO_MACHINE_ELIGIBILITY_OPERATIONS": len(zero_machine),
        "ZERO_WORKER_ELIGIBILITY_OPERATIONS": len(zero_worker),
        "STRUCTURAL_BLOCKERS": sorted(set(structural_blockers)),
        "CURRENT_CRAFT_WORKLOAD_SAFE_TO_BUILD": not structural_blockers,
        "EXISTING_ARTIFACTS_UNCHANGED": verification["protected_artifacts_unchanged"],
        "ARTIFACT_HASHES_VERIFIED": verification["all_hashes_verified"],
    }, default=_json_default, indent=2))


if __name__ == "__main__":
    main()
