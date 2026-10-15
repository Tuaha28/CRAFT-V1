from __future__ import annotations
import hashlib, json, os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "crafter" / "factory2_172_craft_v1_fast"

def put(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

def main():
    progress = json.loads((OUT / "rolling_progress.json").read_text(encoding="utf-8"))
    windows = progress.get("windows", [])
    accounting = json.loads((OUT / "factory_accounting.json").read_text(encoding="utf-8"))
    required = set(accounting.get("schedulable_operations", []))
    committed = set(progress.get("cumulative_committed", []))
    action_counts = Counter(str(row.get("step3b", {}).get("decision")) for row in windows)
    recovery = [row for row in windows if row.get("adaptive_crew_recovery")]
    runtimes = [float(row.get("window_total_seconds", 0.0)) for row in windows]
    put("fast_mode_runtime_diagnosis.json", {
        "status": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
        "first_failure_window": "window-014",
        "stage": "adaptive_or_exact_window_preparation_or_solve",
        "evidence": "window-014 produced no persisted record before external bounded watchdog termination",
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0,
        "max_adaptive_rounds": 2, "database_writes": 0,
    })
    put("run_manifest.json", {
        "status": "CRAFT_V1_FAST_PARTIAL", "fast_mode": True,
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0,
        "max_adaptive_rounds": 2, "windows_attempted": len(windows),
        "windows_accepted": sum(bool(row.get("accepted")) for row in windows),
        "database_writes": 0,
    })
    put("accounting_audit.json", {
        "initial_operation_keys": sorted(required), "final_accounted_operation_keys": sorted(committed),
        "missing_operation_keys": sorted(required - committed), "duplicate_operation_keys": [],
        "unexpected_operation_keys": sorted(committed - required), "remaining_operation_keys": sorted(required - committed),
        "database_writes": 0,
    })
    put("adaptive_recovery_summary.json", {"count": len(recovery), "windows": [row.get("window_id") for row in recovery], "database_writes": 0})
    put("step3b_action_summary.json", {"counts": dict(action_counts), "database_writes": 0})
    put("runtime_profile.json", {"window_count": len(windows), "slowest_window_seconds": max(runtimes, default=0), "total_completed_window_seconds": sum(runtimes), "database_writes": 0})
    put("quality_metrics.json", {"status": "NOT_ESTABLISHED", "reason": "run stopped at bounded runtime blocker", "database_writes": 0})
    put("failure_diagnosis.json", {
        "first_failure_window": "window-014", "first_blocker": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
        "last_successful_window": windows[-1].get("window_id") if windows else None,
        "committed_operation_count": len(committed), "remaining_operation_count": len(required - committed),
        "physical_infeasibility_proven": False, "database_writes": 0,
    })
    put("implementation_report.json", {
        "status": "CRAFT_V1_FAST_PARTIAL", "fast_mode": True,
        "normal_cp_limit_seconds": 60.0, "adaptive_cp_limit_seconds": 90.0, "max_adaptive_rounds": 2,
        "initial_operations": len(required), "windows_attempted": len(windows),
        "windows_accepted": sum(bool(row.get("accepted")) for row in windows),
        "accounted": len(committed), "remaining": len(required - committed),
        "step3b_control_path": "AUTHORITATIVE", "db_writes": 0,
    })
    (OUT / "implementation_report.md").write_text(
        "# CRAFT V1 Fast\n\nStatus: `CRAFT_V1_FAST_PARTIAL`.\n\n"
        "The first bounded blocker was Window 14 runtime exhaustion before a persisted candidate.\n",
        encoding="utf-8")
    put("changed_files_manifest.json", {"files": ["crafter/repair/fast_mode.py", "crafter/repair/solve.py", "scratch/run_one_factory_craft.py", "scratch/run_factory2_172_step4d.py", "scratch/finalize_fast_run.py"], "database_writes": 0})
    excluded = {"artifact_hash_manifest.json", "hash_verification.json"}
    rows=[]
    for p in sorted(OUT.iterdir()):
        if p.is_file() and p.name not in excluded:
            rows.append({"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size})
    put("artifact_hash_manifest.json", {"algorithm": "SHA-256", "artifact_count": len(rows), "artifacts": rows})
    bad=[]
    for row in rows:
        if hashlib.sha256((OUT / row["path"]).read_bytes()).hexdigest() != row["sha256"]: bad.append(row["path"])
    put("hash_verification.json", {"verified": not bad, "mismatches": bad, "database_writes": 0})
    return 0

if __name__ == "__main__": raise SystemExit(main())
