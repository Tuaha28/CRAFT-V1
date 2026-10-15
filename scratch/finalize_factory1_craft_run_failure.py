from __future__ import annotations
import hashlib, json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "outputs/crafter/factory1_craft_workload_v1"
OUT = ROOT / "outputs/crafter/factory1_craft_run_v1"
REF = "2026-09-10T00:00:00"

def sha_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def tree_hash(path):
    if not path.exists(): return None
    h = hashlib.sha256()
    for p in sorted((x for x in path.rglob("*") if x.is_file()), key=lambda x: str(x.relative_to(path))):
        h.update(str(p.relative_to(path)).replace("\\", "/").encode()); h.update(b"\0"); h.update(bytes.fromhex(sha_file(p)))
    return h.hexdigest()

def read(name): return json.loads((WORKLOAD / name).read_text(encoding="utf-8"))

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    identity = read("workload_identity.json")
    state = read("state_operations.json")["operations"]
    running = read("running_boundary_operations.json")["operations"]
    optimizable = json.loads((WORKLOAD / "optimizable_operations.json").read_text(encoding="utf-8"))["operations"]
    protected = {
        "frozen_workload": WORKLOAD,
        "factory1_schedulable_audit": ROOT / "outputs/crafter/factory1_schedulable_workload_audit_v1",
        "factory2_craft": ROOT / "outputs/crafter/factory2_172_production_v1_workerfix_full",
        "evaluation_v2": ROOT / "outputs/crafter/evaluation_v2",
        "fifo": ROOT / "outputs/crafter/baselines/fifo_factory2_v1",
        "edd": ROOT / "outputs/crafter/baselines/edd_factory2_v1",
        "spt": ROOT / "outputs/crafter/baselines/spt_factory2_v1",
        "full_database_eda": ROOT / "outputs/crafter/full_database_eda_v1",
    }
    before = {k: tree_hash(v) for k,v in protected.items()}
    selected = ["33_3578", "33_3579", "33_3580", "33_3581", "33_3582", "33_3583", "33_3584", "469_1744", "469_1745", "469_1746", "469_1747", "469_1748", "469_1749", "469_1750", "469_1751", "469_1752", "887_3501", "926_2230", "926_2231", "926_2232", "926_2233", "979_952"]
    failure = {
        "status": "FACTORY1_CRAFT_INCOMPLETE",
        "failure_classification": "COMPUTATIONAL_LIMIT",
        "first_failed_window": "window-001",
        "scheduling_clock_seconds": 0,
        "step3b": "SOLVE_READY_WORK",
        "selected_operation_count": 22,
        "selected_operation_keys": selected,
        "resource_configuration_count": 75,
        "calendar_regime_count": 602,
        "resource_configuration_seconds": 0.4158723996952175,
        "calendar_regime_seconds": 19.77225559996441,
        "cp_status": "NOT_REACHED",
        "candidate_exists": False,
        "materialization": "NOT_REACHED",
        "replay": "NOT_REACHED",
        "physical_validation": "NOT_REACHED",
        "scope_validation": "NOT_REACHED",
        "reason": "The exact frozen Factory-1 first-window controller preparation/CP-recovery path grew to unsafe multi-GB memory before emitting a CP status; process was stopped by the run guard. No physical infeasibility was proven and no schedule was committed.",
        "database_writes": 0,
    }
    state_keys = {str(x["operation_key"]) for x in state}
    running_keys = {str(x["operation_key"]) for x in running}
    opt_keys = {str(x["operation_key"]) for x in optimizable}
    accounting = {
        "database": "garment_fyp_db", "reference_time": REF, "factory_id": 1,
        "initial_orders": 335, "initial_state_operations": 2699,
        "initial_optimizable_operations": 2692, "initial_running_boundary_operations": 7,
        "initial_running_boundary_accounted": 7, "optimizable_operations_completed": 0,
        "operations_accounted_for": 7, "remaining_optimizable_operations": 2692,
        "remaining_state_operations": 2692, "missing_state_operation_count": 2692,
        "missing_state_operations": sorted(state_keys - running_keys),
        "orders_completed": 0, "orders_with_running_boundary": sorted({str(x["order_id"]) for x in running}),
        "orders_accounted_for": 0, "remaining_orders": 335,
        "order_790_present": False, "database_writes": 0,
    }
    run_identity = {
        "status": failure["status"], "scheduler": "CRAFT Production V1", "calendar_engine": "V1",
        "database": "garment_fyp_db", "reference_time": REF, "frozen_workload_only": True,
        "workload_identity": identity, "adaptive_cp_budgets": True,
        "initial_state_operations": 2699, "initial_optimizable_operations": 2692,
        "initial_running_boundary_operations": 7, "database_writes": 0,
        "note": "Run stopped before a CP status due computational/memory guard; no workload membership was regenerated.",
    }
    rows = [{"window_id":"window-001", "window_start_seconds":0, "window_end_seconds":86400,
             "commit_end_seconds":20000, "accepted":False, **failure}]
    files = {
        "run_identity.json": run_identity,
        "initial_state_identity.json": identity,
        "window_summary.json": {"window_count": 1, "windows": rows, "database_writes":0},
        "window_001.json": failure,
        "rolling_progress.json": {"status": failure["status"], "windows": rows, "failure": failure, "database_writes":0},
        "final_schedule.json": {"factory_id":1, "schedule":[], "running_boundary_operations":running, "accounted_operation_keys":sorted(running_keys), "database_writes":0},
        "final_state.json": {"status": failure["status"], "accounting":accounting, "database_writes":0},
        "completion_accounting.json": accounting,
        "cp_status_summary.json": {"optimal_windows":0,"feasible_windows":0,"unknown_windows":0,"infeasible_windows":0,"not_reached_windows":1,"first_cp_status":"NOT_REACHED","database_writes":0},
        "runtime_summary.json": {"status":failure["status"],"total_wall_seconds":None,"observed_runtime":"process stopped after unsafe multi-GB preparation growth","database_writes":0},
        "replay_report.json": {"status":"NOT_REACHED","database_writes":0},
        "physical_validation.json": {"status":"NOT_REACHED","database_writes":0},
        "scope_validation.json": {"status":"NOT_REACHED","database_writes":0},
        "final_kpis.json": {"status":"NOT_CALCULATED_INCOMPLETE_RUN","database_writes":0},
    }
    for name,payload in files.items():
        (OUT/name).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str)+"\n", encoding="utf-8")
    output_hashes = {p.name: sha_file(p) for p in sorted(OUT.glob("*.json"))}
    (OUT/"artifact_hash_manifest.json").write_text(json.dumps({"algorithm":"SHA-256","output_hashes":output_hashes,"protected_before":before,"database_writes":0}, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    after = {k: tree_hash(v) for k,v in protected.items()}
    output_ok = all(sha_file(OUT/n)==h for n,h in output_hashes.items())
    verification = {"all_hashes_verified": bool(output_ok and before==after),"output_hashes_verified":output_ok,"protected_artifacts_unchanged":before==after,"workload_identity_verified":identity.get("counts")=={"orders":335,"state_operations":2699,"optimizable_operations":2692,"running_operations":7,"future_release_operations":1447},"database_writes":0}
    (OUT/"hash_verification.json").write_text(json.dumps(verification, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps({"status":failure["status"],"first_blocker":failure["failure_classification"],"workload_hash_verified":verification["workload_identity_verified"],"artifact_hashes_verified":verification["all_hashes_verified"],"database_writes":0}, indent=2))

if __name__ == "__main__": main()
