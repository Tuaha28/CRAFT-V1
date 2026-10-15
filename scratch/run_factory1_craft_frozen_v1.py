"""Run CRAFT Production V1 from the frozen Factory-1 workload artifacts.

The workload membership comes exclusively from
``outputs/crafter/factory1_craft_workload_v1``.  Read-only database queries are
limited to static resource/calendar metadata and authoritative boundary timing
needed by the existing planning kernel; they never determine membership.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKLOAD = ROOT / "outputs/crafter/factory1_craft_workload_v1"
OUT = ROOT / "outputs/crafter/factory1_craft_run_v1"
REF_TIME = datetime.fromisoformat("2026-09-10T00:00:00")
FACTORY_ID = 1
EXCLUDED_ORDER = "790"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_file():
        return sha_file(path)
    h = hashlib.sha256()
    for child in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p.relative_to(path))):
        h.update(str(child.relative_to(path)).replace("\\", "/").encode())
        h.update(b"\0")
        h.update(bytes.fromhex(sha_file(child)))
    return h.hexdigest()


PROTECTED = {
    "frozen_workload": WORKLOAD,
    "factory1_schedulable_audit": ROOT / "outputs/crafter/factory1_schedulable_workload_audit_v1",
    "factory2_craft": ROOT / "outputs/crafter/factory2_172_production_v1_workerfix_full",
    "evaluation_v2": ROOT / "outputs/crafter/evaluation_v2",
    "fifo": ROOT / "outputs/crafter/baselines/fifo_factory2_v1",
    "edd": ROOT / "outputs/crafter/baselines/edd_factory2_v1",
    "spt": ROOT / "outputs/crafter/baselines/spt_factory2_v1",
    "full_database_eda": ROOT / "outputs/crafter/full_database_eda_v1",
}


def _seconds(value):
    if value is None or pd.isna(value):
        return None
    return int(math.ceil((pd.Timestamp(value).to_pydatetime() - REF_TIME).total_seconds()))


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _query(conn, sql):
    return pd.read_sql(text(sql), conn)


def _verify_frozen_workload():
    identity = read_json(WORKLOAD / "workload_identity.json")
    manifest = read_json(WORKLOAD / "artifact_hash_manifest.json")
    verification = read_json(WORKLOAD / "hash_verification.json")
    if not verification.get("all_hashes_verified"):
        raise RuntimeError("frozen workload hash verification artifact is not valid")
    for name, expected in manifest.get("output_hashes", {}).items():
        path = WORKLOAD / name
        if not path.exists() or sha_file(path) != expected:
            raise RuntimeError(f"frozen workload hash mismatch: {name}")
    expected_counts = {
        "orders": 335, "state_operations": 2699,
        "optimizable_operations": 2692, "running_operations": 7,
        "future_release_operations": 1447,
    }
    if identity.get("database") != "garment_fyp_db" or identity.get("reference_time") != REF_TIME.isoformat():
        raise RuntimeError("frozen workload database/reference identity mismatch")
    if identity.get("counts") != expected_counts:
        raise RuntimeError(f"frozen workload count mismatch: {identity.get('counts')}")
    return identity, manifest


def _build_loaded():
    """Construct the existing loaded tuple from frozen membership artifacts."""
    from agent.decision_engine import AIDecisionEngine
    from crafter.contracts import (
        FixedMachineCommitment, FixedWorkerCommitment, LabourCapacitySpec,
        LineSpec, MachineSpec, OperationSpec, OrderSpec, ResourceState,
        SchedulingProblem,
    )
    from crafter.lns_probe.analysis import build_incumbent_index
    from crafter.lns_probe.transaction import frozen_incumbent
    from crafter.repair.global_planner import WindowBoundaryState
    from database.db import get_engine
    from database.queries import fetch_assignable_operators, fetch_line_factory_map, fetch_machine_state
    from utils.machine_reserve import NON_USABLE_STATUSES, build_reserve
    from utils.machine_taxonomy import machine_type_to_operation_type

    contract = read_json(WORKLOAD / "workload_contract.json")
    orders_doc = read_json(WORKLOAD / "orders.json")
    state_doc = read_json(WORKLOAD / "state_operations.json")
    running_doc = read_json(WORKLOAD / "running_boundary_operations.json")
    future_doc = read_json(WORKLOAD / "future_release_operations.json")
    resource_state = read_json(WORKLOAD / "resource_state.json")
    precedence_doc = read_json(WORKLOAD / "precedence_audit.json")
    identity, _manifest = _verify_frozen_workload()

    state_records = {r["operation_key"]: r for r in state_doc["operations"]}
    running_records = {r["operation_key"]: r for r in running_doc["operations"]}
    future_records = {r["operation_key"]: r for r in future_doc["operations"]}
    state_keys = set(state_records)
    optimizable_keys = set(r["operation_key"] for r in json.loads((WORKLOAD / "optimizable_operations.json").read_text(encoding="utf-8"))["operations"])
    running_keys = set(running_records)
    order_records = {str(r["order_id"]): r for r in orders_doc["orders"]}

    with get_engine().connect() as conn:
        db_name = str(conn.execute(text("SELECT DATABASE()")).scalar_one())
        routes = _query(conn, """
            SELECT po.production_order_id, po.product_id,
                   ro.operation_id, ro.operation_sequence_number,
                   ro.operation_name, ro.standard_allowed_minutes,
                   ro.required_machine_type
            FROM Production_Orders po
            JOIN Routing_Operations ro ON ro.product_id=po.product_id
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(po.production_order_id AS CHAR)
            WHERE ofo.factory_id=1 AND po.production_order_id<>790
            ORDER BY po.production_order_id, ro.operation_sequence_number, ro.operation_id
        """)
        schedules = _query(conn, """
            SELECT j.schedule_item_id, j.production_order_id, j.operation_id,
                   j.line_id, j.asset_id, j.employee_id, j.lot_index,
                   j.lot_quantity, j.execution_status,
                   j.planned_start_time, j.planned_end_time,
                   j.actual_start_time, j.actual_end_time,
                   j.actual_work_segments
            FROM Job_Shop_Schedules j
            JOIN Order_Factory_Ownership ofo
              ON CAST(ofo.production_order_id AS CHAR)=CAST(j.production_order_id AS CHAR)
            WHERE ofo.factory_id=1 AND j.production_order_id<>790
            ORDER BY j.production_order_id, j.operation_id, j.lot_index, j.schedule_item_id
        """)
        lines = _query(conn, """
            SELECT pl.line_id, pl.department_id, pl.line_number,
                   pl.target_efficiency_percentage, pl.is_active, d.factory_id
            FROM Production_Lines pl JOIN Departments d ON d.department_id=pl.department_id
            WHERE pl.is_active=1
        """)

    if db_name != "garment_fyp_db":
        raise RuntimeError(f"unexpected connected database: {db_name}")
    if len(routes) != 2870:
        raise RuntimeError(f"frozen-vs-route static identity mismatch: {len(routes)} routes")

    machine_df = fetch_machine_state()
    machine_df = machine_df.dropna(subset=["asset_id", "factory_id"]).copy()
    reserved_ids, _caps = build_reserve(machine_df)
    machine_rows = []
    machine_by_id = {}
    for asset_id, group in machine_df.groupby("asset_id", sort=True):
        first = group.iloc[0]
        if int(first["factory_id"]) != FACTORY_ID:
            continue
        capabilities = tuple(sorted({str(x).strip().lower() for x in group["operation_type"].dropna().tolist()}))
        usable = int(asset_id) not in reserved_ids and str(first.get("current_status", "Active")) not in NON_USABLE_STATUSES
        machine = MachineSpec(int(asset_id), FACTORY_ID, int(first["line_id"]), capabilities, usable)
        machine_rows.append(machine); machine_by_id[machine.machine_id] = machine
    line_factory = fetch_line_factory_map()
    line_specs = tuple(
        LineSpec(int(row.line_id), int(row.factory_id), str(row.line_number))
        for row in lines.itertuples(index=False) if int(row.factory_id) == FACTORY_ID
    )
    workers = fetch_assignable_operators()
    operator_index = AIDecisionEngine._build_operator_index(workers, lines, line_factory)

    route_records = []
    route_by_order = defaultdict(list)
    route_by_key = {}
    for row in routes.to_dict(orient="records"):
        order_id = str(row["production_order_id"]); op_id = int(row["operation_id"])
        key = f"{order_id}_{op_id}"
        row["production_order_id"] = order_id; row["operation_id"] = op_id
        row["operation_key"] = key; row["routing_sequence"] = int(row["operation_sequence_number"])
        route_by_order[order_id].append(row); route_by_key[key] = row; route_records.append(row)
    for values in route_by_order.values(): values.sort(key=lambda r: (r["routing_sequence"], r["operation_id"]))

    schedule_by_key = defaultdict(list)
    for row in schedules.to_dict(orient="records"):
        key = f"{int(row['production_order_id'])}_{int(row['operation_id'])}"
        schedule_by_key[key].append(row)

    def resource_for(key, capability):
        rec = resource_state["operation_resource_records"]
        if isinstance(rec, dict): rec = rec.get(key)
        else: rec = next((x for x in rec if x.get("operation_key") == key), None)
        if rec is not None:
            mids = tuple(sorted(int(x) for x in rec.get("eligible_machine_ids", ())))
            wids = tuple(sorted(str(x) for x in rec.get("eligible_worker_ids", ())))
        else:
            mids = tuple(sorted(m.machine_id for m in machine_rows if m.is_usable and m.supports(capability)))
            wids = tuple(sorted(str(x) for x in operator_index.get("by_factory", {}).get((FACTORY_ID, capability), ())))
        lines_for = tuple(sorted({machine_by_id[m].line_id for m in mids if m in machine_by_id}))
        return lines_for, mids, wids

    order_specs = []
    for order_id in sorted(order_records, key=lambda x: (int(x), x)):
        record = order_records[order_id]
        release = pd.Timestamp(record["release_date"]) if record.get("release_date") else None
        due = pd.Timestamp(record["due_date"]) if record.get("due_date") else None
        release_seconds = max(0, _seconds(release) or 0)
        deadline_seconds = _seconds(due) if due is not None else None
        order_specs.append(OrderSpec(order_id, FACTORY_ID, release_seconds, deadline_seconds, 1))

    operation_specs = []
    op_details = {}
    for route in route_records:
        key = route["operation_key"]
        capability = machine_type_to_operation_type(str(route["required_machine_type"]))
        capability = str(capability or "").strip().lower()
        minutes = float(route["standard_allowed_minutes"])
        if not math.isfinite(minutes) or minutes <= 0:
            raise RuntimeError(f"invalid frozen duration for {key}: {minutes}")
        lines_for, mids, wids = resource_for(key, capability)
        if not lines_for or not mids or not wids:
            raise RuntimeError(f"empty frozen resource domain for {key}")
        operation_specs.append(OperationSpec(
            key, route["production_order_id"], str(route["operation_id"]),
            int(route["routing_sequence"]), capability, lines_for,
            max(1, int(math.ceil(minutes * 60.0))), 900, 1, 1))
        op_details[key] = {"setup_mins": 15.0, "serial_processing_mins": minutes}
    # A horizon based on the complete frozen operation set, not a DB workload
    # query, keeps the existing contract's rolling planner well-formed.
    sequential_upper = sum(op.setup_seconds + op.serial_productive_seconds + 1500 for op in operation_specs)
    max_deadline = max((o.deadline_seconds or 0 for o in order_specs), default=0)
    problem = SchedulingProblem(
        problem_id="factory1-frozen-craft-workload-v1", origin=REF_TIME,
        horizon_seconds=max(max_deadline, sequential_upper) + 86_400,
        lines=line_specs, machines=tuple(machine_rows),
        orders=tuple(order_specs), operations=tuple(operation_specs), labour_capacities=(),
        resources=ResourceState(), changeover_seconds=1500)

    def aggregate_row(key):
        route = route_by_key[key]; op = next(o for o in operation_specs if o.operation_key == key)
        record = state_records.get(key) or running_records.get(key) or future_records.get(key)
        rows = schedule_by_key.get(key, [])
        running = key in running_keys
        completed = key not in state_keys
        lines_for, mids, wids = resource_for(key, op.required_capability)
        if running:
            running_boundary = record.get("running_boundary") or record
            mids = tuple(int(x) for x in running_boundary.get("running_machine_ids", ())) or mids[:1]
            wids = tuple(str(x) for x in running_boundary.get("running_worker_ids", ())) or wids[:1]
            lines_for = tuple(int(x) for x in running_boundary.get("running_line_ids", ())) or lines_for[:1]
            start_seconds = _seconds(record.get("actual_start_evidence"))
            end_seconds = _seconds(record.get("actual_end_evidence"))
            start_seconds = -1 if start_seconds is None else start_seconds
            end_seconds = max(1, end_seconds or 1)
        elif completed and rows:
            starts = [_seconds(r.get("actual_start_time")) for r in rows if r.get("actual_start_time") is not None]
            ends = [_seconds(r.get("actual_end_time")) for r in rows if r.get("actual_end_time") is not None]
            start_seconds = min(starts) if starts else -1
            end_seconds = max(ends) if ends else -1
            mids = tuple(sorted({int(r["asset_id"]) for r in rows if pd.notna(r.get("asset_id"))})) or mids[:1]
            wids = tuple(sorted({str(r["employee_id"]) for r in rows if pd.notna(r.get("employee_id"))})) or wids[:1]
            lines_for = tuple(sorted({int(r["line_id"]) for r in rows if pd.notna(r.get("line_id"))})) or lines_for[:1]
        else:
            release = max(0, _seconds(pd.Timestamp(order_records[key.split("_", 1)[0]]["release_date"])) or 0)
            start_seconds = release
            end_seconds = start_seconds + 900 + op.serial_productive_seconds
            mids = mids[:1]; wids = wids[:1]; lines_for = lines_for[:1]
        worker_segments = {}
        if running:
            for raw in rows:
                payload = raw.get("actual_work_segments")
                if not payload or pd.isna(payload): continue
                try: parsed = json.loads(payload) if isinstance(payload, str) else payload
                except Exception: parsed = []
                for segment in parsed or []:
                    if len(segment) != 2: continue
                    a, b = _seconds(segment[0]), _seconds(segment[1])
                    if a is not None and b is not None and b > max(0, a):
                        start_dt = REF_TIME + timedelta(seconds=max(0, a))
                        end_dt = REF_TIME + timedelta(seconds=max(0, b))
                        worker_segments.setdefault(str(raw.get("employee_id")), []).append([
                            start_dt.isoformat(), end_dt.isoformat()])
        return {
            "operation_key": key, "production_order_id": route["production_order_id"],
            "order_id": route["production_order_id"], "operation_id": str(route["operation_id"]),
            "line_id": int(lines_for[0]), "machine_ids": list(mids), "employee_ids": list(wids),
            "start_minutes": float(start_seconds) / 60.0,
            "productive_start_minutes": float(start_seconds + 900) / 60.0,
            "end_minutes": float(end_seconds) / 60.0,
            "productive_minutes": float(op.serial_productive_seconds) / 60.0,
            "setup_minutes": 15.0, "release_seconds": int(max(0, problem.order_by_id[route["production_order_id"]].release_seconds)),
            "worker_segments_by_employee": worker_segments,
        }
    rows = [aggregate_row(r["operation_key"]) for r in route_records]
    index = build_incumbent_index(problem, rows, op_details)
    # The production controller's route-pressure diagnostics require the
    # immutable incumbent shape.  Construct it from the same frozen rows;
    # this is diagnostic context, not a second workload source.
    baseline = frozen_incumbent(problem, rows)

    # Build an exact initial boundary from the frozen running boundaries and
    # completed-operation completion evidence.  No order-790 facts enter it.
    completed_keys = {k for k in {r["operation_key"] for r in route_records} if k not in state_keys}
    routing_completion = {}
    for key in sorted(completed_keys):
        end = int(math.ceil(float(next(r for r in rows if r["operation_key"] == key)["end_minutes"]) * 60.0))
        if end <= 0:
            routing_completion[key] = {"operation_key": key, "authoritative_end_seconds": end, "production_order_id": key.split("_", 1)[0]}
    machine_commitments = []
    worker_commitments = []
    order_completion = {}
    for key in sorted(running_keys):
        record = running_records[key]; boundary = record.get("running_boundary") or record
        end = max(1, _seconds(record.get("actual_end_evidence")) or 1)
        for machine in boundary.get("running_machine_ids", ()):
            machine_commitments.append({"machine_id": int(machine), "operation_key": key, "production_order_id": record["order_id"], "start_seconds": 0, "end_seconds": end, "factory_id": FACTORY_ID, "line_id": int(boundary["running_line_ids"][0])})
        for worker in boundary.get("running_worker_ids", ()):
            worker_commitments.append({"worker_id": str(worker), "operation_key": key, "start_seconds": 0, "end_seconds": end, "factory_id": FACTORY_ID})
        routing_completion[key] = {"operation_key": key, "authoritative_end_seconds": end, "production_order_id": record["order_id"]}
        order_completion[record["order_id"]] = {"operation_key": key, "end_seconds": end}
    boundary = WindowBoundaryState(
        FACTORY_ID, tuple(sorted(completed_keys | running_keys)), tuple(sorted(running_keys)),
        tuple(machine_commitments), tuple(worker_commitments), order_completion,
        routing_completion, "factory1-frozen-initial-boundary-v1")
    source = {"source": "factory1_craft_workload_v1", "database": db_name,
              "membership_source": str(WORKLOAD), "database_writes": 0}
    loaded = (problem, operator_index, rows, {}, op_details, index, baseline, None, source)
    return loaded, boundary, tuple(sorted(optimizable_keys)), identity, contract


def main():
    if OUT.exists() and any(OUT.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty output: {OUT}")
    protected_before = {name: tree_hash(path) for name, path in PROTECTED.items()}
    loaded, initial_boundary, optimizable_keys, identity, contract = _build_loaded()
    # Import only after environment is configured and then inject the frozen
    # loader/membership into the unchanged rolling runner.
    os.environ.update({
        "CRAFT_ONE_FACTORY_OUT": str(OUT), "CRAFT_ONE_FACTORY_ID": "1",
        "CRAFT_ONE_FACTORY_NORMAL_CAP": "22", "CRAFT_ONE_FACTORY_MAX_MACHINE_SETS": "1",
        "CRAFT_ONE_FACTORY_MAX_WORKER_CREWS": "3", "CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V3": "0",
        "CRAFT_ONE_FACTORY_ADAPTIVE_CREW_RECOVERY": "0", "CRAFT_ONE_FACTORY_STEP4D": "1",
        "CRAFT_PRODUCTION_V1": "1", "CRAFT_FAST_MODE": "0", "CRAFT_FAST_EVIDENCE": "1",
        "CRAFT_ADAPTIVE_CP_BUDGETS": "1", "CRAFT_ADAPTIVE_CP_RETRY_SECONDS": "120,240",
        "CRAFT_ONE_FACTORY_MAX_WINDOWS": "1",
    })
    import scratch.run_one_factory_craft as runner
    runner.OUT = OUT
    runner._load_state = lambda: loaded
    runner.classify_factory_operations = lambda problem, factory_id: optimizable_keys
    original_state_class = runner.CommittedScheduleState
    runner.CommittedScheduleState = lambda factory_id: original_state_class(
        int(factory_id), (initial_boundary,), tuple(initial_boundary.committed_operation_keys))
    peak_rss = {"bytes": 0}
    stop_sampling = threading.Event()
    def sample_rss():
        try:
            import psutil
            process = psutil.Process(os.getpid())
            while not stop_sampling.is_set():
                peak_rss["bytes"] = max(peak_rss["bytes"],
                                         int(process.memory_info().rss))
                stop_sampling.wait(0.10)
        except Exception:
            return
    sampler = threading.Thread(target=sample_rss, name="factory1-window1-rss",
                               daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        result = runner.run()
    finally:
        stop_sampling.set()
        sampler.join(timeout=1.0)
        runner.CommittedScheduleState = original_state_class
    elapsed = time.perf_counter() - started

    # Preserve the frozen-workload accounting and create the requested run
    # identity/progress envelopes without changing the scheduler output.
    OUT.mkdir(parents=True, exist_ok=True)
    progress = read_json(OUT / "rolling_progress.json") if (OUT / "rolling_progress.json").exists() else {}
    windows = progress.get("windows", [])
    assembled = read_json(OUT / "assembled_factory_schedule.json") if (OUT / "assembled_factory_schedule.json").exists() else {}
    accounted_optimizable = set(assembled.get("assembled_operation_keys", progress.get("cumulative_committed", [])))
    accounted = accounted_optimizable | set(initial_boundary.committed_operation_keys)
    all_state = set(read_json(WORKLOAD / "state_operations.json")["operations"][i]["operation_key"] for i in range(len(read_json(WORKLOAD / "state_operations.json")["operations"])))
    accounting = {
        "factory_id": FACTORY_ID, "initial_orders": identity["counts"]["orders"],
        "initial_state_operations": identity["counts"]["state_operations"],
        "initial_optimizable_operations": identity["counts"]["optimizable_operations"],
        "initial_running_boundary_operations": identity["counts"]["running_operations"],
        "accounted_operation_count": len(accounted), "accounted_optimizable_count": len(accounted_optimizable),
        "missing_state_operations": sorted(all_state - accounted),
        "unexpected_accounted_operations": sorted(accounted - all_state),
        "order_790_present": any(key.startswith("790_") for key in accounted),
        "database_writes": 0,
    }
    (OUT / "run_identity.json").write_text(json.dumps({"database": "garment_fyp_db", "reference_time": REF_TIME.isoformat(), "frozen_workload_identity": identity, "frozen_workload_contract_sha256": sha_file(WORKLOAD / "workload_contract.json"), "scheduler": "CRAFT Production V1", "calendar_engine": "V1", "adaptive_cp_budgets": True, "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "initial_state_identity.json").write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "completion_accounting.json").write_text(json.dumps(accounting, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "window_summary.json").write_text(json.dumps({"window_count": len(windows), "windows": windows, "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "final_state.json").write_text(json.dumps({"status": result.get("status"), "accounting": accounting, "progress": progress, "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "final_schedule.json").write_text(json.dumps({"factory_id": FACTORY_ID, "optimizable_schedule": assembled.get("assembled_schedule", []), "running_boundary_operations": read_json(WORKLOAD / "running_boundary_operations.json")["operations"], "accounted_operation_keys": sorted(accounted), "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "runtime_summary.json").write_text(json.dumps({"total_wall_seconds": elapsed, "runner_result": result, "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "window1_memory_diagnostic.json").write_text(json.dumps({
        "peak_rss_bytes": int(peak_rss["bytes"]),
        "peak_rss_mb": round(peak_rss["bytes"] / (1024 * 1024), 3),
        "window_limit": 1,
        "database_writes": 0,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "replay_report.json").write_text(json.dumps({"runner_validation": read_json(OUT / "assembled_factory_validation.json") if (OUT / "assembled_factory_validation.json").exists() else None, "database_writes": 0}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "physical_validation.json").write_text(json.dumps(read_json(OUT / "assembled_factory_validation.json") if (OUT / "assembled_factory_validation.json").exists() else {"status": "NOT_REACHED"}, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (OUT / "scope_validation.json").write_text(json.dumps({"status": "SCOPE_VALID" if not accounting["missing_state_operations"] and not accounting["unexpected_accounted_operations"] else "SCOPE_INCOMPLETE", "missing": accounting["missing_state_operations"], "unexpected": accounting["unexpected_accounted_operations"], "database_writes": 0}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Hash run artifacts and confirm every protected input stayed unchanged.
    excluded = {"artifact_hash_manifest.json", "hash_verification.json"}
    output_hashes = {p.name: sha_file(p) for p in sorted(OUT.glob("*.json")) if p.name not in excluded}
    (OUT / "artifact_hash_manifest.json").write_text(json.dumps({"algorithm": "SHA-256", "output_hashes": output_hashes, "protected_before": protected_before, "database_writes": 0}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    protected_after = {name: tree_hash(path) for name, path in PROTECTED.items()}
    outputs_verified = all(sha_file(OUT / name) == value for name, value in output_hashes.items())
    verification = {"all_hashes_verified": outputs_verified and protected_before == protected_after, "output_hashes_verified": outputs_verified, "protected_artifacts_unchanged": protected_before == protected_after, "database_writes": 0}
    (OUT / "hash_verification.json").write_text(json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result.get("status"), "windows": len(windows), "scheduled_optimizable": len(accounted_optimizable), "accounted_state": len(accounted), "remaining_state": accounting["missing_state_operations"], "database_writes": 0, "hashes_verified": verification["all_hashes_verified"]}, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
