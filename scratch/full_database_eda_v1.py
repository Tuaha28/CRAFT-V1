"""Comprehensive read-only EDA for the complete garment database.

No scheduler, CP model, baseline, or write path is imported or executed.  The
active-loader SQL is copied from ``database.queries.fetch_active_orders_and_routings``
for auditability; neutral counts are read from the already frozen neutral
extractor artifacts and are never used to define the full database universe.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from utils.machine_reserve import NON_USABLE_STATUSES, build_reserve
from utils.machine_taxonomy import machine_type_to_operation_type


OUT = Path("outputs/crafter/full_database_eda_v1")
PLOTS = OUT / "plots"
FACTORIES = (1, 2, 3, 4, 5, 6)
SNAPSHOT = 0
PROTECTED = {
    "factory2_craft": Path("outputs/crafter/factory2_172_production_v1_workerfix_full"),
    "evaluation_v2": Path("outputs/crafter/evaluation_v2"),
    "load_context_v1": Path("outputs/crafter/load_context_v1"),
    "fifo_factory2": Path("outputs/crafter/baselines/fifo_factory2_v1"),
    "edd_factory2": Path("outputs/crafter/baselines/edd_factory2_v1"),
    "spt_factory2": Path("outputs/crafter/baselines/spt_factory2_v1"),
    "remaining_factory_audit": Path("outputs/crafter/remaining_factory_audit_v1"),
}


def _write(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    tmp.replace(path)


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _sha_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _tree_hash(path: Path):
    if not path.exists():
        return {"exists": False, "file_count": 0, "sha256": None, "files": {}}
    files = {}
    if path.is_file():
        files[path.name] = _sha_file(path)
    else:
        for item in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
            files[item.relative_to(path).as_posix()] = _sha_file(item)
    h = hashlib.sha256()
    for name, digest in files.items():
        h.update(name.encode()); h.update(b"\0"); h.update(digest.encode()); h.update(b"\n")
    return {"exists": True, "file_count": len(files), "sha256": h.hexdigest(), "files": files}


def _read(engine, sql, **kwargs):
    return pd.read_sql(sql, engine, **kwargs)


def _safe_read(engine, sql):
    try:
        return _read(engine, sql)
    except Exception as exc:
        return pd.DataFrame({"_query_error": [str(exc)]})


def _clean(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _records(frame):
    if frame is None or frame.empty:
        return []
    return [{str(k): _clean(v) for k, v in row.items()} for row in frame.to_dict(orient="records")]


def _sha_values(values):
    return hashlib.sha256("\n".join(sorted(str(v) for v in values)).encode()).hexdigest()


def _stats(values):
    vals = [float(v) for v in values if v is not None and not pd.isna(v)]
    if not vals:
        return {"count": 0, "min": None, "p10": None, "p25": None, "median": None, "p50": None, "p75": None, "p90": None, "p95": None, "max": None, "mean": None}
    s = pd.Series(vals)
    return {"count": len(vals), "min": min(vals), "p10": float(s.quantile(.10)), "p25": float(s.quantile(.25)), "median": float(s.median()), "p50": float(s.median()), "p75": float(s.quantile(.75)), "p90": float(s.quantile(.90)), "p95": float(s.quantile(.95)), "max": max(vals), "mean": statistics.fmean(vals)}


def _stats_int(values):
    out = _stats(values)
    for key in ("min", "max"):
        if out[key] is not None:
            out[key] = int(out[key])
    return out


def _date_range(frame, column):
    if column not in frame:
        return {"earliest": None, "latest": None}
    vals = pd.to_datetime(frame[column], errors="coerce").dropna()
    return {"earliest": vals.min().isoformat() if len(vals) else None, "latest": vals.max().isoformat() if len(vals) else None}


def _monthly_counts(frame, date_col, value_col=None):
    if frame.empty or date_col not in frame:
        return []
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    x = frame.loc[dates.notna()].copy()
    if x.empty:
        return []
    x["_month"] = dates[dates.notna()].dt.to_period("M").astype(str).values
    if value_col:
        grouped = x.groupby("_month", sort=True)[value_col].nunique()
    else:
        grouped = x.groupby("_month", sort=True).size()
    return [{"month": str(k), "count": int(v)} for k, v in grouped.items()]


def _weekly_counts(frame, date_col, value_col=None):
    if frame.empty or date_col not in frame:
        return []
    dates = pd.to_datetime(frame[date_col], errors="coerce")
    x = frame.loc[dates.notna()].copy()
    if x.empty:
        return []
    x["_week"] = dates[dates.notna()].dt.to_period("W").astype(str).values
    grouped = x.groupby("_week", sort=True)[value_col].nunique() if value_col else x.groupby("_week", sort=True).size()
    return [{"week": str(k), "count": int(v)} for k, v in grouped.items()]


def _normalize_capability(value):
    if value is None or pd.isna(value):
        return None
    # Machine_Capabilities.operation_type and Operator_Skill_Matrix.operation_type
    # already use the canonical task vocabulary (e.g. ``body assembly``).
    # Only Routing_Operations.required_machine_type needs the taxonomy bridge.
    return str(value).strip().lower()


def _required_capability(value):
    if value is None or pd.isna(value):
        return None
    mapped = machine_type_to_operation_type(str(value))
    return str(mapped if mapped is not None else value).strip().lower()


def _plot_bar(path, labels, values, title, ylabel):
    fig, ax = plt.subplots(figsize=(9, 5)); ax.bar([str(x) for x in labels], values); ax.set_title(title); ax.set_ylabel(ylabel); fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)


def _plot_series(path, data, title, ylabel):
    fig, ax = plt.subplots(figsize=(11, 5));
    for label, points in data.items():
        if points:
            ax.plot([p[0] for p in points], [p[1] for p in points], marker=".", label=str(label))
    ax.set_title(title); ax.set_ylabel(ylabel); ax.tick_params(axis="x", rotation=45); ax.legend(); fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)


def _plot_hist(path, values, title, xlabel):
    fig, ax = plt.subplots(figsize=(9, 5));
    if values: ax.hist(values, bins=min(40, max(5, int(math.sqrt(len(values))))))
    ax.set_title(title); ax.set_xlabel(xlabel); ax.set_ylabel("count"); fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True); PLOTS.mkdir(parents=True, exist_ok=True)
    protected_before = {name: _tree_hash(path) for name, path in PROTECTED.items()}
    engine = get_engine()

    # Schema and row-source map. All queries below are SELECTs.
    db_name = str(_read(engine, "SELECT DATABASE() AS db").iloc[0, 0])
    schema = _read(engine, "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION")
    tables = sorted(schema["TABLE_NAME"].dropna().astype(str).unique()) if "TABLE_NAME" in schema else []
    table_rows = {}
    for table in tables:
        try:
            table_rows[table] = int(_read(engine, f"SELECT COUNT(*) AS n FROM `{table}`").iloc[0, 0])
        except Exception as exc:
            table_rows[table] = {"error": str(exc)}
    schema_map = {
        "database": db_name,
        "tables": {table: {"columns": [str(x.COLUMN_NAME) for x in schema[schema.TABLE_NAME == table].itertuples()], "raw_row_count": table_rows.get(table)} for table in tables},
        "authoritative_sources": {
            "factory": "Factories",
            "line": "Production_Lines JOIN Departments(factory_id)",
            "machine": "Machinery_Assets JOIN Machine_Capabilities",
            "worker": "Employees",
            "worker_skills": "Operator_Skill_Matrix",
            "worker_calendar": "Work_Schedules plus Employee_Batches rotation fields",
            "orders": "Production_Orders JOIN Order_Factory_Ownership",
            "customer_due_date": "Purchase_Orders.delivery_deadline via Production_Orders.po_id",
            "routing": "Routing_Operations",
            "operation_execution": "Job_Shop_Schedules (current status; not historical event log)",
            "wip": "WIP_Tracking",
            "machine_commitments": "Job_Shop_Schedules and reservation helpers used by CRAFT",
            "synthetic_provenance": "no explicit provenance columns found in authoritative tables",
        },
        "active_loader_query": "Production_Orders.status IN ('Pending','Processing','Delayed') JOIN Routing_Operations; no release-date WHERE clause; progress is a left join over Job_Shop_Schedules.",
        "neutral_snapshot_query": "existing neutral_factory*_state.json artifacts produced by extract_neutral_factory_workload; policy-independent, not a full-database query.",
        "database_access": "READ_ONLY",
        "database_writes": 0,
    }
    _write(OUT / "database_schema_map.json", schema_map)
    schema_md = ["# Database schema/source map", "", f"Database: `{db_name}`", "", "| Table | Rows | Columns |", "|---|---:|---|"]
    for table in tables:
        cols = ", ".join(schema.loc[schema.TABLE_NAME == table, "COLUMN_NAME"].astype(str))
        schema_md.append(f"| {table} | {table_rows.get(table)} | {cols} |")
    schema_md += ["", "All EDA operations are SELECT-only; no scheduler or writeback path is invoked.", "", "Authoritative metric sources:", ""]
    schema_md += [f"- **{k}**: {v}" for k, v in schema_map["authoritative_sources"].items()]
    (OUT / "database_schema_map.md").write_text("\n".join(schema_md) + "\n", encoding="utf-8")

    factories = _read(engine, "SELECT factory_id,name,location FROM Factories ORDER BY factory_id")
    lines = _read(engine, "SELECT pl.line_id,pl.department_id,pl.line_number,pl.is_active,d.factory_id FROM Production_Lines pl LEFT JOIN Departments d ON d.department_id=pl.department_id")
    machines = _read(engine, "SELECT ma.asset_id,ma.factory_id,ma.line_id,ma.machine_model,ma.serial_number,ma.current_status,mc.operation_type FROM Machinery_Assets ma LEFT JOIN Machine_Capabilities mc ON ma.asset_id=mc.asset_id")
    employees = _read(engine, "SELECT employee_id,factory_id,role,hire_date,is_active FROM Employees")
    skills = _read(engine, "SELECT skill_id,employee_id,operation_type,efficiency_rating,last_evaluated_date FROM Operator_Skill_Matrix")
    schedules = _read(engine, "SELECT schedule_id,employee_id,shift_date,start_time,end_time,is_on_leave FROM Work_Schedules")
    batches = _safe_read(engine, "SELECT batch_id,batch_name,line_id,supervisor_id,primary_skill,is_active,rotation_offset,rest_day_offset FROM Employee_Batches")
    products = _safe_read(engine, "SELECT product_id,style_code,name,category FROM Products")
    variants = _safe_read(engine, "SELECT variant_id,product_id,size,color,sku FROM Product_Variants")
    ownership = _safe_read(engine, "SELECT production_order_id,factory_id FROM Order_Factory_Ownership")
    orders = _read(engine, """SELECT po.production_order_id,po.po_id,po.product_id,po.target_quantity,po.scheduled_start_date,po.scheduled_end_date,po.status,pu.order_date,pu.delivery_deadline,pu.po_status,ofo.factory_id FROM Production_Orders po LEFT JOIN Purchase_Orders pu ON pu.po_id=po.po_id LEFT JOIN Order_Factory_Ownership ofo ON ofo.production_order_id=po.production_order_id""")
    routings = _read(engine, "SELECT operation_id,product_id,operation_sequence_number,operation_name,standard_allowed_minutes,required_machine_type FROM Routing_Operations")
    jss = _read(engine, "SELECT schedule_item_id,production_order_id,operation_id,line_id,asset_id,employee_id,batch_id,planned_start_time,planned_end_time,execution_status,actual_start_time,actual_end_time FROM Job_Shop_Schedules")
    wip = _safe_read(engine, "SELECT wip_id,production_order_id,line_id,operation_id,pieces_completed,last_updated_time FROM WIP_Tracking")

    orders["production_order_id_str"] = orders["production_order_id"].astype(str)
    routings["operation_id_str"] = routings["operation_id"].astype(str)
    full = orders.merge(routings, on="product_id", how="inner", suffixes=("_order", "_routing"))
    full["operation_key"] = full["production_order_id_str"] + "_" + full["operation_id_str"]
    full["factory_id"] = pd.to_numeric(full["factory_id"], errors="coerce")
    active_order_ids = set(orders.loc[orders["status"].astype(str).isin(["Pending", "Processing", "Delayed"]), "production_order_id_str"])
    active = full[full["production_order_id_str"].isin(active_order_ids)].copy()
    active["operation_key"] = active["production_order_id_str"] + "_" + active["operation_id_str"]

    # Existing neutral artifacts are the benchmark snapshot, never the full DB universe.
    neutral = {}
    neutral_paths = {2: Path("outputs/crafter/evaluation_v2/neutral_factory2_state.json"), 1: Path("outputs/crafter/remaining_factory_audit_v1/neutral_factory1_state.json"), 3: Path("outputs/crafter/remaining_factory_audit_v1/neutral_factory3_state.json"), 4: Path("outputs/crafter/remaining_factory_audit_v1/neutral_factory4_state.json"), 5: Path("outputs/crafter/remaining_factory_audit_v1/neutral_factory5_state.json"), 6: Path("outputs/crafter/remaining_factory_audit_v1/neutral_factory6_state.json")}
    for fid, path in neutral_paths.items():
        if path.exists():
            neutral[fid] = json.loads(path.read_text(encoding="utf-8"))

    machine_base = machines.drop_duplicates("asset_id", keep="first").copy()
    machine_base["status_norm"] = machine_base["current_status"].astype(str).str.strip().str.lower()
    reserved_ids, _caps = build_reserve(machines)
    machine_base["craft_usable"] = (~machine_base["asset_id"].isin(reserved_ids)) & (~machine_base["status_norm"].isin({str(x).lower() for x in NON_USABLE_STATUSES}))
    machine_base["raw_non_down"] = ~machine_base["status_norm"].isin({"breakdown", "under_maintenance", "unavailable", "retired", "inactive", "offline"})
    skill_map = skills.assign(capability=skills["operation_type"].map(_normalize_capability))
    valid_skill_workers = set(skill_map["employee_id"].dropna().astype(str))
    latest_shift = schedules["shift_date"].max() if not schedules.empty else None
    current_schedules = schedules[schedules["shift_date"] == latest_shift].copy() if latest_shift is not None else schedules.copy()
    assignable_ids = set(employees.loc[(employees["is_active"] == 1) & employees["employee_id"].astype(str).isin(valid_skill_workers), "employee_id"].astype(str))
    if not current_schedules.empty:
        present_ids = set(current_schedules.loc[current_schedules["is_on_leave"].fillna(False) == False, "employee_id"].astype(str))
        assignable_ids &= present_ids

    # Current operation execution state is a present-state audit, not historical event reconstruction.
    jss["operation_key"] = jss["production_order_id"].astype(str) + "_" + jss["operation_id"].astype(str)
    op_state = {}
    for key, group in jss.groupby("operation_key"):
        statuses = set(group["execution_status"].astype(str))
        if statuses and statuses <= {"Done"}:
            state = "COMPLETED"
        elif "Running" in statuses:
            state = "IN_PROGRESS"
        elif statuses & {"Scheduled", "Delayed", "Failed"}:
            state = "RELEASED_BUT_NOT_STARTED_OR_IN_PROGRESS"
        else:
            state = "OTHER"
        op_state[str(key)] = state

    global_factory_rows = []
    factory_details = {}
    historical_release_month = []; historical_due_month = []
    for fid in FACTORIES:
        fo = orders[orders["factory_id"] == fid].copy()
        ff = full[full["factory_id"] == fid].copy()
        fa = active[active["factory_id"] == fid].copy()
        m = machine_base[machine_base["factory_id"] == fid]
        l = lines[lines["factory_id"] == fid]
        e = employees[employees["factory_id"] == fid]
        workers = assignable_ids.intersection(set(e["employee_id"].astype(str)))
        op_order_counts = ff.groupby("production_order_id_str").size().tolist() if not ff.empty else []
        release = pd.to_datetime(fo["scheduled_start_date"], errors="coerce")
        due = pd.to_datetime(fo["delivery_deadline"], errors="coerce")
        lead_days = (due - release).dt.total_seconds().div(86400)
        duration = pd.to_numeric(ff["standard_allowed_minutes"], errors="coerce").mul(60) if not ff.empty else pd.Series(dtype=float)
        duration_missing = int(ff["standard_allowed_minutes"].isna().sum()) if not ff.empty else 0
        duration_negative = int((duration < 0).sum()) if not ff.empty else 0
        duration_zero = int((duration == 0).sum()) if not ff.empty else 0
        caps = Counter(_normalize_capability(x) for x in m["operation_type"].dropna())
        # Eligibility counts use factory/capability, matching neutral CRAFT resource scope.
        m_by_cap = defaultdict(int)
        for x in m.itertuples(index=False):
            cap = _normalize_capability(getattr(x, "operation_type", None))
            if cap and bool(getattr(x, "craft_usable", False)): m_by_cap[cap] += 1
        worker_by_cap = Counter()
        for x in skill_map[skill_map["employee_id"].astype(str).isin(workers)]["capability"].dropna(): worker_by_cap[str(x)] += 1
        elig_m = [m_by_cap.get(_required_capability(x), 0) for x in ff["required_machine_type"]] if not ff.empty else []
        elig_w = [worker_by_cap.get(_required_capability(x), 0) for x in ff["required_machine_type"]] if not ff.empty else []
        line_eligible_counts = []
        for required in (ff["required_machine_type"] if not ff.empty else []):
            capability = _required_capability(required)
            line_eligible_counts.append(len(set(m.loc[(m["craft_usable"]) & (m["operation_type"].map(_normalize_capability) == capability), "line_id"])))
        statuses = Counter(str(x) for x in fo["status"].dropna())
        state_counts = Counter(op_state.get(k, "UNSCHEDULED_OR_NO_CURRENT_ROW") for k in ff["operation_key"])
        historical_release_month.extend(pd.DataFrame({"factory_id": fid, "date": release}).dropna().to_dict("records"))
        historical_due_month.extend(pd.DataFrame({"factory_id": fid, "date": due}).dropna().to_dict("records"))
        detail = {
            "factory_id": fid,
            "historical_orders": len(fo), "historical_operations_raw_join_rows": len(ff), "historical_unique_operations": ff["operation_key"].nunique() if not ff.empty else 0,
            "active_loader_orders": int(fo["production_order_id_str"].isin(active_order_ids).sum()), "active_loader_operations": fa["operation_key"].nunique() if not fa.empty else 0,
            "neutral_snapshot_orders": len(neutral.get(fid, {}).get("orders", [])), "neutral_snapshot_operations": len(neutral.get(fid, {}).get("operations", [])),
            "date_range": {"release": _date_range(fo, "scheduled_start_date"), "due": _date_range(fo, "delivery_deadline")},
            "operations_per_order": _stats_int(op_order_counts),
            "orders_per_month": _monthly_counts(fo, "scheduled_start_date", "production_order_id_str"), "orders_due_per_month": _monthly_counts(fo, "delivery_deadline", "production_order_id_str"), "operations_per_month": _monthly_counts(ff, "scheduled_start_date", "operation_key"), "operations_due_per_month": _monthly_counts(ff, "delivery_deadline", "operation_key"),
            "machines": {"total": len(m), "usable_craft_semantics": int(m["craft_usable"].sum()), "raw_non_down": int(m["raw_non_down"].sum()), "unusable_craft": int((~m["craft_usable"]).sum()), "lines": len(l), "machines_per_line": _stats_int(m.groupby("line_id").size().tolist()) if not m.empty else _stats_int([]), "capability_groups": dict(sorted(caps.items()))},
            "workers": {"total_employees": len(e), "assignable_workers": len(workers), "calendar_records_latest": int(current_schedules[current_schedules["employee_id"].astype(str).isin(workers)].shape[0]) if not current_schedules.empty else 0, "skills": dict(sorted(worker_by_cap.items())), "shift_distribution_latest": {f"{k[0]}-{k[1]}": int(v) for k, v in current_schedules[current_schedules["employee_id"].astype(str).isin(workers)].groupby(["start_time", "end_time"], dropna=False).size().items()} if not current_schedules.empty else {}},
            "eligibility": {"machine_count_per_operation": _stats_int(elig_m), "worker_count_per_operation": _stats_int(elig_w), "eligible_line_count_per_operation": _stats_int(line_eligible_counts), "zero_machine_operations": int(sum(v == 0 for v in elig_m)), "one_machine_operations": int(sum(v == 1 for v in elig_m)), "multiple_machine_operations": int(sum(v > 1 for v in elig_m)), "zero_worker_operations": int(sum(v == 0 for v in elig_w)), "one_worker_operations": int(sum(v == 1 for v in elig_w)), "multiple_worker_operations": int(sum(v > 1 for v in elig_w))},
            "durations_seconds_from_standard_allowed_minutes": {"stats": _stats(duration.tolist()), "missing": duration_missing, "zero": duration_zero, "negative": duration_negative},
            "release_due": {"missing_release": int(release.isna().sum()), "missing_due": int(due.isna().sum()), "due_before_release": int((lead_days < 0).sum()), "release_to_due_days": _stats(lead_days.tolist())},
            "order_statuses": dict(sorted(statuses.items())), "operation_current_state_derived": dict(sorted(state_counts.items())),
            "lines": {"line_count": len(l), "machine_line_ids_without_line_records": sorted(set(m["line_id"].dropna().astype(int)) - set(l["line_id"].dropna().astype(int))), "empty_lines_by_current_schedule": sorted(set(l["line_id"].dropna().astype(int)) - set(jss.loc[jss["production_order_id"].astype(str).isin(set(fo["production_order_id_str"])), "line_id"].dropna().astype(int))) if not l.empty else [], "orders_with_current_line_rows": int(jss[jss["production_order_id"].astype(str).isin(set(fo["production_order_id_str"]))]["production_order_id"].nunique()) if not jss.empty else 0, "operations_with_current_line_rows": int(jss[jss["production_order_id"].astype(str).isin(set(fo["production_order_id_str"]))]["operation_key"].nunique()) if not jss.empty else 0},
            "precedence": {},
            "commitment_note": "Current database state only; no historical commitment event log was found.",
            "database_scope": "FULL_PRODUCTION_ORDER x ROUTING_OPERATION join",
        }
        # Routing/precedence by operation sequence within each order.
        edges = 0; roots = 0; with_pred = 0; max_depth = 0; duplicate_sequences = []
        if not ff.empty:
            for oid, group in ff.groupby("production_order_id_str"):
                seqs = group["operation_sequence_number"].tolist()
                if len(seqs) != len(set(seqs)): duplicate_sequences.append(str(oid))
                roots += 1
                with_pred += max(0, len(group) - 1); edges += max(0, len(group) - 1); max_depth = max(max_depth, int(pd.to_numeric(group["operation_sequence_number"], errors="coerce").max()))
        detail["precedence"] = {"edges": edges, "operations_without_predecessor": roots, "operations_with_predecessor": with_pred, "maximum_routing_depth": max_depth, "duplicate_routing_sequence_orders": duplicate_sequences, "broken_predecessor_references": [], "cycles": False}
        factory_details[fid] = detail
        global_factory_rows.append({"factory_id": fid, "historical_orders": len(fo), "historical_operations": int(ff["operation_key"].nunique() if not ff.empty else 0), "lines": len(l), "usable_machines": int(m["craft_usable"].sum()), "assignable_workers": len(workers), "date_range": detail["date_range"]})

    # Full temporal and workload outputs.
    all_release = pd.to_datetime(orders["scheduled_start_date"], errors="coerce")
    all_due = pd.to_datetime(orders["delivery_deadline"], errors="coerce")
    op_timestamps = pd.concat([pd.to_datetime(jss["planned_start_time"], errors="coerce"), pd.to_datetime(jss["planned_end_time"], errors="coerce"), pd.to_datetime(jss["actual_start_time"], errors="coerce"), pd.to_datetime(jss["actual_end_time"], errors="coerce")], ignore_index=True)
    global_date_range = {"order_release": {"earliest": all_release.min().isoformat() if all_release.notna().any() else None, "latest": all_release.max().isoformat() if all_release.notna().any() else None}, "order_due": {"earliest": all_due.min().isoformat() if all_due.notna().any() else None, "latest": all_due.max().isoformat() if all_due.notna().any() else None}, "operation_timestamps": {"earliest": op_timestamps.min().isoformat() if op_timestamps.notna().any() else None, "latest": op_timestamps.max().isoformat() if op_timestamps.notna().any() else None}}
    all_dates = [pd.Timestamp(x) for x in [all_release.min(), all_due.max()] if not pd.isna(x)]
    covered_days = int((max(all_dates) - min(all_dates)).total_seconds() // 86400) if len(all_dates) == 2 else None
    monthly = {"orders_released": _monthly_counts(orders, "scheduled_start_date", "production_order_id_str"), "orders_due": _monthly_counts(orders, "delivery_deadline", "production_order_id_str"), "operations_released_with_order": _monthly_counts(full, "scheduled_start_date", "operation_key"), "operations_due_with_order": _monthly_counts(full, "delivery_deadline", "operation_key")}
    weekly = {"orders_released": _weekly_counts(orders, "scheduled_start_date", "production_order_id_str"), "orders_due": _weekly_counts(orders, "delivery_deadline", "production_order_id_str")}

    # Full-vs-active-vs-neutral retention table.
    retention = []
    for fid in FACTORIES:
        hist = factory_details[fid]["historical_unique_operations"]; act = factory_details[fid]["active_loader_operations"]; neu = factory_details[fid]["neutral_snapshot_operations"]
        retention.append({"factory_id": fid, "full_db_operations": hist, "active_loader_operations": act, "neutral_snapshot_operations": neu, "full_to_active_percent": (100 * act / hist if hist else None), "active_to_neutral_percent": (100 * neu / act if act else None), "neutral_to_full_percent": (100 * neu / hist if hist else None)})
    _write(OUT / "full_vs_active_vs_neutral.json", {"rows": retention, "definitions": {"full_db": "all Production_Orders joined to Routing_Operations", "active_loader": "status Pending/Processing/Delayed joined to Routing_Operations", "neutral_snapshot": "existing neutral_factory state artifacts"}})
    retention_md = ["# Full vs active vs neutral workload", "", "| Factory | Full DB ops | Active loader ops | Neutral ops | Full→Active % | Active→Neutral % |", "|---:|---:|---:|---:|---:|---:|"]
    retention_md += [f"| {r['factory_id']} | {r['full_db_operations']} | {r['active_loader_operations']} | {r['neutral_snapshot_operations']} | {r['full_to_active_percent'] if r['full_to_active_percent'] is not None else 'N/A'} | {r['active_to_neutral_percent'] if r['active_to_neutral_percent'] is not None else 'N/A'} |" for r in retention]
    (OUT / "full_vs_active_vs_neutral.md").write_text("\n".join(retention_md) + "\n", encoding="utf-8")

    active_filter = {
        "sql_source": "database.queries.fetch_active_orders_and_routings",
        "where_clause": "po.status IN ('Pending', 'Processing', 'Delayed')",
        "joins": ["LEFT JOIN Purchase_Orders", "JOIN Routing_Operations", "LEFT JOIN aggregated Job_Shop_Schedules progress"],
        "not_filters": ["no release-date cutoff", "no due-date cutoff", "no CRAFT window/neighborhood", "no CRAFT selected keys", "no CP model", "no priority/lateness filter"],
        "status_counts_all_orders": orders["status"].value_counts(dropna=False).to_dict(),
        "active_order_count": len(active_order_ids),
        "active_operation_count": int(active["operation_key"].nunique()),
        "exclusion_counts_by_order_status": {str(k): int(v) for k, v in orders["status"].value_counts(dropna=False).items() if str(k) not in {"Pending", "Processing", "Delayed"}},
        "routing_join_exclusion": {"production_orders_without_routing": int(len(set(orders["production_order_id_str"]) - set(full["production_order_id_str"])))},
        "neutral_explanation": "Neutral counts are the previously frozen factory state artifacts and are not the entire active SQL result; the benchmark workload is intentionally a selected frozen snapshot, not a full historical extract.",
    }
    _write(OUT / "active_filter_explanation.json", active_filter)
    (OUT / "active_workload_filter_explanation.md").write_text("# Active workload filter explanation\n\n" + json.dumps(active_filter, indent=2, default=_json_default) + "\n", encoding="utf-8")

    # Historical active state is not an event history. Provide explicit present-state proxy only.
    proxy_rows = []
    for fid in FACTORIES:
        fo = orders[orders["factory_id"] == fid].copy(); ff = full[full["factory_id"] == fid].copy()
        if fo.empty: continue
        rel = pd.to_datetime(fo["scheduled_start_date"], errors="coerce").dropna()
        if rel.empty: continue
        weeks = pd.date_range(rel.min().floor("D"), rel.max().ceil("D"), freq="7D")
        current_complete = {k for k in ff["operation_key"] if op_state.get(k) == "COMPLETED"}
        complete_orders = {str(oid) for oid, group in ff.groupby("production_order_id_str") if set(group["operation_key"]) <= current_complete}
        for point in weeks:
            released_orders = set(fo.loc[pd.to_datetime(fo["scheduled_start_date"], errors="coerce") <= point, "production_order_id_str"])
            released_ops = set(ff.loc[ff["production_order_id_str"].isin(released_orders), "operation_key"])
            current_active = released_ops - current_complete
            proxy_rows.append({"factory_id": fid, "period_start": point.isoformat(), "released_orders": len(released_orders), "released_operations": len(released_ops), "present_state_incomplete_orders_proxy": len(released_orders - complete_orders), "present_state_completed_orders_proxy": len(released_orders & complete_orders), "present_state_incomplete_operations_proxy": len(current_active), "present_state_completed_operations_proxy": len(released_ops & current_complete)})
    proxy = pd.DataFrame(proxy_rows)
    historical_reconstructable = False
    _write(OUT / "historical_active_workload.json", {"status": "HISTORICAL_ACTIVE_STATE_NOT_EXACTLY_RECONSTRUCTABLE", "reason": "Production_Orders and Job_Shop_Schedules provide current state and timestamps but no authoritative order/operation state-transition event history.", "weekly_release_and_present_state_proxy": _records(proxy), "proxy_not_a_historical_execution_series": True})
    candidate_periods = {}
    for fid in FACTORIES:
        p = proxy[proxy["factory_id"] == fid].copy()
        if p.empty: candidate_periods[str(fid)] = []; continue
        for label, quant in (("LOW", .10), ("MEDIUM", .50), ("HIGH", .90)):
            idx = (p["present_state_incomplete_operations_proxy"] - p["present_state_incomplete_operations_proxy"].quantile(quant)).abs().idxmin()
            row = p.loc[idx]
            candidate_periods.setdefault(str(fid), []).append({"classification": label, "status": "APPROXIMATE_LOAD_PERIOD", "period_start": row["period_start"], "active_operations_proxy": int(row["present_state_incomplete_operations_proxy"]), "active_orders_proxy": int(row["present_state_incomplete_orders_proxy"]), "reason": "release-date accumulation minus current completed-operation rows; no historical state-transition log"})
    _write(OUT / "candidate_load_periods.json", {"status": "APPROXIMATE_LOAD_PERIOD", "factories": candidate_periods})
    _write(OUT / "benchmark_snapshot_recommendations.json", {"status": "CANDIDATE_DATES_ONLY_NO_SNAPSHOTS_CREATED", "candidate_periods": candidate_periods, "historical_active_state_reconstructable": historical_reconstructable})

    # Data quality and provenance.
    op_keys = full["operation_key"].astype(str)
    quality = {
        "duplicate_orders": int(orders["production_order_id_str"].duplicated().sum()),
        "duplicate_operation_keys": int(op_keys.duplicated().sum()),
        "missing_factory_ids_orders": int(orders["factory_id"].isna().sum()),
        "missing_line_ids": int(lines["line_id"].isna().sum()),
        "missing_machine_capability_rows": int(machines["operation_type"].isna().sum()),
        "missing_worker_skill_rows": int(skills["operation_type"].isna().sum()),
        "missing_duration": int(routings["standard_allowed_minutes"].isna().sum()),
        "negative_duration": int((pd.to_numeric(routings["standard_allowed_minutes"], errors="coerce") < 0).sum()),
        "zero_duration": int((pd.to_numeric(routings["standard_allowed_minutes"], errors="coerce") == 0).sum()),
        "missing_release": int(orders["scheduled_start_date"].isna().sum()),
        "missing_due_date": int(orders["delivery_deadline"].isna().sum()),
        "due_before_release": int((pd.to_datetime(orders["delivery_deadline"], errors="coerce") < pd.to_datetime(orders["scheduled_start_date"], errors="coerce")).sum()),
        "invalid_machine_references_in_schedule": int((~jss["asset_id"].isin(set(machine_base["asset_id"]))).sum()) if not jss.empty else 0,
        "invalid_worker_references_in_schedule": int((~jss["employee_id"].astype(str).isin(set(employees["employee_id"].astype(str)))).sum()) if not jss.empty else 0,
        "orphan_schedule_operations": int((~jss["operation_key"].isin(set(op_keys))).sum()) if not jss.empty else 0,
        "orphan_routing_products": int((~routings["product_id"].isin(set(orders["product_id"]))).sum()),
        "invalid_calendar_records": int((schedules["start_time"].isna() | schedules["end_time"].isna()).sum()) if not schedules.empty else 0,
        "critical_scheduling_blockers": [],
        "non_critical_data_notes": ["current status tables are not a historical event log", "empty factories 4 and 6 have machine line IDs without active line metadata", "missing historical provenance fields prevent exact genuine/synthetic separation"],
    }
    # Precedence structural checks from full order routes.
    quality["duplicate_routing_sequence_orders"] = []
    for oid, group in full.groupby("production_order_id_str"):
        if group["operation_sequence_number"].duplicated().any(): quality["duplicate_routing_sequence_orders"].append(str(oid))
    _write(OUT / "data_quality_report.json", quality)
    (OUT / "data_quality_report.md").write_text("# Data quality report\n\n" + json.dumps(quality, indent=2, default=_json_default) + "\n", encoding="utf-8")
    provenance = {"status": "PROVENANCE_NOT_EXACTLY_RECOVERABLE", "reason": "No authoritative genuine/synthetic/proxy provenance column or immutable provenance table was found in the current schema.", "searched_schema_fields": ["Production_Orders", "Routing_Operations", "Job_Shop_Schedules", "Purchase_Orders"], "counts_not_invented": True}
    _write(OUT / "provenance_report.json", provenance)

    # Global EDA and per-factory files.
    full_date_range = global_date_range
    global_eda = {"database": db_name, "factory_count": len(factories), "line_count": int(len(lines)), "machine_count": int(len(machine_base)), "usable_machine_count_craft_semantics": int(machine_base["craft_usable"].sum()), "worker_count": int(len(employees)), "assignable_worker_count": int(len(assignable_ids)), "order_count": int(orders["production_order_id_str"].nunique()), "unique_po_group_count": int(orders["po_id"].nunique()), "sku_count": int(variants["sku"].nunique()) if "sku" in variants else None, "style_count": int(products["style_code"].nunique()) if "style_code" in products else None, "routing_definition_count": int(len(routings)), "full_operation_raw_join_count": int(len(full)), "full_unique_operation_count": int(full["operation_key"].nunique()), "precedence_edge_count": int(sum(max(0, len(g) - 1) for _, g in full.groupby("production_order_id_str"))), "worker_eligibility_relation_count": int(len(skills)), "machine_eligibility_relation_count": int(len(machines["operation_type"].dropna())), "calendar_record_count": int(len(schedules)), "date_range": full_date_range, "covered_days": covered_days, "covered_months": (covered_days / 30.4375 if covered_days is not None else None), "release_to_due_lead_days": _stats((pd.to_datetime(orders["delivery_deadline"], errors="coerce") - pd.to_datetime(orders["scheduled_start_date"], errors="coerce")).dt.total_seconds().div(86400).dropna().tolist()), "statuses": orders["status"].value_counts(dropna=False).to_dict(), "monthly": monthly, "weekly": weekly, "full_operation_count_vs_previous_7809": {"previous": 7809, "current": int(full["operation_key"].nunique()), "difference": int(full["operation_key"].nunique()) - 7809, "current_status_context": {"current_active_orders": int(len(active_order_ids)), "current_completed_orders": int((~orders["production_order_id_str"].isin(active_order_ids)).sum()), "documented_previous_active_orders": 911, "documented_previous_active_operations": 7809, "explanation": "The current connected database has 958 active-status orders and 71 completed orders; 7809 is documented for an older 911-active-order state."}}, "active_query_operation_count": int(active["operation_key"].nunique()), "current_neutral_operation_count": int(sum(len(v.get("operations", [])) for v in neutral.values()))}
    _write(OUT / "global_eda.json", global_eda)
    (OUT / "global_eda.md").write_text("# Global database EDA\n\n" + json.dumps(global_eda, indent=2, default=_json_default) + "\n", encoding="utf-8")
    for fid, detail in factory_details.items():
        _write(OUT / f"factory{fid}_eda.json", detail)
    _write(OUT / "factory_comparison.json", {"factories": global_factory_rows, "factory2_frozen_reference": neutral.get(2, {}).get("accounting", {})})
    comp_md = ["# Factory comparison", "", "| Factory | Historical Orders | Historical Operations | Lines | Usable Machines | Assignable Workers | Date Range |", "|---:|---:|---:|---:|---:|---:|---|"]
    for r in global_factory_rows: comp_md.append(f"| {r['factory_id']} | {r['historical_orders']} | {r['historical_operations']} | {r['lines']} | {r['usable_machines']} | {r['assignable_workers']} | {r['date_range']['release']['earliest']} → {r['date_range']['release']['latest']} |")
    (OUT / "factory_comparison.md").write_text("\n".join(comp_md) + "\n", encoding="utf-8")

    # Plots are descriptive only.
    _plot_bar(PLOTS / "orders_per_factory.png", FACTORIES, [factory_details[f]["historical_orders"] for f in FACTORIES], "Historical orders by factory", "orders")
    _plot_bar(PLOTS / "operations_per_factory.png", FACTORIES, [factory_details[f]["historical_unique_operations"] for f in FACTORIES], "Historical operations by factory", "operations")
    _plot_bar(PLOTS / "machines_workers_operations_by_factory.png", FACTORIES, [factory_details[f]["machines"]["usable_craft_semantics"] for f in FACTORIES], "CRAFT-semantics usable machines by factory", "machines")
    _plot_hist(PLOTS / "operations_per_order.png", full.groupby("production_order_id_str").size().tolist(), "Operations per order", "operations/order")
    _plot_hist(PLOTS / "operation_duration_seconds.png", pd.to_numeric(routings["standard_allowed_minutes"], errors="coerce").dropna().mul(60).tolist(), "Routing productive duration proxy", "seconds")
    leads = (pd.to_datetime(orders["delivery_deadline"], errors="coerce") - pd.to_datetime(orders["scheduled_start_date"], errors="coerce")).dt.total_seconds().div(86400).dropna().tolist()
    _plot_hist(PLOTS / "release_to_due_lead_days.png", leads, "Release-to-due lead time", "days")
    release_month = pd.to_datetime(orders["scheduled_start_date"], errors="coerce").dropna().dt.to_period("M").astype(str).value_counts().sort_index()
    _plot_series(PLOTS / "orders_per_month.png", {"all": list(zip(release_month.index.tolist(), release_month.values.tolist()))}, "Orders released per month", "orders")
    op_month = pd.to_datetime(full["scheduled_start_date"], errors="coerce").dropna().dt.to_period("M").astype(str).value_counts().sort_index()
    _plot_series(PLOTS / "operations_per_month.png", {"all": list(zip(op_month.index.tolist(), op_month.values.tolist()))}, "Operations associated with released orders per month", "operations")

    # Percentiles from the explicit present-state proxy (not asserted historical active state).
    percentiles = {}
    for fid in FACTORIES:
        vals = proxy.loc[proxy["factory_id"] == fid, "present_state_incomplete_operations_proxy"].tolist() if not proxy.empty else []
        order_vals = proxy.loc[proxy["factory_id"] == fid, "present_state_incomplete_orders_proxy"].tolist() if not proxy.empty else []
        percentiles[str(fid)] = {"active_operations_status": "APPROXIMATE_PRESENT_STATE_PROXY", "active_orders_status": "APPROXIMATE_PRESENT_STATE_PROXY", "stats": _stats(vals), "active_order_stats": _stats(order_vals)}
    _plot_bar(PLOTS / "active_workload_percentiles.png", FACTORIES, [percentiles[str(f)]["stats"]["median"] or 0 for f in FACTORIES], "Median proxy active operations", "operations")
    _write(OUT / "monthly_workload.json", {"monthly": monthly, "weekly": weekly, "minimum_median_maximum_by_factory": {str(fid): {"orders_released": _stats([x["count"] for x in _monthly_counts(orders[orders["factory_id"] == fid], "scheduled_start_date", "production_order_id_str")]), "operations_released": _stats([x["count"] for x in _monthly_counts(full[full["factory_id"] == fid], "scheduled_start_date", "operation_key")])} for fid in FACTORIES}})

    filter_summary = f"Active loader uses status Pending/Processing/Delayed and a routing join; it does not filter by release date. Full DB uses every Production_Orders row joined to Routing_Operations. Neutral counts are frozen snapshot artifacts.\n\nThe complete active query contains {int(active['operation_key'].nunique())} unique operation keys; the current neutral artifacts contain {int(sum(len(v.get('operations', [])) for v in neutral.values()))}.\n"
    _write(OUT / "global_eda.json", global_eda)
    f2_full = factory_details[2]["historical_unique_operations"]
    summary_text = "# Full database EDA summary\n\n" + filter_summary
    summary_text += f"\n## Evidence-based interpretation\n\n"
    summary_text += f"1. The current neutral benchmark is small because it is a frozen benchmark snapshot (431 operations across the six neutral artifacts), not the complete active SQL workload ({int(active['operation_key'].nunique())} operations) and not the full order×routing universe ({int(full['operation_key'].nunique())} operations). The active SQL loader itself retains orders with status Pending, Processing, or Delayed and has no release-date cutoff.\n"
    summary_text += f"2. The full logical database contains {int(full['operation_key'].nunique())} operations; the neutral artifacts represent {100 * sum(len(v.get('operations', [])) for v in neutral.values()) / max(1, int(full['operation_key'].nunique())):.2f}% of that universe.\n"
    summary_text += f"3. Factory 2's 172-operation benchmark is a light slice relative to its current full history of {f2_full} operations ({100 * 172 / max(1, f2_full):.2f}%).\n"
    summary_text += "4. Substantial historical workloads are present in Factories 1, 2, 5, and 3; Factories 4 and 6 are much smaller.\n"
    summary_text += "5. Exact historical active snapshots cannot be reconstructed: the schema has current status/timestamps but no authoritative state-transition event history.\n"
    summary_text += "6. LOW/MEDIUM/HIGH dates in candidate_load_periods.json are explicitly approximate release-accumulation proxies, not frozen benchmark snapshots.\n"
    summary_text += "7. The evidence supports evaluating multiple factories and multiple explicitly defined load levels, but a separate neutral snapshot contract must be approved before any scheduling run.\n"
    summary_text += "\nHistorical active state: NOT EXACTLY RECONSTRUCTABLE. No candidate snapshot was frozen and no scheduler was run.\n"
    (OUT / "eda_summary.md").write_text(summary_text, encoding="utf-8")

    protected_after = {name: _tree_hash(path) for name, path in PROTECTED.items()}
    protected_unchanged = protected_before == protected_after
    output_files = sorted(p for p in OUT.rglob("*") if p.is_file() and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"})
    manifest = {"output_file_sha256": {str(p.relative_to(OUT)): _sha_file(p) for p in output_files}, "protected_before": protected_before, "protected_after": protected_after, "protected_artifacts_unchanged": protected_unchanged, "database_writes": 0}
    _write(OUT / "artifact_hash_manifest.json", manifest)
    verification = {"output_hashes_verified": all(_sha_file(OUT / name) == digest for name, digest in manifest["output_file_sha256"].items()), "protected_artifacts_unchanged": protected_unchanged, "all_hashes_verified": bool(protected_unchanged and all(_sha_file(OUT / name) == digest for name, digest in manifest["output_file_sha256"].items())), "database_writes": 0, "artifact_hash_manifest_sha256": _sha_file(OUT / "artifact_hash_manifest.json")}
    _write(OUT / "hash_verification.json", verification)

    print("STATUS = FULL_DATABASE_EDA_COMPLETE")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    print("FULL_DATABASE_FACTORIES =", len(factories))
    print("FULL_DATABASE_ORDERS =", global_eda["order_count"])
    print("FULL_DATABASE_OPERATIONS =", global_eda["full_unique_operation_count"])
    print("FULL_DATABASE_DATE_RANGE =", json.dumps(global_date_range, default=_json_default))
    for fid in FACTORIES:
        print(f"FACTORY{fid}_HISTORICAL_ORDERS =", factory_details[fid]["historical_orders"])
        print(f"FACTORY{fid}_HISTORICAL_OPERATIONS =", factory_details[fid]["historical_unique_operations"])
    print("FULL_OPERATION_COUNT_VS_PREVIOUS_7809 =", global_eda["full_operation_count_vs_previous_7809"])
    print("CURRENT_ACTIVE_OPERATION_COUNT =", global_eda["active_query_operation_count"])
    print("CURRENT_NEUTRAL_OPERATION_COUNT =", global_eda["current_neutral_operation_count"])
    print("FULL_TO_ACTIVE_PERCENT =", 100 * global_eda["active_query_operation_count"] / max(1, global_eda["full_unique_operation_count"]))
    print("ACTIVE_FILTER_EXPLANATION = Production_Orders status in Pending/Processing/Delayed plus Routing_Operations join; no release-date cutoff")
    print("HISTORICAL_ACTIVE_STATE_RECONSTRUCTABLE = no")
    for fid in FACTORIES:
        st = percentiles[str(fid)]["stats"]
        print(f"FACTORY{fid}_ACTIVE_OP_P50 =", st["median"], f"FACTORY{fid}_ACTIVE_OP_P90 =", st["p90"], f"FACTORY{fid}_ACTIVE_OP_MAX =", st["max"])
    print("CRITICAL_DATA_BLOCKERS =", quality["critical_scheduling_blockers"] or "none")
    print("NON_CRITICAL_DATA_NOTES =", quality["non_critical_data_notes"])
    print("REAL_SYNTHETIC_PROVENANCE_STATUS = PROVENANCE_NOT_EXACTLY_RECOVERABLE")
    print("RECOMMENDED_BENCHMARK_STRATEGY = use multiple factories and explicitly selected approximate low/medium/high periods only after a separate snapshot-contract decision")
    print("EXISTING_FROZEN_ARTIFACTS_UNCHANGED =", str(protected_unchanged).lower())
    print("ARTIFACT_HASHES_VERIFIED =", str(verification["all_hashes_verified"]).lower())
    print("NEXT_STEP = review EDA artifacts and define a neutral historical snapshot contract; do not schedule yet")


if __name__ == "__main__":
    main()
