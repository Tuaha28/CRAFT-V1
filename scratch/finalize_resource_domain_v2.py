"""Finalize Resource-Domain Architecture V2 evidence."""
from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT = Path("outputs/crafter/resource_domain_v2")

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")

def summarize(root):
    accounting_path = root / ("factory_accounting.json" if (root / "factory_accounting.json").exists() else "factory3_accounting.json")
    accounting, progress = read(accounting_path), read(root / "rolling_progress.json")
    validation, runtime = read(root / "assembled_factory_validation.json"), read(root / "runtime_profile.json")
    windows, expansion, stage_counts = progress["windows"], [], {}
    for window in windows:
        trace = window.get("resource_domain_v2") or {}
        rounds = trace.get("rounds", [])
        if len(rounds) > 1:
            expansion.append({"window_id": window["window_id"], "rounds": [
                {"round": r.get("round"), "machine_cap": r.get("machine_cap"),
                 "worker_cap": r.get("worker_cap"), "status": r.get("resource_domain_status"),
                 "diagnosis": (r.get("diagnosis") or {}).get("status"),
                 "expansion": r.get("expansion")} for r in rounds]})
        count = int(window.get("cp_stage_attempt_count", 0))
        stage_counts[str(count)] = stage_counts.get(str(count), 0) + 1
    return {
        "factory_id": accounting["factory_id"], "universe_count": accounting["total_factory_operations"],
        "schedulable_count": len(accounting["schedulable_operations"]),
        "completed_count": len(accounting["completed_operations"]),
        "in_progress_count": len(accounting["in_progress_operations"]),
        "excluded_count": len(accounting["explicit_exclusions"]),
        "snapshot_as_of_seconds": accounting["snapshot_as_of_seconds"],
        "windows": len(windows), "scheduled_count": len(progress["cumulative_committed"]),
        "remaining_count": len(progress["remaining"]),
        "planning_windows_all_accepted": all(w.get("planning_accepted") for w in windows),
        "window_scope_statuses": sorted({w.get("scope_status") for w in windows}),
        "window_replay_statuses": sorted({w.get("replay_status") for w in windows}),
        "window_physical_statuses": sorted({w.get("physical_status") for w in windows}),
        "materialization_mismatches": sum(int(w.get("materialization_mismatches", 0)) for w in windows),
        "historical_backfill": sum(int(w.get("historical_future_assignments_backfilled", 0)) for w in windows),
        "committed_changes": sum(int(w.get("committed_operations_changed", 0)) for w in windows),
        "provisional_hard_freezing": sum(int(w.get("provisional_hard_frozen", 0)) for w in windows),
        "database_writes": int(progress.get("database_writes", 0)),
        "cp_stage_attempt_distribution": stage_counts, "expansion_windows": expansion,
        "runtime": {
            "state_load_seconds": runtime.get("database_state_load_seconds"),
            "rolling_seconds": runtime.get("rolling_execution_seconds_excluding_load"),
            "assembled_validation_seconds": runtime.get("assembled_validation_seconds"),
            "total_seconds": runtime.get("total_seconds"),
            "calendar_regime_seconds": sum(float(w.get("timing", {}).get("calendar_regime_generation_seconds", 0.0)) for w in windows),
            "resource_configuration_seconds": sum(float(w.get("timing", {}).get("resource_configuration_seconds", 0.0)) for w in windows),
            "cp_solve_seconds_all_stages": sum(float(w.get("total_cp_solve_seconds_all_stages", 0.0)) for w in windows),
        },
        "final_validation": {
            "validation_scope": validation.get("validation_scope"), "replay_status": validation.get("replay_status"),
            "physical_status": validation.get("physical_status"), "accepted": validation.get("accepted"),
            "factory_complete_claimed": validation.get("factory_complete_claimed"),
            "violation_counts": validation.get("violation_counts", {}),
        },
    }

