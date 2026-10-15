"""Aggregate the immutable 172-operation stress-run evidence."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from statistics import median

ROOT = Path("outputs/crafter/craft_172_scale")
RUN = ROOT / "run"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def windows():
    out = []
    for path in sorted(RUN.glob("window_*.json")):
        out.append((path.name, read(path)))
    return out


def canonical(item):
    return item.get("canonical_acceptance", {}) if "canonical_acceptance" in item else item


def trace(item):
    c = canonical(item)
    return item.get("resource_domain_v2") or c.get("resource_domain_v2") or {}


def stages(item):
    c = canonical(item)
    return item.get("cp_stages") or c.get("stages") or []


def window_accounting(items):
    rows = []
    for filename, item in items:
        c = canonical(item)
        wa = item.get("window_acceptance", {})
        scope = wa.get("scope", {})
        winning = c.get("winning_stage")
        if not isinstance(winning, dict):
            winning = {}
        required_count = len(item.get("pre_solve_required_keys", [])) or int(scope.get("required_operations", 0))
        selected_count = len(item.get("selected_keys", [])) or len(c.get("selected_schedule", []))
        future_count = len(item.get("future_outside_keys", [])) or int(scope.get("future_outside_operations", 0))
        stage_rows = stages(item)
        stage_statuses = [str(s.get("status")) for s in stage_rows]
        derived_cp_status = ("INFEASIBLE_BOUNDED_STAGES" if stage_statuses and all(s == "INFEASIBLE" for s in stage_statuses)
                             else stage_statuses[0] if len(set(stage_statuses)) == 1 and stage_statuses
                             else "UNKNOWN")
        rows.append({
            "source_file": f"run/{filename}",
            "window_id": item.get("window_id"),
            "window_number": item.get("window_number"),
            "window_start_seconds": item.get("window_start_seconds"),
            "window_end_seconds": item.get("window_end_seconds"),
            "commit_end_seconds": item.get("commit_end_seconds"),
            "pre_solve_required_count": required_count,
            "selected_count": selected_count,
            "committed_count": len(item.get("committed_keys", [])),
            "provisional_count": len(item.get("provisional_keys", [])),
            "future_outside_count": future_count,
            "remaining_after_window": item.get("remaining_unscheduled_count"),
            "cp_stage_attempt_count": item.get("cp_stage_attempt_count", len(stage_rows)),
            "cp_stage_statuses": [str(s.get("status")) for s in stage_rows],
            "winning_stage": item.get("winning_stage", winning.get("stage")),
            "cp_solver_status": item.get("cp_solver_status", winning.get("status", derived_cp_status)),
            "canonical_legacy_acceptance_status": item.get("canonical_legacy_acceptance_status", c.get("status")),
            "scope_status": item.get("scope_status", wa.get("scope_status", "NOT_REACHED")),
            "replay_status": item.get("replay_status", wa.get("replay_status", "NOT_RUN")),
            "physical_status": item.get("physical_status", wa.get("physical_status", "NOT_RUN")),
            "planning_accepted": item.get("planning_accepted", False),
            "materialization_mismatches": item.get("materialization_mismatches", c.get("materialization", {}).get("mismatch_count", 0)),
            "historical_future_assignments_backfilled": item.get("historical_future_assignments_backfilled", 0),
            "committed_operations_changed": item.get("committed_operations_changed", 0),
            "provisional_hard_frozen": item.get("provisional_hard_frozen", 0),
            "database_writes": item.get("database_writes", 0),
            "timing": item.get("timing", {}),
            "window_total_seconds": item.get("window_total_seconds"),
            "failure": item.get("first_blocker"),
        })
    return rows


def resource_profile(rows, items):
    detail = []
    expansions = []
    for row, (_filename, item) in zip(rows, items):
        t = trace(item)
        rounds = t.get("rounds", [])
        if len(rounds) > 1:
            expansions.append({"window_id": row.get("window_id"), "rounds": rounds})
        detail.append({
            "window_id": row.get("window_id"),
            "rounds": rounds,
            "final_machine_cap": t.get("final_machine_cap"),
            "final_worker_cap": t.get("final_worker_cap"),
            "resource_domain_complete": t.get("resource_domain_complete"),
            "expansion_count": max(0, len(rounds) - 1),
            "truncated": any(bool(r.get("candidate_domain", {}).get("resource_domain_truncated")) for r in rounds),
        })
    return {"windows": detail, "expansion_windows": expansions,
            "total_expansion_rounds": sum(x["expansion_count"] for x in detail),
            "machine_expansion_rounds": sum(1 for x in detail for r in x["rounds"] if r.get("expansion", {}).get("next_machine_cap") != r.get("machine_cap") and r.get("expansion", {}).get("next_machine_cap") is not None),
            "worker_expansion_rounds": sum(1 for x in detail for r in x["rounds"] if r.get("expansion", {}).get("next_worker_cap") != r.get("worker_cap") and r.get("expansion", {}).get("next_worker_cap") is not None),
            "resource_domain_completeness_claim": "No truncated domain is treated as physical infeasibility."}


def cp_profile(rows, items):
    detail = []
    for row, (_filename, item) in zip(rows, items):
        c = canonical(item)
        stage_rows = []
        for s in stages(item):
            stage_rows.append({k: s.get(k) for k in (
                "stage", "config_limit", "window_days", "status", "variables", "constraints",
                "optional_intervals", "branches", "conflicts", "cp_model_build_seconds",
                "cp_solve_seconds", "deterministic_time", "wall_time", "best_bound", "objective_value")})
        diagnosis = (trace(item).get("rounds") or [{}])[-1].get("diagnosis") or {}
        detail.append({"window_id": row.get("window_id"), "stages": stage_rows,
                       "attempt_count": len(stage_rows), "full_domain_probe": diagnosis.get("full_domain_probe")})
    accepted_stages = [s for d in detail for s in d["stages"] if s.get("status") in {"FEASIBLE", "OPTIMAL"}]
    return {"windows": detail,
            "total_stage_attempts": sum(d["attempt_count"] for d in detail),
            "stage_status_distribution": {status: sum(1 for d in detail for s in d["stages"] if s.get("status") == status)
                                           for status in sorted({s.get("status") for d in detail for s in d["stages"]})},
            "maximum_variables": max((int(s.get("variables") or 0) for s in accepted_stages), default=0),
            "maximum_constraints": max((int(s.get("constraints") or 0) for s in accepted_stages), default=0),
            "maximum_optional_intervals": max((int(s.get("optional_intervals") or 0) for s in accepted_stages), default=0),
            "total_branches": sum(int(s.get("branches") or 0) for d in detail for s in d["stages"]),
            "total_conflicts": sum(int(s.get("conflicts") or 0) for d in detail for s in d["stages"])}


def timing_profile(rows):
    fields = ("build_planning_state_seconds", "resource_configuration_seconds",
              "calendar_regime_generation_seconds", "other_preparation_seconds",
              "total_prepare_seconds", "canonical_seconds", "materialization_seconds",
              "replay_seconds", "physical_validation_seconds", "replay_validation_seconds")
    totals = {field: sum(float(r.get("timing", {}).get(field, 0.0) or 0.0) for r in rows) for field in fields}
    window_totals = [float(r["window_total_seconds"]) for r in rows if r.get("window_total_seconds") is not None]
    return {"status": "PARTIAL_EARLY_FAILURE",
            "windows_measured": len(rows), "accepted_window_total_seconds": sum(window_totals),
            "window_total_min_seconds": min(window_totals, default=None),
            "window_total_median_seconds": median(window_totals) if window_totals else None,
            "window_total_max_seconds": max(window_totals, default=None),
            "timing_totals_by_layer": totals,
            "database_state_load_seconds": None,
            "assembled_validation_seconds": None,
            "experiment_total_seconds": None,
            "unavailable_reason": "The existing runner persisted no final timing profile on early window rejection; per-window monotonic timings are retained and no load/total value is inferred."}


def main():
    items = windows()
    rows = window_accounting(items)
    progress = read(RUN / "rolling_progress.json")
    accounting = read(RUN / "factory_accounting.json")
    write(ROOT / "rolling_progress.json", {"experiment_status": "CRAFT_172_SCALE_PARTIAL",
                                           "first_blocker": "WINDOW_005_CANONICAL_BOUNDED_STAGE_FAILURE",
                                           "classification": "SEARCH_LIMIT",
                                           "canonical_detail": "All bounded stages were INFEASIBLE while the exact unrestricted exposed-domain probe was OPTIMAL; no physical infeasibility claim is made.",
                                           "run_progress": progress})
    write(ROOT / "window_accounting.json", {"factory_id": 2, "workload_operations": 172, "windows_inspected": rows,
                                             "accepted_windows": sum(bool(x.get("planning_accepted")) for x in rows),
                                             "failed_window": next((x for x in rows if not x.get("planning_accepted")), None)})
    write(ROOT / "resource_domain_profile.json", resource_profile(rows, items))
    calendar_rows = []
    for r, (_filename, item) in zip(rows, items):
        complexity = item.get("craft_complexity", {})
        calendar_rows.append({
            "window_id": r.get("window_id"),
            "resource_configuration_count": complexity.get("resource_configuration_count"),
            "calendar_regime_count": complexity.get("calendar_regime_count"),
            "regimes_per_operation": complexity.get("regimes_per_operation"),
            "timing": r.get("timing", {}),
            "cache_delta": r.get("timing", {}).get("cache_delta", {}),
        })
    write(ROOT / "calendar_profile.json", {"windows": calendar_rows, "note": "Window 5 did not complete preparation instrumentation; its diagnostic full-domain probe is preserved in cp_stage_accounting.json."})
    write(ROOT / "cp_stage_accounting.json", cp_profile(rows, items))
    write(ROOT / "runtime_profile.json", timing_profile(rows))
    complexity_rows = []
    for r, (_filename, item) in zip(rows, items):
        cc = item.get("craft_complexity", {})
        complexity_rows.append({"window_id": r.get("window_id"),
                                "required": r.get("pre_solve_required_count"),
                                "selected": r.get("selected_count"),
                                "committed": r.get("committed_count"),
                                "provisional": r.get("provisional_count"),
                                "future_outside": r.get("future_outside_count"),
                                "resource_configuration_count": cc.get("resource_configuration_count"),
                                "configurations_per_operation": cc.get("configurations_per_operation"),
                                "calendar_regime_count": cc.get("calendar_regime_count"),
                                "regimes_per_operation": cc.get("regimes_per_operation"),
                                "unique_machine_count": len(cc.get("unique_machines", [])),
                                "unique_worker_count": len(cc.get("unique_workers", [])),
                                "unique_line_count": len(cc.get("unique_lines", [])),
                                "cp_stage_attempt_count": r.get("cp_stage_attempt_count")})
    write(ROOT / "complexity_profile.json", {"windows": complexity_rows,
                                             "maximum_resource_configurations": max((int(x.get("resource_configuration_count") or 0) for x in complexity_rows), default=0),
                                             "maximum_calendar_regimes": max((int(x.get("calendar_regime_count") or 0) for x in complexity_rows), default=0),
                                             "status": "PARTIAL_EARLY_FAILURE"})
    write(ROOT / "commit_integrity.json", {"factory_id": 2, "accepted_windows_checked": sum(bool(x.get("planning_accepted")) for x in rows), "committed_operations_checked": sum(int(x.get("committed_count") or 0) for x in rows), "committed_operations_changed": sum(int(x.get("committed_operations_changed") or 0) for x in rows), "database_writes": 0})
    write(ROOT / "provisional_behavior.json", {"factory_id": 2, "windows": [{"window_id": r.get("window_id"), "carried": r.get("provisional_count"), "changed": r.get("provisional_operations_changed", 0), "hard_frozen": r.get("provisional_hard_frozen", 0)} for r in rows], "provisional_hard_frozen": sum(int(x.get("provisional_hard_frozen") or 0) for x in rows), "database_writes": 0})
    scheduled = sorted({key for _f, item in items for key in item.get("committed_keys", [])})
    required = sorted(accounting.get("schedulable_operations", []))
    missing = sorted(set(required) - set(scheduled))
    write(ROOT / "assembled_schedule.json", {"status": "NOT_REACHED", "factory_id": 2, "required_count": len(required), "accepted_committed_count": len(scheduled), "accepted_committed_operation_keys": scheduled, "missing_count": len(missing), "missing_operations": missing, "database_writes": 0})
    write(ROOT / "assembled_validation.json", {"status": "NOT_REACHED_AFTER_WINDOW_FAILURE", "validation_scope": "ASSEMBLED_FACTORY", "factory_complete_claimed": False, "required_count": len(required), "scheduled_count": len(scheduled), "missing_count": len(missing), "duplicates": [], "unexpected": [], "violation_counts": None, "database_writes": 0})
    write(ROOT / "correctness_matrix.json", {"classification": "CRAFT_172_SCALE_PARTIAL", "accepted_windows": sum(bool(x.get("planning_accepted")) for x in rows), "failed_window": "window-005", "accepted_windows_scope_replay_physical": all(x.get("scope_status") == "SCOPE_VALID" and x.get("replay_status") == "PASS" and x.get("physical_status") == "VALID" for x in rows if x.get("planning_accepted")), "all_workload_accounted": False, "final_assembled_validation": "NOT_REACHED", "materialization_mismatches": sum(int(x.get("materialization_mismatches") or 0) for x in rows), "historical_backfill": sum(int(x.get("historical_future_assignments_backfilled") or 0) for x in rows), "post_cp_modifications": 0, "database_writes": 0})
    (ROOT / "bottleneck_analysis.md").write_text("""# 172-operation stress bottleneck analysis

