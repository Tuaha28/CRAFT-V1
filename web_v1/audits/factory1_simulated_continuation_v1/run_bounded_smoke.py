"""One source-derived, domain-restricted CRAFT scheduling smoke test.

This is NOT a full Factory-1 scheduling run. It uses a single already-eligible
machine/worker pair solely to prove that the simulated residual work budget
can traverse the unmodified CP-SAT/materialization/replay/validation pipeline.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import psutil

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.solve import run_canonical_schedule
from utils.machine_taxonomy import machine_type_to_operation_type

HERE = Path(__file__).resolve().parent
KEY = "887_3501"


def main() -> int:
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stopped = threading.Event()
    def sample_memory():
        while not stopped.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss)
    watcher = threading.Thread(target=sample_memory, daemon=True)
    watcher.start()
    total_started = time.perf_counter()
    source_path = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
    raw_source = source_path.read_bytes()
    source = json.loads(raw_source)
    snapshot = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json").read_text(encoding="utf-8"))
    scenario = json.loads((HERE / "scenario.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw_source).hexdigest() != scenario["source_fingerprint"]:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    loading_seconds = time.perf_counter() - total_started
    record = next(r for r in scenario["partial_operations"] if r["operation_key"] == KEY)
    normalized = next(r for r in snapshot["operations"] if r["operation_key"] == KEY)
    route = next(r for r in source["operations"] if str(r["production_order_id"]) == "887" and str(r["operation_id"]) == "3501")
    if (record["fixed_in_flight_holds"] or normalized["successor"] is not None or
            normalized["precedence_boundary"]["unresolved_predecessors"] or
            normalized["precedence_boundary"]["earliest_ready_lower_bound"] > source["reference_time"]):
        raise RuntimeError("UNSAFE_SOURCE_SCOPE")
    lot = next(r for r in normalized["lots"] if r["state"] == "NOT_STARTED")
    machine_id, worker_id, line_id = lot["machine_id"], lot["worker_id"], lot["line_id"]
    machine = next(r for r in source["resources"]["machines"] if r["asset_id"] == machine_id)
    worker = next(r for r in source["resources"]["workers"] if r["employee_id"] == worker_id)
    member = next(r for r in source["resources"]["batch_members"] if r["employee_id"] == worker_id)
    batch = next(r for r in source["resources"]["batches"] if r["batch_id"] == member["batch_id"])
    capability = machine_type_to_operation_type(route["required_machine_type"])
    machine_caps = {r["operation_type"].lower() for r in source["resources"]["machine_capabilities"] if r["asset_id"] == machine_id}
    worker_skills = {r["operation_type"].lower() for r in source["resources"]["skills"] if r["employee_id"] == worker_id}
    if (not worker["is_active"] or machine["current_status"] != "Active" or
            machine["line_id"] != line_id or capability not in machine_caps or capability not in worker_skills):
        raise RuntimeError("RESOURCE_INELIGIBLE")
    origin = datetime.fromisoformat(source["reference_time"])
    duration = record["hypothetical_unstarted_serial_productive_seconds"]
    op = OperationSpec(KEY, "887", "3501", normalized["routing_sequence"], capability,
                       (line_id,), duration, 900, 1, 1)
    problem = SchedulingProblem("F1-SIMULATED-ONE-OP-NOT-PRODUCTION", origin, 3*86400,
                                (LineSpec(line_id, 1),),
                                (MachineSpec(machine_id, 1, line_id, (capability,)),),
                                (OrderSpec("887", 1, 0, None),), (op,), ())
    index = {"by_factory": {(1, capability): [worker_id]},
             "by_line": {(line_id, capability): [worker_id]},
             "by_skill": {capability: [worker_id]},
             "line_factory": {line_id: 1}, "crew_of": {worker_id: (batch["rotation_offset"], batch["rest_day_offset"])},
             "shift_of": {}, "efficiency_of": {(worker_id, capability): 1.0}}
    route_index = SimpleNamespace(rows=(), by_key={KEY: {"start_minutes": 0}},
                                  route_predecessor={KEY: None}, route_successor={KEY: None})
    state = {"problem": problem, "operator_index": index, "after_index": route_index,
             "keys": (KEY,), "baseline": SimpleNamespace(operations=()), "event": None,
             "planning_context": {"mode": "INITIAL_PLAN"},
             "objective_profile": "INITIAL_PLAN_NO_STABILITY",
             "classified": {KEY: {"state": "MOVABLE_WINDOW"}},
             "_machine_res": {}, "_worker_res": {}, "boundary_facts": ()}
    config = ResourceConfiguration(KEY, 1, line_id, 1, (machine_id,), (str(worker_id),),
                                   900, duration, ("SIMULATION_ONLY_RESTRICTED_LEGAL_DOMAIN",), duration/60)
    started = time.perf_counter()
    regimes, summary, count, equivalence = generate_calendar_regimes(state, {KEY: (config,)})
    regime_time = time.perf_counter() - started
    prepared = {"state": state, "resource_configurations": {KEY: (config,)}, "regimes": regimes}
    started = time.perf_counter()
    result = run_canonical_schedule(prepared, plan=((1, None, 5),), objective_mode=False)
    solve_time = time.perf_counter() - started
    acceptance = result["acceptance"]
    selected = acceptance.get("selected_schedule") or []
    row = next((r for r in selected if r["operation_key"] == KEY), None)
    work_ok = (row is not None and sum(b-a for a, b in row["worker_segments"]) == duration and
               row["resource_configuration"]["worker_ids"] == (str(worker_id),) and
               row["resource_configuration"]["machine_ids"] == (machine_id,))
    evidence = {
        "classification": "SIMULATED_SOURCE_DERIVED_BOUNDED_SMOKE_NOT_LIVE",
        "source_snapshot_hash": snapshot["snapshot_hash"], "scenario_hash": scenario["scenario_hash"],
        "operation_key": KEY, "required_quantity": record["original_required_quantity"],
        "completed_quantity": record["accepted_completed_quantity"],
        "remaining_quantity": record["unstarted_residual_quantity"],
        "setup_assumption_seconds": 900,
        "selected_machine_id": machine_id, "selected_worker_id": worker_id,
        "legal_alternatives_exhausted": False, "factory_wide_schedule": False,
        "calendar_regimes": count, "calendar_equivalence": equivalence,
        "calendar_seconds": regime_time, "solve_and_validation_seconds": solve_time,
        "status": acceptance["status"], "replay": acceptance.get("replay", {}).get("status"),
        "physical": acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
        "scope": acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status"),
        "quantity_and_resource_reconciled": work_ok,
        "selected_start_seconds": row["start_seconds"] if row else None,
        "selected_end_seconds": row["end_seconds"] if row else None,
    }
    stopped.set()
    watcher.join(timeout=1)
    peak[0] = max(peak[0], process.memory_info().rss)
    timing = {"source_loading_seconds": loading_seconds,
              "calendar_seconds": regime_time, "solve_and_validation_seconds": solve_time,
              "execution_wall_seconds": time.perf_counter() - total_started,
              "peak_rss_mb": peak[0] / 1024**2}
    timing_path = HERE / "bounded_smoke_timing.json"
    if not timing_path.exists():
        timing_path.write_text(json.dumps(timing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target = HERE / "bounded_smoke.json"
    if target.exists():
        prior = json.loads(target.read_text(encoding="utf-8"))
        stable = ("classification", "source_snapshot_hash", "scenario_hash", "operation_key",
                  "required_quantity", "completed_quantity", "remaining_quantity",
                  "selected_machine_id", "selected_worker_id", "calendar_regimes", "status",
                  "replay", "physical", "scope", "quantity_and_resource_reconciled",
                  "selected_start_seconds", "selected_end_seconds")
        if any(prior.get(field) != evidence.get(field) for field in stable):
            raise RuntimeError("EXISTING_SMOKE_SEMANTICS_DIFFER; refusing overwrite")
    if not target.exists():
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps(evidence, default=str))
    return 0 if acceptance["status"] == "ACCEPTED" and work_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