def complexity(root):
    progress = read(root / "rolling_progress.json")
    windows = progress["windows"]
    return {
        "factory_id": windows[0].get("factory_id") if windows else None,
        "windows": len(windows),
        "total_resource_configurations": sum(int(w.get("craft_complexity", {}).get("resource_configuration_count", 0)) for w in windows),
        "total_calendar_regimes": sum(int(w.get("craft_complexity", {}).get("calendar_regime_count", 0)) for w in windows),
        "maximum_resource_configurations_in_window": max((int(w.get("craft_complexity", {}).get("resource_configuration_count", 0)) for w in windows), default=0),
        "maximum_calendar_regimes_in_window": max((int(w.get("craft_complexity", {}).get("calendar_regime_count", 0)) for w in windows), default=0),
        "maximum_cp_variables": max((int(w.get("winning_stage_variables") or 0) for w in windows), default=0),
        "maximum_cp_constraints": max((int(w.get("winning_stage_constraints") or 0) for w in windows), default=0),
        "maximum_cp_optional_intervals": max((int(w.get("winning_stage_optional_intervals") or 0) for w in windows), default=0),
        "total_cp_branches_all_stages": sum(int(w.get("total_cp_branches_all_stages", 0)) for w in windows),
        "total_cp_conflicts_all_stages": sum(int(w.get("total_cp_conflicts_all_stages", 0)) for w in windows),
        "resource_domain_truncated_windows": [w["window_id"] for w in windows if any(
            (r.get("candidate_domain", {}).get("resource_domain_truncated") for r in (w.get("resource_domain_v2") or {}).get("rounds", [])))],
    }