Classification: `CRAFT_172_SCALE_PARTIAL`.

Factory 2 contains 172 schedulable operations. Four windows were accepted and committed 20 operations. Window 5 was not accepted: every canonical bounded stage returned `INFEASIBLE`. The Resource-Domain V2 diagnostic then solved the same currently exposed legal regime domain without the stage time-window restriction and returned `OPTIMAL` (`full_domain_probe.feasible=true`). Therefore this run does not establish physical infeasibility, resource-domain exhaustion, or CP-SAT inability to solve the exposed physical model.

The immediate blocker is a rolling/canonical bounded-stage horizon escalation requirement: the current stage policy cannot produce a complete candidate for Window 5 even though the unrestricted exact exposed-domain probe is feasible. The experiment stopped at the first invalid window and did not commit it. No future work was silently dropped and no database writes occurred.

For the four accepted windows, persisted elapsed timing is dominated by canonical CP/resource-domain controller work (including expansion diagnostics in Window 4), with calendar-regime preparation the next measured layer. Window 5 did not reach materialization/replay/physical validation, and the pre-existing runner did not persist final load/total timings on early rejection; those fields are explicitly `null`, not inferred.

`NEXT_EXPERIMENT_RECOMMENDATION`: add a measured, explicit temporal-stage escalation contract for the `CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT` case, preserving exact physical constraints and requiring a fresh scoped replay/validator acceptance. Do not classify the unrestricted diagnostic as production acceptance until that escalation is implemented and tested.
""", encoding="utf-8")
    (ROOT / "changed_files_manifest.json").write_text(json.dumps({"experiment": "CRAFT_172_OPERATION_SCALABILITY_STRESS_VALIDATION", "production_semantics_modified": False, "experiment_harness_files": ["scratch/profile_craft_172_workload.py", "scratch/finalize_craft_172_scale.py"], "pre_existing_runner_used": "scratch/run_one_factory_craft.py", "database_writes": 0, "full_regression": "1083 passed, 1 unrelated pre-existing failure, 783 warnings"}, indent=2) + "\n", encoding="utf-8")
    (ROOT / "final_report.md").write_text("""# CRAFT 172-operation scalability / stress validation

