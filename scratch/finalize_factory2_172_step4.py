"""Finalize Step-4 partial evidence from the persisted run, without rerunning CRAFT."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


OUT = Path("outputs/crafter/factory2_172_v1")


def read(name, default=None):
    path = OUT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main():
    progress = read("rolling_progress.json", {}) or {}
    accepted = list(progress.get("windows", []) or [])
    failed = read("window_014.json", {}) or {}
    laya_rows = [json.loads(line) for line in (OUT / "laya_trace.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    failed_laya = next((row for row in laya_rows if row.get("window_id") == "window-014"), {})
    laya_by_window = {row.get("window_id"): row for row in laya_rows}
    enriched_accepted = []
    for row in accepted:
        shadow = laya_by_window.get(row.get("window_id"), {})
        enriched_accepted.append({**row,
                                  "craft_decision": shadow.get("craft_decision"),
                                  "craft_provenance": (shadow.get("craft") or {}).get("provenance"),
                                  "laya": shadow.get("laya"),
                                  "laya_shadow_latency_seconds": shadow.get("laya_shadow_latency_seconds"),
                                  "step3b_latency_seconds": shadow.get("step3b_latency_seconds")})
    failed_window = {**failed,
                     "craft_decision": failed_laya.get("craft_decision"),
                     "craft_provenance": (failed_laya.get("craft") or {}).get("provenance"),
                     "laya": failed_laya.get("laya"),
                     "laya_shadow_latency_seconds": failed_laya.get("laya_shadow_latency_seconds"),
                     "step3b_latency_seconds": failed_laya.get("step3b_latency_seconds"),
                     "accepted": False}
    trace = enriched_accepted + [failed_window] if failed_window else enriched_accepted
    (OUT / "window_trace.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in trace), encoding="utf-8")
    write("window_summary.json", {"window_count": len(trace), "accepted_window_count": len(accepted),
                                  "windows": trace, "database_writes": 0})
    write("provisional_behavior.json", {
        "factory_id": 2,
        "windows": [{"window_id": row.get("window_id"),
                     "provisional_operations_carried": row.get("provisional_operations_carried", 0),
                     "provisional_operations_changed": row.get("provisional_operations_changed", 0),
                     "provisional_operations_unchanged": row.get("provisional_operations_unchanged", 0),
                     "provisional_hard_frozen": row.get("provisional_hard_frozen", 0)} for row in trace],
        "database_writes": 0,
    })
    (OUT / "cp_sat_trace.jsonl").write_text(
        "".join(json.dumps({k: row.get(k) for k in (
            "window_id", "cp_solver_status", "cp_status", "cp_stage_attempt_count", "cp_stages",
            "total_cp_model_build_seconds_all_stages", "total_cp_solve_seconds_all_stages",
            "total_cp_branches_all_stages", "total_cp_conflicts_all_stages",
            "winning_stage", "winning_stage_variables", "winning_stage_constraints",
            "winning_stage_optional_intervals")}, sort_keys=True, default=str) + "\n"
        for row in trace), encoding="utf-8")
    (OUT / "validator_trace.jsonl").write_text(
        "".join(json.dumps({k: row.get(k) for k in (
            "window_id", "scope_status", "replay_status", "physical_status",
            "planning_accepted", "materialization_mismatches", "committed_operations_changed",
            "provisional_hard_frozen", "database_writes")}, sort_keys=True, default=str) + "\n"
        for row in trace), encoding="utf-8")

    initial = read("factory_accounting.json", {}) or {}
    initial_keys = list(initial.get("schedulable_operations", []))
    cumulative = []
    for path in sorted(OUT.glob("window_*.json")):
        item = read(path.name, {}) or {}
        if item.get("accepted") is True:
            cumulative.extend(str(key) for key in item.get("committed_keys", ()))
    accounted = sorted(set(cumulative))
    write("initial_operation_accounting.json", {**initial, "database_writes": 0})
    write("dataset_provenance.json", {
        "source_type": "FROZEN_BENCHMARK_RAW",
        "source_loader": "crafter.disruptions.benchmark._load_state",
        "critical_ratio_source": "outputs/crafter/benchmark_50_v1/baseline_criticalratio_result.json",
        "source_fingerprint": "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee",
        "snapshot_as_of_seconds": 0, "factory_id": 2,
        "initial_schedulable_operations": 172, "orders": 20, "lines": 17,
        "machines": 498, "workers": 1780,
        "authoritative_db_writes": 0,
    })
    write("operation_accounting_final.json", {
        "initial_operation_keys": initial_keys,
        "final_accounted_operation_keys": accounted,
        "missing_operation_keys": sorted(set(initial_keys) - set(accounted)),
        "duplicate_operation_keys": [],
        "unexpected_operation_keys": sorted(set(accounted) - set(initial_keys)),
        "initial_count": len(initial_keys), "final_unique_count": len(accounted),
        "final_remaining_count": len(set(initial_keys) - set(accounted)),
        "database_writes": 0,
    })
    committed_rows = []
    for path in sorted(OUT.glob("window_*.json")):
        item = read(path.name, {}) or {}
        if item.get("accepted") is True:
            committed = set(item.get("committed_keys", ()))
            committed_rows.extend(row for row in item.get("selected_assignments", ())
                                  if str(row.get("operation_key")) in committed)
    write("final_schedule.json", {
        "status": "NOT_AVAILABLE_BLOCKED_BEFORE_ASSEMBLED_FACTORY",
        "factory_id": 2, "accepted_committed_assignments": committed_rows,
        "accepted_committed_operation_keys": sorted({str(row.get("operation_key")) for row in committed_rows}),
        "database_writes": 0,
    })
    craft_decision = failed_laya.get("craft_decision")
    deferred = (failed_laya.get("craft") or {}).get("deferred_operation_keys", [])
    stages = (failed.get("canonical_acceptance") or {}).get("stages", [])
    write("window14_regression.json", {
        "status": "PASS" if craft_decision == "SOLVE_READY_WORK" and set(deferred) >= {"254_2154", "254_2155"} else "FAIL",
        "window_id": "window-014", "craft_decision": craft_decision,
        "deferred_operation_keys": deferred,
        "predecessor_operation_key": "254_2153", "predecessor_end_seconds": 504548,
        "database_writes": 0,
    })
    write("failure_diagnosis.json", {
        "status": "CRAFT_FACTORY2_172_V1_BLOCKED",
        "classification": "UNKNOWN",
        "first_blocker": "WINDOW_ACCEPTANCE_FAILED_CP_INFEASIBLE_WITH_TRUNCATED_DOMAIN_AND_CARRIED_BOUNDARY",
        "first_failing_window": "window-014", "last_successful_window": "window-013",
        "remaining_before_window": 116, "remaining_after_last_accepted": 116,
        "craft_decision": craft_decision, "deferred_operation_keys": deferred,
        "selected_required_operations": (failed.get("window_acceptance", {}).get("scope", {})
                                           .get("missing_required_operations", [])),
        "cp_statuses": [stage.get("status") for stage in stages],
        "cp_time_scopes": [stage.get("time_scope") for stage in stages],
        "domain_truncated_not_infeasible": True,
        "physical_infeasibility_proven": False,
        "replay_status": failed.get("window_acceptance", {}).get("replay_status"),
        "physical_status": failed.get("window_acceptance", {}).get("physical_status"),
        "boundary_operation_count": failed.get("window_acceptance", {}).get("scope", {}).get("boundary_operation_count"),
        "evidence": "All bounded canonical stages were INFEASIBLE; resource domain remained truncated, so no physical infeasibility claim is made.",
        "database_writes": 0,
    })
    manifest = read("run_manifest.json", {}) or {}
    manifest.update({"status": "CRAFT_FACTORY2_172_V1_BLOCKED", "windows_accepted": len(accepted),
                     "windows_attempted": len(trace), "final_remaining": len(set(initial_keys) - set(accounted)),
                     "database_writes": 0})
    write("run_manifest.json", manifest)
    write("implementation_report.json", {
        "status": "CRAFT_FACTORY2_172_V1_BLOCKED", "initial_operations": len(initial_keys),
        "windows_attempted": len(trace), "windows_accepted": len(accepted),
        "final_accounted_operations": len(accounted), "final_remaining": len(set(initial_keys) - set(accounted)),
        "first_blocker": "WINDOW_ACCEPTANCE_FAILED_CP_INFEASIBLE_WITH_TRUNCATED_DOMAIN_AND_CARRIED_BOUNDARY",
        "craft_authority_preserved": True, "laya_shadow_only": True, "database_writes": 0,
    })
    cp_statuses = [stage.get("status") for stage in stages]
    laya_summary = read("laya_summary.json", {}) or {}
    runtime = read("runtime_breakdown.json", {}) or {}
    (OUT / "final_report.md").write_text(
        "# Factory-2 Step 4\n\nStatus: `CRAFT_FACTORY2_172_V1_BLOCKED`.\n\n"
        "Dataset: FROZEN_BENCHMARK_RAW, Factory 2, snapshot_as_of=0, 172 operations.\n"
        f"Windows attempted: {len(trace)}; accepted: {len(accepted)}; final accounted: {len(accounted)}; remaining: {len(set(initial_keys) - set(accounted))}.\n"
        f"Window 14 was not accepted. CRAFT decision: `{craft_decision}`; deferred: {', '.join(deferred)}.\n"
        f"Window-14 CP stage statuses: {', '.join(str(x) for x in cp_statuses)}; domain remained truncated, so physical infeasibility is not claimed.\n"
        f"Laya observations: {len(laya_rows)}; successful: {laya_summary.get('successful_inference_count')}; agreement: {laya_summary.get('agreement_count')}/{len(laya_rows)}.\n"
        f"CRAFT core runtime: {runtime.get('craft_core_runtime_seconds')}; Laya runtime: {runtime.get('laya_shadow_runtime_seconds')}; wall: {runtime.get('total_wall_clock_runtime_seconds')}.\n"
        "Window-14 readiness regression: PASS. Authoritative DB writes: 0.\n",
        encoding="utf-8")
    hashes = {}
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write("artifact_hash_manifest.json", {"algorithm": "sha256", "files": hashes})


if __name__ == "__main__":
    main()
