"""Factory-2 Step-4D rolling validation from a hash-pinned captured input.

The capture is a new controlled experiment, not proof of equivalence with
historical mixed-source Step-4 outputs. Scheduling is forbidden from querying
the database after the capture is loaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get(
    "CRAFT_STEP4D_OUT",
    str(ROOT / "outputs" / "crafter" / "factory2_172_step4d_rerun_1"),
))


def write(name: str, value) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_evidence(stage: str) -> dict:
    return {
        "stage": stage,
        "benchmark_runner": "scratch/run_one_factory_craft.py::run",
        "admission": "crafter.repair.planning::select_precedence_safe_window",
        "step3a": "crafter.repair.planning::boundary_readiness_for_operation",
        "step3b": "crafter.repair.rolling_horizon_v2::decide_rolling_orchestration_v2",
        "step3b_execution": "crafter.repair.rolling_horizon_v2::apply_rolling_orchestration_decision_v2",
        "normal_resource_domain": "crafter.repair.resource_domain_v3::run_v3_planning_schedule",
        "adaptive_recovery": "scratch.run_one_factory_craft::_attempt_route_aware_crew_recovery",
        "crew_generation": "crafter.repair.route_aware_crew_expansion::run_route_aware_adaptive_crew_expansion",
        "regimes": "crafter.repair.calendar_regimes::generate_calendar_regimes",
        "cp_sat": "crafter.repair.solve::run_canonical_schedule",
        "window_validation": "crafter.repair.scoped_validation::validate_planning_window",
        "boundary": "crafter.repair.global_planner::GlobalCRAFTPlanner.boundary_state_from_accepted_repair",
        "next_window": "scratch/run_one_factory_craft.py::run current_start = commit_end",
        "database_writes": 0,
    }


def _hash_manifest() -> dict:
    excluded = {"artifact_hash_manifest.json", "hash_verification.json"}
    rows = []
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.name not in excluded:
            rows.append({"path": path.name, "sha256": digest(path), "bytes": path.stat().st_size})
    return {"algorithm": "SHA-256", "artifacts": rows, "artifact_count": len(rows)}


def main() -> int:
    capture = os.environ.get("CRAFT_STEP4D_CAPTURE_PATH")
    if not capture:
        raise RuntimeError("Factory-2 Step4D requires a captured, hash-pinned input")
    capture_path = Path(capture).resolve()
    manifest = json.loads((capture_path.parent / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("capture_version") != 1 or
            manifest.get("classification") !=
            "NEW_CONTROLLED_SNAPSHOT_CAPTURE_NOT_HISTORICAL_EQUIVALENCE" or
            manifest.get("factory_id") != 2 or
            manifest.get("factory_operations") != 172 or
            digest(capture_path) != manifest.get("input_sha256")):
        raise RuntimeError("Factory-2 capture identity failed")
    if OUT.exists() and any(OUT.iterdir()):
        raise RuntimeError(f"refusing to overwrite an existing benchmark output: {OUT}")
    OUT.mkdir(parents=True, exist_ok=True)
    write("production_path_before.json", {
        **_path_evidence("BEFORE_STEP4D_WIRING"),
        "step3b_control_path": "PARTIALLY_OBSERVATIONAL",
        "adaptive_crew_recovery": "OPTIONAL_DIAGNOSTIC_ONLY",
    })
    write("step3b_control_path_before.json", {
        "status": "PARTIALLY_OBSERVATIONAL",
        "evidence": "historical scratch/run_factory2_172_step4.py wrapped selector telemetry but did not execute decision actions",
        "database_writes": 0,
    })
    production_v1 = os.environ.get("CRAFT_PRODUCTION_V1", "0") == "1"
    os.environ.update({
        "CRAFT_ONE_FACTORY_ID": "2",
        "CRAFT_ONE_FACTORY_NORMAL_CAP": "22",
        "CRAFT_ONE_FACTORY_MAX_MACHINE_SETS": "1",
        "CRAFT_ONE_FACTORY_MAX_WORKER_CREWS": "1",
        "CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V3": "0" if production_v1 else "1",
        "CRAFT_ONE_FACTORY_STEP4D": "1",
        "CRAFT_ONE_FACTORY_ADAPTIVE_CREW_RECOVERY": "0" if production_v1 else "1",
        "CRAFT_ONE_FACTORY_OUT": str(OUT),
        "CRAFT_ONE_FACTORY_CAPTURE_PATH": str(capture_path),
        "CRAFT_ONE_FACTORY_CAPTURE_SHA256": manifest["input_sha256"],
    })
    import scratch.run_one_factory_craft as runner
    # A captured benchmark must not consult any database while scheduling.
    from sqlalchemy import event
    from database.db import engine

    def reject_database_query(*_args, **_kwargs):
        raise RuntimeError("captured Factory-2 benchmark attempted a database query")

    event.listen(engine, "before_cursor_execute", reject_database_query)

    started = time.perf_counter()
    error = None
    try:
        result = runner.run()
    except Exception as exc:  # retain exact failure rather than repairing in-run
        error = {"type": type(exc).__name__, "message": str(exc)}
        result = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE", "error": error,
                  "database_writes": 0}
    elapsed = time.perf_counter() - started

    progress = json.loads((OUT / "rolling_progress.json").read_text(encoding="utf-8")) if (OUT / "rolling_progress.json").exists() else {}
    windows = progress.get("windows", [])
    accounting = json.loads((OUT / "factory_accounting.json").read_text(encoding="utf-8")) if (OUT / "factory_accounting.json").exists() else {}
    validation = json.loads((OUT / "assembled_factory_validation.json").read_text(encoding="utf-8")) if (OUT / "assembled_factory_validation.json").exists() else {}
    required = set(accounting.get("schedulable_operations", []))
    assembled_payload = json.loads((OUT / "assembled_factory_schedule.json").read_text(encoding="utf-8")) if (OUT / "assembled_factory_schedule.json").exists() else {}
    window_commits = [key for row in windows if row.get("accepted")
                      for key in row.get("committed_keys", [])]
    actual = list(assembled_payload.get("assembled_operation_keys",
                                        progress.get("cumulative_committed", window_commits)))
    actual_set = set(actual)
    duplicates = sorted(key for key, count in Counter(actual).items() if count > 1)
    remaining = sorted(required - actual_set)

    source_files = [
        ROOT / "crafter" / "disruptions" / "benchmark.py",
        ROOT / "scratch" / "run_one_factory_craft.py",
        ROOT / "crafter" / "repair" / "planning.py",
    ]
    fingerprint = hashlib.sha256("".join(digest(path) for path in source_files).encode()).hexdigest()
    write("starting_state.json", {
        "factory_id": 2, "snapshot_as_of_seconds": 0,
        "initial_schedulable_operations": len(required), "orders": 20, "lines": 17,
        "machines": 498, "workers": 1780, "database_writes": 0,
    })
    write("benchmark_identity.json", {
        "source": "FROZEN_SNAPSHOT_CAPTURE", "factory_id": 2,
        "snapshot_as_of_seconds": 0, "initial_operation_count": len(required),
        "capture_sha256": manifest["input_sha256"],
        "capture_classification": manifest["classification"],
        "source_file_sha256": {str(path.relative_to(ROOT)): digest(path) for path in source_files},
        "benchmark_fingerprint": fingerprint, "database_writes": 0,
    })
    write("production_path_after.json", {
        **_path_evidence("AFTER_STEP4D_WIRING"),
        "step3b_control_path": "AUTHORITATIVE",
        "adaptive_crew_recovery": "TRIGGERED_ONLY_AFTER_FAILED_TRUNCATED_WORKER_DOMAIN",
    })
    write("step3b_control_path_after.json", {
        "status": "AUTHORITATIVE",
        "actions": ["SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED", "NEIGHBORHOOD_EXPANSION_REQUIRED", "FAST_FORWARD_TO_EVENT", "NO_EXECUTABLE_WORK", "UNRESOLVED_ROLLING_STATE"],
        "controller": "apply_rolling_orchestration_decision_v2",
        "database_writes": 0,
    })
    write("window_summary.json", {"window_count": len(windows), "windows": windows, "database_writes": 0})
    write("rolling_progress.json", progress)
    recovery_rows = [
        {"window_id": row.get("window_id"), "recovery": row.get("adaptive_crew_recovery")}
        for row in windows if row.get("adaptive_crew_recovery")
    ]
    write("adaptive_recovery_summary.json", {
        "attempt_count": len(recovery_rows), "attempts": recovery_rows,
        "database_writes": 0,
    })
    action_rows = [row.get("step3b", {}).get("decision") for row in windows if row.get("step3b")]
    write("step3b_action_summary.json", {"counts": dict(Counter(action_rows)), "database_writes": 0})
    write("accounting_audit.json", {
        "initial_operation_keys": sorted(required), "final_accounted_operation_keys": sorted(actual_set),
        "missing_operation_keys": sorted(required - actual_set), "duplicate_operation_keys": duplicates,
        "unexpected_operation_keys": sorted(actual_set - required), "remaining_operation_keys": sorted(remaining),
        "database_writes": 0,
    })
    write("boundary_integrity_audit.json", {
        "committed_operations": len(actual),
        "committed_operations_changed": sum(int(row.get("committed_operations_changed", 0)) for row in windows),
        "provisional_hard_frozen": sum(int(row.get("provisional_hard_frozen", 0)) for row in windows),
        "database_writes": 0,
    })
    timings = [row.get("timing", {}) for row in windows]
    write("runtime_profile.json", {
        "benchmark_wall_seconds": elapsed,
        "window_total_seconds": sum(float(row.get("window_total_seconds", 0)) for row in windows),
        "planning_seconds": sum(float(row.get("build_planning_state_seconds", 0)) for row in timings),
        "resource_generation_seconds": sum(float(row.get("resource_configuration_seconds", 0)) for row in timings),
        "regime_generation_seconds": sum(float(row.get("calendar_regime_generation_seconds", 0)) for row in timings),
        "cp_seconds": sum(float(row.get("total_cp_solve_seconds_all_stages", 0)) for row in windows),
        "replay_seconds": sum(float(row.get("replay_seconds", 0)) for row in timings),
        "validator_seconds": sum(float(row.get("physical_validation_seconds", 0)) for row in timings),
        "database_writes": 0,
    })
    complexities = [row.get("craft_complexity", {}) for row in windows]
    write("complexity_profile.json", {
        "peak_cp_variables": max((int(row.get("winning_stage_variables") or 0) for row in windows), default=0),
        "peak_cp_constraints": max((int(row.get("winning_stage_constraints") or 0) for row in windows), default=0),
        "peak_optional_intervals": max((int(row.get("winning_stage_optional_intervals") or 0) for row in windows), default=0),
        "peak_resource_configurations": max((int(row.get("resource_configuration_count", 0)) for row in complexities), default=0),
        "peak_calendar_regimes": max((int(row.get("calendar_regime_count", 0)) for row in complexities), default=0),
        "database_writes": 0,
    })
    write("quality_metrics.json", {"status": "NOT_ESTABLISHED", "reason": "only existing validated metrics may be reported after completed assembled schedule", "database_writes": 0})
    write("final_replay.json", validation.get("replay", {"status": "NOT_REACHED"}))
    write("final_physical_validation.json", validation.get("physical", {"status": "NOT_REACHED"}))
    write("comparison_with_step4_baseline.json", {
        "historical_step4": {"status": "CRAFT_FACTORY2_172_V1_BLOCKED", "windows_attempted": 14, "accepted": 13, "committed": 56, "remaining": 116},
        "step4d": {"status": result.get("status"), "windows_attempted": len(windows), "committed": len(actual_set), "remaining": len(remaining)},
        "database_writes": 0,
    })
    if not result.get("status", "").endswith("VALIDATED"):
        write("failure_diagnosis.json", {
            "first_failure_window": result.get("window_id") or result.get("window_number"),
            "first_blocker": result.get("first_blocker", "UNKNOWN"), "result": result,
            "remaining_operation_count": len(remaining), "committed_operation_count": len(actual_set),
            "physical_infeasibility_proven": False, "database_writes": 0,
        })
    full_success = (
        result.get("status", "").endswith("VALIDATED") and len(actual_set) == 172
        and not duplicates and not (required - actual_set) and not (actual_set - required)
        and bool(validation.get("accepted"))
    )
    status = "CRAFT_FACTORY2_172_STEP4D_VALIDATED" if full_success else "CRAFT_FACTORY2_172_STEP4D_PARTIAL"
    write("implementation_report.json", {
        "status": status, "runner_status": result.get("status"), "initial_operations": len(required),
        "windows_attempted": len(windows), "windows_accepted": sum(bool(row.get("accepted")) for row in windows),
        "accounted": len(actual_set), "remaining": len(remaining), "duplicates": duplicates,
        "missing": sorted(required - actual_set), "unexpected": sorted(actual_set - required),
        "step3b_control_path": "AUTHORITATIVE", "database_writes": 0,
    })
    write("changed_files_manifest.json", {"production": [
        "crafter/repair/rolling_horizon_v2.py", "scratch/run_one_factory_craft.py"],
        "harness": ["scratch/run_factory2_172_step4d.py"], "database_writes": 0})
    report = read = json.loads((OUT / "implementation_report.json").read_text(encoding="utf-8"))
    (OUT / "implementation_report.md").write_text(
        f"# Factory-2 Step 4D\n\nStatus: `{report['status']}`.\n\n"
        f"Windows attempted: {report['windows_attempted']}; accounted: {report['accounted']}; remaining: {report['remaining']}.\n",
        encoding="utf-8")
    manifest = _hash_manifest(); write("artifact_hash_manifest.json", manifest)
    mismatches = [row["path"] for row in manifest["artifacts"] if digest(OUT / row["path"]) != row["sha256"]]
    write("hash_verification.json", {"verified": not mismatches, "mismatches": mismatches, "database_writes": 0})
    print(json.dumps({"status": status, "runner_status": result.get("status"),
                      "windows": len(windows), "accounted": len(actual_set), "remaining": len(remaining)}))
    return 0 if full_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