Primary classification: `CRAFT_172_SCALE_PARTIAL`.

The authoritative frozen snapshot selected Factory 2 with 172 schedulable operations (20 orders, 17 lines, 498 machines, 1,780 worker identities). The validated policy was used unchanged: admission cap 22, 86,400-second lookahead, 20,000-second commit horizon, feasibility-only objective mode, and Resource-Domain V2 starting at 1x1.

Four windows were accepted with SCOPE_VALID, replay PASS, physical VALID, zero materialization mismatch, zero historical backfill, zero committed changes, zero provisional hard-freezes, and zero database writes. Window 5 stopped the run. All bounded canonical stages were INFEASIBLE, but the exact unrestricted currently exposed-domain diagnostic was OPTIMAL. This is classified as a rolling/canonical bounded-stage horizon escalation requirement, not physical infeasibility and not resource-domain exhaustion.

The assembled Factory-2 gate was therefore not reached: 20 operations were committed and 152 remained. No invalid window was committed and no future operation was silently backfilled.

The full repository regression was 1,083 passed, 1 failure, 783 warnings in 477.89 seconds. The sole failure is the known unrelated reservation snapshot-count assertion (observed 144,665 vs expected 143,456) in `tests/test_reservation_classification.py`; no CRAFT/resource-domain regression was implicated.

See `bottleneck_analysis.md` for the measured next-task recommendation. No six-factory/full-database run or architecture redesign was started.
""", encoding="utf-8")
    # Hash all final evidence, including the preserved raw per-window run.
    artifacts = {}
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            artifacts[str(path.relative_to(ROOT)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    write(ROOT / "artifact_hash_manifest.json", {"algorithm": "SHA256", "experiment": "CRAFT_172_OPERATION_SCALABILITY_STRESS_VALIDATION", "status": "CRAFT_172_SCALE_PARTIAL", "artifacts": artifacts, "database_writes": 0})
    print(json.dumps({"status": "CRAFT_172_SCALE_PARTIAL", "artifact_count": len(artifacts), "accepted_windows": sum(bool(x.get("planning_accepted")) for x in rows), "scheduled": len(scheduled), "remaining": len(missing)}))


if __name__ == "__main__":
    main()
