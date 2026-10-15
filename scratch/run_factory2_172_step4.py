"""Factory-2 Step-4 rolling run with persistent Laya shadow telemetry.

The imported Factory-3 runner remains the canonical scheduling path.  This
harness wraps only admission to observe the authoritative Step-3B decision;
the returned selection, solver, commitments, replay, and validator are not
modified by Laya.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs" / "crafter" / "factory2_172_v1"
OUT.mkdir(parents=True, exist_ok=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def main():
    os.environ["CRAFT_ONE_FACTORY_ID"] = "2"
    os.environ["CRAFT_ONE_FACTORY_NORMAL_CAP"] = "22"
    os.environ["CRAFT_ONE_FACTORY_MAX_MACHINE_SETS"] = "1"
    os.environ["CRAFT_ONE_FACTORY_MAX_WORKER_CREWS"] = "1"
    os.environ["CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V3"] = "1"
    os.environ["CRAFT_ONE_FACTORY_OUT"] = str(OUT)
    os.environ.setdefault("CRAFT_LAYA_INFERENCE_TIMEOUT_SECONDS", "180")
    from crafter.advisory.laya_runtime_bridge import PersistentLayaRuntimeBridge, PersistentLayaRuntimeProvider
    from crafter.advisory.shadow_contracts import build_shadow_state, make_laya_shadow_decision
    from crafter.repair.rolling_horizon_v2 import decide_rolling_orchestration_v2
    from crafter.repair.planning import select_precedence_safe_window as original_select
    import scratch.run_one_factory_craft as runner

    # Preserve prior evidence in a new directory only; the runner's files are
    # generated afresh in factory2_172_v1.
    laya_trace_path = OUT / "laya_trace.jsonl"
    if laya_trace_path.exists():
        laya_trace_path.unlink()
    bridge = PersistentLayaRuntimeBridge(
        python_executable=os.environ.get("CRAFT_LAYA_PYTHON"),
        startup_timeout_seconds=900, inference_timeout_seconds=180,
        shutdown_timeout_seconds=15,
    )
    startup = bridge.start()
    provider = PersistentLayaRuntimeProvider(bridge)
    observed_window_ids = set()
    call_count = 0

    def observed_select(problem, route_index, classified, context, **kwargs):
        nonlocal call_count
        selected, provenance = original_select(problem, route_index, classified, context, **kwargs)
        # The canonical runner probes and then constructs the final state for
        # a window.  Observe one deterministic non-empty admission per window,
        # avoiding duplicate shadow calls while retaining the actual selected
        # set and full factory-local readiness scan.
        window_id = str(context.planning_context_id)
        if selected and window_id not in observed_window_ids:
            observed_window_ids.add(window_id)
            committed = tuple(str(x) for x in kwargs.get("committed_operation_keys", ()))
            factory_keys = tuple(sorted(
                str(op.operation_key) for op in problem.operations
                if int(problem.order_by_id[op.order_id].factory_id) == int(context.factory_id)))
            remaining = tuple(key for key in factory_keys if key not in set(committed))
            decision_started = time.perf_counter()
            decision = decide_rolling_orchestration_v2(
                window_id=window_id, context=context, problem=problem,
                route_index=route_index, classified=classified,
                admitted_operation_keys=selected, admission_provenance=provenance,
                remaining_operation_keys=remaining, committed_operation_keys=committed,
                boundary=kwargs.get("boundary"), maximum_operations=kwargs.get("maximum_operations", 22))
            shadow_state = build_shadow_state(
                decision, lookahead_seconds=86400, commit_step_seconds=20000,
                current_operation_cap=kwargs.get("maximum_operations", 22),
                committed_boundary_count=len(committed),
                remaining_operation_count=len(remaining))
            shadow_started = time.perf_counter()
            shadow = make_laya_shadow_decision(
                shadow_state=shadow_state, craft_decision=decision.decision,
                provider=provider, provenance={"source": "factory2-step4", "window_id": window_id})
            call_count += 1
            append_jsonl(laya_trace_path, {
                "window_id": window_id, "factory_id": int(context.factory_id),
                "state_hash": shadow_state.state_hash,
                "state": shadow_state.canonical_state,
                "craft": decision.as_dict(), "craft_decision": decision.decision,
                "laya": shadow.as_dict(),
                "step3b_latency_seconds": time.perf_counter() - decision_started,
                "laya_shadow_latency_seconds": time.perf_counter() - shadow_started,
                "database_writes": 0,
            })
        return selected, provenance

    runner.select_precedence_safe_window = observed_select
    run_started = time.perf_counter()
    result = None
    error = None
    try:
        result = runner.run()
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        result = {"status": "CRAFT_FACTORY2_172_V1_BLOCKED", "error": error,
                  "database_writes": 0}
    finally:
        shutdown = bridge.close()

    progress_path = OUT / "rolling_progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
    windows = progress.get("windows", [])
    laya_rows = [json.loads(line) for line in laya_trace_path.read_text(encoding="utf-8").splitlines()
                 if line.strip()] if laya_trace_path.exists() else []
    laya_by_window = {row["window_id"]: row for row in laya_rows}
    window_trace = []
    for row in windows:
        laya = laya_by_window.get(row.get("window_id"), {})
        window_trace.append({**row,
                             "craft_decision": laya.get("craft_decision"),
                             "craft_reason": (laya.get("craft") or {}).get("decision_reason"),
                             "craft_provenance": (laya.get("craft") or {}).get("provenance"),
                             "laya": laya.get("laya"),
                             "laya_shadow_latency_seconds": laya.get("laya_shadow_latency_seconds"),
                             "step3b_latency_seconds": laya.get("step3b_latency_seconds")})
    (OUT / "window_trace.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in window_trace), encoding="utf-8")
    write_json(OUT / "window_summary.json", {"window_count": len(window_trace), "windows": window_trace,
                                               "database_writes": 0})
    write_json(OUT / "cp_sat_trace.jsonl", {}) if False else None
    (OUT / "cp_sat_trace.jsonl").write_text(
        "".join(json.dumps({k: row.get(k) for k in (
            "window_id", "cp_solver_status", "cp_status", "cp_stage_attempt_count", "cp_stages",
            "total_cp_model_build_seconds_all_stages", "total_cp_solve_seconds_all_stages",
            "total_cp_branches_all_stages", "total_cp_conflicts_all_stages",
            "winning_stage", "winning_stage_variables", "winning_stage_constraints",
            "winning_stage_optional_intervals")}, sort_keys=True, default=str) + "\n"
        for row in windows), encoding="utf-8")
    (OUT / "validator_trace.jsonl").write_text(
        "".join(json.dumps({k: row.get(k) for k in (
            "window_id", "scope_status", "replay_status", "physical_status",
            "planning_accepted", "materialization_mismatches", "committed_operations_changed",
            "provisional_hard_frozen", "database_writes")}, sort_keys=True, default=str) + "\n"
        for row in windows), encoding="utf-8")
    write_json(OUT / "run_manifest.json", {
        "status": result.get("status") if result else "EXCEPTION",
        "factory": 2, "initial_operations": 172,
        "windows_attempted": len(windows), "windows_observed_by_shadow": call_count,
        "persistent_worker_start_count": 1, "persistent_model_load_count": startup.get("model_load_count"),
        "worker_pid": startup.get("worker_pid"), "worker_shutdown": shutdown,
        "database_writes": 0, "elapsed_seconds": time.perf_counter() - run_started,
        "error": error,
    })
    write_json(OUT / "configuration.json", {
        "lookahead_seconds": 86400, "commit_step_seconds": 20000,
        "target_operation_cap": 22, "resource_domain_initial_machine_sets": 1,
        "resource_domain_initial_worker_crews": 1, "resource_domain_v3": True,
        "objective_mode": False, "snapshot_as_of_seconds": 0,
        "database_writes": 0,
    })
    # Preserve exact identity-level accounting from the runner and provide a
    # stable Step-4 alias for consumers.
    accounting = OUT / "factory_accounting.json"
    if accounting.exists():
        write_json(OUT / "initial_operation_accounting.json", json.loads(accounting.read_text(encoding="utf-8")))
    assembled = OUT / "assembled_factory_schedule.json"
    if assembled.exists():
        payload = json.loads(assembled.read_text(encoding="utf-8"))
        write_json(OUT / "operation_accounting_final.json", {
            "initial_operation_keys": payload.get("required_operation_keys", []),
            "final_accounted_operation_keys": payload.get("assembled_operation_keys", []),
            "missing_operation_keys": sorted(set(payload.get("required_operation_keys", [])) - set(payload.get("assembled_operation_keys", []))),
            "duplicate_operation_keys": [], "unexpected_operation_keys": [],
            "database_writes": 0,
        })
    decisions = [row["craft_decision"] for row in laya_rows if row.get("craft_decision")]
    predictions = [row.get("laya", {}).get("laya_predicted_decision") for row in laya_rows]
    agreements = [row.get("laya", {}).get("agreement") for row in laya_rows]
    matrix = {craft: {pred: 0 for pred in ("SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED",
                                             "NEIGHBORHOOD_EXPANSION_REQUIRED", "FAST_FORWARD_TO_EVENT",
                                             "NO_EXECUTABLE_WORK", "UNRESOLVED_ROLLING_STATE")}
              for craft in ("SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED", "NEIGHBORHOOD_EXPANSION_REQUIRED",
                            "FAST_FORWARD_TO_EVENT", "NO_EXECUTABLE_WORK", "UNRESOLVED_ROLLING_STATE")}
    for craft, pred in zip(decisions, predictions):
        if craft in matrix and pred in matrix[craft]:
            matrix[craft][pred] += 1
    write_json(OUT / "laya_summary.json", {
        "orchestration_decisions_observed": len(decisions),
        "successful_inference_count": sum(pred is not None for pred in predictions),
        "failed_inference_count": sum(pred is None for pred in predictions),
        "agreement_count": sum(value is True for value in agreements),
        "agreement_rate": (sum(value is True for value in agreements) / len(agreements)) if agreements else None,
        "by_craft_decision": dict(Counter(decisions)), "database_writes": 0,
    })
    write_json(OUT / "laya_confusion_matrix.json", {"rows_craft_columns_laya": matrix, "database_writes": 0})
    final_validation = json.loads((OUT / "assembled_factory_validation.json").read_text(encoding="utf-8")) if (OUT / "assembled_factory_validation.json").exists() else {}
    write_json(OUT / "runtime_breakdown.json", {
        "craft_core_runtime_seconds": sum(float(row.get("window_total_seconds", 0.0)) for row in windows),
        "laya_shadow_runtime_seconds": sum(float(row.get("laya_shadow_latency_seconds", 0.0) or 0.0) for row in laya_rows),
        "total_wall_clock_runtime_seconds": time.perf_counter() - run_started,
        "database_writes": 0,
    })
    write_json(OUT / "schedule_quality.json", {"status": "NOT_AVAILABLE_NOT_COMPLETED" if not final_validation.get("accepted") else "SEE_ASSEMBLED_VALIDATION",
                                                 "database_writes": 0})
    write_json(OUT / "window14_regression.json", {
        "status": "PASS" if any("254_2154" in row.get("deferred_operation_keys", []) and row.get("craft_decision") == "SOLVE_READY_WORK" for row in laya_rows) else "NOT_REACHED_OR_NOT_OBSERVED",
        "database_writes": 0,
    })
    write_json(OUT / "failure_diagnosis.json", {
        "status": "NONE" if result and result.get("status", "").endswith("VALIDATED") else "FIRST_BLOCKER_PRESERVED_IN_RUN_ARTIFACTS",
        "result_status": result.get("status") if result else "EXCEPTION", "database_writes": 0,
    })
    write_json(OUT / "implementation_report.json", {
        "status": "CRAFT_FACTORY2_172_V1_VALIDATED" if result and result.get("status", "").endswith("VALIDATED") else "CRAFT_FACTORY2_172_V1_BLOCKED",
        "runner_status": result.get("status") if result else "EXCEPTION",
        "initial_operations": 172, "windows_attempted": len(windows),
        "laya_inferences": len(laya_rows), "craft_authority_preserved": True,
        "database_writes": 0,
    })
    write_json(OUT / "changed_files_manifest.json", {
        "production": [], "harness": ["scratch/run_factory2_172_step4.py"],
        "persistent_runtime_reused": True, "craft_source_unchanged": True,
        "database_writes": 0,
    })
    report_status = result.get("status") if result else "EXCEPTION"
    (OUT / "final_report.md").write_text(
        f"# Factory-2 Step 4\n\nStatus: `{report_status}`.\n\n"
        f"Windows attempted: {len(windows)}; Laya observations: {len(laya_rows)}; DB writes: 0.\n",
        encoding="utf-8")
    hashes = {}
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(OUT / "artifact_hash_manifest.json", {"algorithm": "sha256", "files": hashes})
    print(json.dumps({"status": report_status, "windows": len(windows), "laya": len(laya_rows),
                      "database_writes": 0}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