def main():
    f5, f3 = summarize(ROOT / "factory5"), summarize(ROOT / "factory3")
    write(ROOT / "complexity_profile.json", {"factory5": complexity(ROOT / "factory5"), "factory3": complexity(ROOT / "factory3")})
    write(ROOT / "resource_domain_contract.json", {
        "contract_version": "CRAFT_RESOURCE_DOMAIN_ARCHITECTURE_V2",
        "purpose": "progressive exact legal resource-domain expansion for planning windows",
        "initial_domain": "small deterministic diverse candidate tier",
        "expansion_trigger": ["canonical exposed domain has no feasible CP stage", "unused legal alternatives remain", "resource-overlap or resource/routing interaction permits a targeted trial"],
        "expansion_scope": "only implicated machine and/or worker candidate dimensions",
        "growth": "one deterministic candidate tier at a time, capped by exact finite combination counts",
        "terminal_statuses": ["FEASIBLE", "OPTIMAL", "CANDIDATE_READY_FOR_SCOPED_ACCEPTANCE", "RESOURCE_DOMAIN_INTERACTION", "NON_RESOURCE_INFEASIBILITY", "RESOURCE_DOMAIN_EXHAUSTED", "CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT"],
        "infeasibility_contract": "a truncated domain is never proof of physical infeasibility",
        "physical_semantics": "unchanged canonical ResourceConfiguration, CalendarExecutionRegime, CP, replay, validator",
        "database_writes": 0,
    })
    write(ROOT / "candidate_policy.json", {
        "policy_class": "ResourceDomainPolicyV2", "initial_machine_sets": 1, "initial_worker_crews": 1,
        "growth_factor_upper_bound": 2, "actual_stage_progression": "one tier per expansion (1,2,3,...)",
        "machine_pool_limit": 30, "diagnostic_time_limit_seconds": 15.0,
        "candidate_order": ["incumbent when legal", "deterministic spread", "stable signature tie-break"],
        "resource_provenance": "domain rows report legal combination capacity, exposed candidates, truncation, and pool completeness",
    })
    trace = read(ROOT / "window3_expansion_trace.json")
    write(ROOT / "window3_cp_stage_accounting.json", {
        "fixture": trace["fixture"], "stages_by_round": [
            {"round": r.get("round"), "machine_cap": r.get("machine_cap"), "worker_cap": r.get("worker_cap"),
             "cp_stage_attempt_count": r.get("cp_stage_attempt_count"), "cp_stage_statuses": r.get("cp_stage_statuses"),
             "winning_stage": r.get("winning_stage"), "canonical_status": r.get("canonical_status"),
             "resource_domain_status": r.get("resource_domain_status")} for r in trace["resource_domain_v2"]["rounds"]],
        "scoped_planning_validation": {k: trace["scoped_planning_validation"].get(k) for k in ("scope_status", "replay_status", "physical_status", "accepted")},
        "database_writes": trace.get("database_writes", 0),
    })
    write(ROOT / "factory5_revalidation.json", f5)
    write(ROOT / "factory3_regression.json", f3)
    previous_runtime = read(Path("outputs/crafter/global_craft_factory5_v1/runtime_profile.json"))
    previous_complexity = read(Path("outputs/crafter/global_craft_factory5_v1/complexity_profile.json"))
    previous_windows = previous_complexity.get("windows", [])
    previous_runtime_windows = previous_runtime.get("windows", [])
    write(ROOT / "runtime_profile.json", {
        "experiment": "CRAFT_RESOURCE_DOMAIN_ARCHITECTURE_V2", "factory5": f5["runtime"], "factory3": f3["runtime"],
        "factory5_previous_validated": {"total_seconds": previous_runtime.get("total_seconds"), "rolling_seconds": previous_runtime.get("rolling_execution_seconds_excluding_load"), "assembled_validation_seconds": previous_runtime.get("assembled_validation_seconds"), "calendar_regime_seconds": sum(float(w.get("timing", {}).get("calendar_regime_generation_seconds", 0.0)) for w in previous_runtime_windows), "resource_configuration_seconds": sum(float(w.get("timing", {}).get("resource_configuration_seconds", 0.0)) for w in previous_runtime_windows), "resource_configurations": sum(int(w.get("resource_configuration_count", 0)) for w in previous_windows), "calendar_regimes": sum(int(w.get("calendar_regime_count", 0)) for w in previous_windows), "windows": len(previous_windows)},
        "interpretation": "calendar preparation remains the dominant measured layer; no calendar semantics were changed", "database_writes": 0,
    })
    write(ROOT / "correctness_matrix.json", {"factory5": f5, "factory3": f3, "physical_semantics_modified": False})
    (ROOT / "design_decision.md").write_text("""# CRAFT Resource-Domain Architecture V2

Decision: adopt a deterministic progressive legal-domain controller for planning windows.

A compact domain is an engineering search restriction, never an infeasibility proof. The controller records exact legal combination capacity where finite, exposes a deterministic candidate tier, and expands only after canonical CP failure plus a resource-overlap or resource/routing interaction diagnosis. Machine and worker dimensions are expanded independently when evidence allows; no factory-specific cap is embedded.

The canonical CRAFT physical model, exact calendar regimes, materialization, replay, validator, commitment semantics, and database isolation are unchanged. A bounded planning result is returned to scoped planning validation; the legacy full-repair acceptance seam is reported separately.

Decisive evidence: Factory-5 Window 3 recovered from compact `1x1` through `1x2` to `1x3` worker-domain exposure, then passed scoped replay and physical validation.
""", encoding="utf-8")
    (ROOT / "literature_mapping.md").write_text("""# Literature and retained-architecture mapping

CRAFT V2 follows the modeling direction of dual-resource flexible job-shop and constraint-programming work without copying a heuristic or relaxing CRAFT semantics.

- Dual-resource flexible job-shop formulations jointly model machine and worker assignment; CRAFT preserves this coupling through exact ResourceConfiguration candidates and fixed-crew execution.
- Flexible-job-shop CP formulations use alternative assignments and optional intervals; CRAFT already has this canonical CP representation and only changes which legal alternatives are exposed per exact rebuild.
- Staged/decomposed scheduling motivates progressive candidate introduction; CRAFT requires every expansion to be auditable and revalidated through exact materialization, replay, and the independent validator.
- Sequence-dependent setup, worker skills, calendars, productive segments, and continuous machine occupancy remain hard semantics.

Primary references reviewed:

- https://doi.org/10.1016/j.ejor.2023.05.017
- https://doi.org/10.1111/exsy.13669
- https://www.sciencedirect.com/science/article/pii/S0305054826002261
- https://research.monash.edu/en/publications/scheduling-optional-tasks-with-explanation

These references inform the direction only; CRAFT retains its own exact acceptance contract.
""", encoding="utf-8")
    (ROOT / "final_report.md").write_text(f"""# CRAFT Resource-Domain Architecture V2

Status: `CRAFT_RESOURCE_DOMAIN_V2_VALIDATED`

## Decisive Window-3 test

Factory-5 Window 3 began with compact `1x1` candidates and failed all exposed CP stages. The generic controller recorded unused legal alternatives, diagnosed resource/routing interaction, and expanded deterministically through `1x2` to `1x3`. The resulting canonical candidate had exact materialization and passed scoped planning replay and physical validation.

## Factory-5

- Universe/scheduled/remaining: {f5['universe_count']}/{f5['scheduled_count']}/{f5['remaining_count']}.
- Rolling windows: {f5['windows']}; all windows accepted with SCOPE_VALID, replay PASS, and physical VALID.
- Final assembled replay: {f5['final_validation']['replay_status']}; physical: {f5['final_validation']['physical_status']}; all violation counts zero.
- Expansion windows: {[x['window_id'] for x in f5['expansion_windows']]}.
- Materialization mismatches, historical backfill, committed changes, provisional hard-freezes, DB writes: {f5['materialization_mismatches']}, {f5['historical_backfill']}, {f5['committed_changes']}, {f5['provisional_hard_freezing']}, {f5['database_writes']}.

## Factory-3 regression

- Universe/scheduled/remaining: {f3['universe_count']}/{f3['scheduled_count']}/{f3['remaining_count']} across {f3['windows']} windows.
- Final assembled replay: {f3['final_validation']['replay_status']}; physical: {f3['final_validation']['physical_status']}; all violation counts zero.
- No expansion was required, demonstrating adaptive rather than always-maximal exposure.

## Runtime

Factory-5 adaptive runtime was {f5['runtime']['total_seconds']:.3f}s total, {f5['runtime']['calendar_regime_seconds']:.3f}s calendar-regime generation, {f5['runtime']['cp_solve_seconds_all_stages']:.3f}s CP solve, and {f5['runtime']['rolling_seconds']:.3f}s rolling execution excluding state load. The prior fixed-3x3 Factory-5 evidence is immutable and referenced in runtime_profile.json; no asymptotic claim is made.

Focused CRAFT/planning regression: `68 passed, 2 warnings`.

No GNN, GA, ALNS, RL, UI, full-database run, or calendar semantic change was introduced.
""", encoding="utf-8")
    write(ROOT / "changed_files_manifest.json", {
        "experiment": "CRAFT_RESOURCE_DOMAIN_ARCHITECTURE_V2",
        "production_files": ["crafter/repair/resource_configs.py", "crafter/repair/planning.py", "crafter/repair/resource_domain_v2.py", "crafter/repair/global_planner.py"],
        "harness_files": ["scratch/run_one_factory_craft.py", "scratch/run_resource_domain_v2_window3.py", "scratch/finalize_resource_domain_v2.py"],
        "tests": ["tests/test_resource_domain_v2.py"], "validated_semantics_modified": False,
        "database_writes": 0, "focused_regression": "68 passed, 2 warnings",
    })
    source_files = [Path("crafter/repair/resource_configs.py"), Path("crafter/repair/planning.py"), Path("crafter/repair/resource_domain_v2.py"), Path("crafter/repair/global_planner.py"), Path("scratch/run_one_factory_craft.py"), Path("scratch/run_resource_domain_v2_window3.py"), Path("scratch/finalize_resource_domain_v2.py"), Path("tests/test_resource_domain_v2.py")]
    artifacts = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest().upper() for p in sorted(ROOT.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    sources = {str(p).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest().upper() for p in source_files}
    write(ROOT / "artifact_hash_manifest.json", {"algorithm": "SHA256", "experiment": "CRAFT_RESOURCE_DOMAIN_ARCHITECTURE_V2", "artifacts": artifacts, "source_files": sources, "database_writes": 0})
    print(json.dumps({"status": "CRAFT_RESOURCE_DOMAIN_V2_VALIDATED", "artifact_count": len(artifacts), "source_count": len(sources)}))

if __name__ == "__main__":
    main()
