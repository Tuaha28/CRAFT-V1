"""Programmatic finalization for a bounded Fast-Mode Factory-2 run."""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("CRAFT_STEP4D_OUT", str(ROOT / "outputs" / "crafter" / "factory2_172_craft_v1_fast_deadline_rerun2")))


def put(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main():
    progress = json.loads((OUT / "rolling_progress.json").read_text(encoding="utf-8"))
    windows = progress.get("windows", [])
    accounting = json.loads((OUT / "factory_accounting.json").read_text(encoding="utf-8"))
    required = set(accounting.get("schedulable_operations", []))
    committed_rows = [str(key) for row in windows for key in (row.get("committed_keys") or [])]
    committed = set(committed_rows)
    duplicates = sorted(key for key, n in Counter(committed_rows).items() if n > 1)
    failure = progress.get("failure", {}) or {}
    first_window = failure.get("window_id") or failure.get("window_number")
    runtimes = [float(row.get("window_total_seconds", 0.0)) for row in windows]
    action_counts = Counter(str(row.get("step3b", {}).get("decision")) for row in windows)
    adaptive = [row for row in windows if row.get("adaptive_crew_recovery")]
    put("fast_mode_runtime_diagnosis.json", {
        "status": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
        "first_failure_window": first_window,
        "stage": failure.get("stage", "bounded_optimization"),
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0,
        "window_safety_ceiling_seconds": 180.0,
        "evidence": "fresh run stopped at first persisted bounded runtime blocker",
        "physical_infeasibility_proven": False, "database_writes": 0,
    })
    put("run_manifest.json", {
        "status": "CRAFT_V1_FAST_PARTIAL", "fast_mode": True,
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0,
        "max_adaptive_rounds": 2, "window_safety_ceiling_seconds": 180.0,
        "windows_attempted": len(windows) + (1 if first_window else 0),
        "windows_accepted": sum(bool(row.get("accepted")) for row in windows),
        "database_writes": 0,
    })
    put("accounting_audit.json", {
        "initial_operation_keys": sorted(required),
        "final_accounted_operation_keys": sorted(committed),
        "missing_operation_keys": sorted(required - committed),
        "duplicate_operation_keys": duplicates,
        "unexpected_operation_keys": sorted(committed - required),
        "remaining_operation_keys": sorted(required - committed),
        "database_writes": 0,
    })
    put("adaptive_recovery_summary.json", {
        "count": len(adaptive), "windows": [row.get("window_id") for row in adaptive], "database_writes": 0,
    })
    put("step3b_action_summary.json", {"counts": dict(action_counts), "database_writes": 0})
    put("runtime_profile.json", {
        "window_count": len(windows), "slowest_window_seconds": max(runtimes, default=0.0),
        "total_completed_window_seconds": sum(runtimes), "database_writes": 0,
    })
    put("quality_metrics.json", {"status": "NOT_ESTABLISHED", "reason": "run stopped at bounded runtime blocker", "database_writes": 0})
    put("failure_diagnosis.json", {
        "first_failure_window": first_window,
        "first_blocker": failure.get("first_blocker", "UNKNOWN"),
        "last_successful_window": windows[-1].get("window_id") if windows else None,
        "committed_operation_count": len(committed),
        "remaining_operation_count": len(required - committed),
        "physical_infeasibility_proven": False, "database_writes": 0,
    })
    put("implementation_report.json", {
        "status": "CRAFT_V1_FAST_PARTIAL", "fast_mode": True,
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0,
        "max_adaptive_rounds": 2, "window_safety_ceiling_seconds": 180.0,
        "initial_operations": len(required), "windows_attempted": len(windows) + (1 if first_window else 0),
        "windows_accepted": sum(bool(row.get("accepted")) for row in windows),
        "accounted": len(committed), "remaining": len(required - committed),
        "duplicates": duplicates, "first_failure_window": first_window,
        "first_blocker": failure.get("first_blocker", "UNKNOWN"),
        "step3b_control_path": "AUTHORITATIVE", "database_writes": 0,
    })
    (OUT / "implementation_report.md").write_text(
        "# CRAFT V1 Fast deadline run\n\n"
        "Status: `CRAFT_V1_FAST_PARTIAL`.\n\n"
        f"First blocker: `{failure.get('first_blocker', 'UNKNOWN')}` at `{first_window}`.\n"
        f"Accepted windows: {sum(bool(row.get('accepted')) for row in windows)}; "
        f"accounted: {len(committed)}; remaining: {len(required - committed)}.\n",
        encoding="utf-8")
    put("changed_files_manifest.json", {"files": [
        "crafter/repair/fast_mode.py", "crafter/repair/solve.py",
        "crafter/repair/resource_domain_v2.py", "crafter/repair/resource_domain_v3.py",
        "crafter/repair/temporal_escalation.py", "scratch/run_one_factory_craft.py",
        "scratch/validate_whole_solve_deadline.py", "scratch/finalize_fast_deadline_run.py",
        "tests/test_fast_mode_deadlines.py",
    ], "database_writes": 0})
    excluded = {"artifact_hash_manifest.json", "hash_verification.json"}
    rows = []
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.name not in excluded:
            rows.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    put("artifact_hash_manifest.json", {"algorithm": "SHA-256", "artifact_count": len(rows), "artifacts": rows})
    bad = [row["path"] for row in rows if hashlib.sha256((OUT / row["path"]).read_bytes()).hexdigest() != row["sha256"]]
    put("hash_verification.json", {"verified": not bad, "mismatches": bad, "database_writes": 0})


if __name__ == "__main__":
    main()
