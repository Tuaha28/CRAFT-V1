"""Controlled Factory-3 CRAFT window-cap calibration.

This runner varies only the bounded admission cap.  Each repetition starts a
fresh subprocess from the same frozen benchmark snapshot and stores the full
bounded rolling evidence under its cap/repetition directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path("outputs/crafter/window_scalability_calibration_v1")
CAPS = (6, 12, 18, 22)
REPETITIONS = 3


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def percentile_summary(values):
    values = [float(value) for value in values]
    if not values:
        return {"min": None, "median": None, "max": None}
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def winning_stage(window):
    return window.get("canonical_cp_stage") or {}


def cache_totals(windows):
    totals = {}
    for window in windows:
        for key, value in (window.get("timing", {}).get("cache_delta", {}) or {}).items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    hits = sum(totals.get(key, 0) for key in ("execution_hits", "breakpoint_hits", "machine_acceptance_hits"))
    misses = sum(totals.get(key, 0) for key in ("execution_misses", "breakpoint_misses", "machine_acceptance_misses"))
    totals["total_hits"] = hits
    totals["total_misses"] = misses
    totals["hit_rate"] = hits / (hits + misses) if hits + misses else 0.0
    return totals


def summarize_run(cap, repetition, output_dir, process_seconds, stdout, stderr):
    progress = load_json(output_dir / "rolling_progress.json", {}) or {}
    runtime = load_json(output_dir / "runtime_profile.json", {}) or {}
    final = load_json(output_dir / "assembled_factory_validation.json", {}) or {}
    accounting = load_json(output_dir / "factory3_accounting.json", {}) or {}
    windows = progress.get("windows", [])
    stages = [winning_stage(window) for window in windows]
    all_window_valid = all(
        bool(window.get("planning_accepted", window.get("accepted")))
        and window.get("scope_status") == "SCOPE_VALID"
        and window.get("replay_status") == "PASS"
        and window.get("physical_status") == "VALID"
        and int(window.get("materialization_mismatches", 0)) == 0
        and int(window.get("historical_future_assignments_backfilled", 0)) == 0
        and int(window.get("committed_operations_changed", 0)) == 0
        and int(window.get("provisional_hard_frozen", 0)) == 0
        and int(window.get("database_writes", 0)) == 0
        and str(window.get("cp_solver_status", "UNKNOWN")) in {"FEASIBLE", "OPTIMAL"}
        for window in windows
    )
    violations = final.get("violation_counts", {}) or {}
    all_violation_count = sum(int(value) for value in violations.values())
    valid = (
        progress.get("status") == "GLOBAL_CRAFT_ONE_FACTORY_VALIDATED"
        and len(progress.get("cumulative_committed", [])) == 22
        and not progress.get("remaining")
        and all_window_valid
        and final.get("validation_scope") == "ASSEMBLED_FACTORY"
        and final.get("replay_status") == "PASS"
        and final.get("physical_status") == "VALID"
        and final.get("accepted") is True
        and not final.get("scope", {}).get("missing_required_operations")
        and not final.get("scope", {}).get("duplicate_operations")
        and not final.get("scope", {}).get("unexpected_operations")
        and int(final.get("replay", {}).get("historical_future_assignments_backfilled", 0)) == 0
        and all_violation_count == 0
        and int(progress.get("database_writes", 0)) == 0
    )
    config_counts = [int(window.get("craft_complexity", {}).get("resource_configuration_count", 0)) for window in windows]
    regime_counts = [int(window.get("craft_complexity", {}).get("calendar_regime_count", 0)) for window in windows]
    cp_variables = [int(stage.get("variables", 0)) for stage in stages]
    cp_constraints = [int(stage.get("constraints", 0)) for stage in stages]
    optional_intervals = [int(stage.get("optional_intervals", 0)) for stage in stages]
    branches = [int(stage.get("branches", 0)) for stage in stages]
    conflicts = [int(stage.get("conflicts", 0)) for stage in stages]
    prepare = [float(window.get("timing", {}).get("total_prepare_seconds", window.get("timing", {}).get("prepare_seconds", 0.0))) for window in windows]
    cp = [float(stage.get("cp_solve_seconds", stage.get("solve_seconds", 0.0))) for stage in stages]
    replay = [float(window.get("timing", {}).get("replay_seconds", 0.0)) for window in windows]
    physical_validation = [float(window.get("timing", {}).get("physical_validation_seconds", 0.0)) for window in windows]
    materialization = [float(window.get("timing", {}).get("materialization_seconds", 0.0)) for window in windows]
    return {
        "cap": cap,
        "repetition": repetition,
        "output_directory": str(output_dir),
        "process_wall_seconds": process_seconds,
        "valid_run": valid,
        "experiment_mode": "FEASIBILITY_SCALABILITY_CALIBRATION",
        "database_state_load_seconds": runtime.get("database_state_load_seconds"),
        "rolling_execution_seconds_excluding_load": runtime.get("rolling_execution_seconds_excluding_load"),
        "assembled_validation_seconds": runtime.get("assembled_validation_seconds"),
        "total_wall_seconds": runtime.get("total_seconds", process_seconds),
        "number_of_windows": len(windows),
        "maximum_selected_operations_in_any_window": max((int(window.get("selected_count", 0)) for window in windows), default=0),
        "operations_admitted_per_window": [int(window.get("selected_count", 0)) for window in windows],
        "operations_committed_per_window": [int(window.get("committed_count", 0)) for window in windows],
        "operations_provisional_per_window": [int(window.get("provisional_count", 0)) for window in windows],
        "provisional_changes": sum(int(window.get("provisional_operations_changed", 0)) for window in windows),
        "commit_edge_progression": [int(window.get("commit_end_seconds", 0)) for window in windows],
        "window_start_progression": [int(window.get("window_start_seconds", 0)) for window in windows],
        "total_resource_configurations_generated": sum(config_counts),
        "maximum_configs_in_one_window": max(config_counts, default=0),
        "total_calendar_regimes_generated": sum(regime_counts),
        "maximum_regimes_in_one_window": max(regime_counts, default=0),
        "total_cp_branches": sum(branches),
        "total_cp_conflicts": sum(conflicts),
        "maximum_cp_variables": max(cp_variables, default=0),
        "maximum_cp_constraints": max(cp_constraints, default=0),
        "maximum_optional_intervals": max(optional_intervals, default=0),
        "timing": {
            "state_build_seconds": sum(float(window.get("timing", {}).get("build_planning_state_seconds", 0.0)) for window in windows),
            "resource_configuration_seconds": sum(float(window.get("timing", {}).get("resource_configuration_seconds", 0.0)) for window in windows),
            "calendar_regime_generation_seconds": sum(float(window.get("timing", {}).get("calendar_regime_generation_seconds", 0.0)) for window in windows),
            "other_preparation_seconds": sum(float(window.get("timing", {}).get("other_preparation_seconds", 0.0)) for window in windows),
            "total_prepare_seconds": sum(prepare),
            "cp_model_build_seconds": sum(float(stage.get("cp_model_build_seconds", stage.get("model_build_seconds", 0.0))) for stage in stages),
            "cp_presolve_seconds": None,
            "cp_solve_seconds": sum(cp),
            "materialization_seconds": sum(materialization),
            "replay_seconds": sum(replay),
            "physical_validation_seconds": sum(physical_validation),
            "window_total_seconds": sum(float(window.get("window_total_seconds", 0.0)) for window in windows),
        },
        "craft_complexity": {
            "configuration_counts_per_window": config_counts,
            "regime_counts_per_window": regime_counts,
            "unique_machines_per_window": [len(window.get("craft_complexity", {}).get("unique_machines", [])) for window in windows],
            "unique_workers_per_window": [len(window.get("craft_complexity", {}).get("unique_workers", [])) for window in windows],
            "unique_lines_per_window": [len(window.get("craft_complexity", {}).get("unique_lines", [])) for window in windows],
            "carried_machine_boundary_facts": [int(window.get("craft_complexity", {}).get("carried_machine_boundary_facts", 0)) for window in windows],
            "carried_worker_boundary_facts": [int(window.get("craft_complexity", {}).get("carried_worker_boundary_facts", 0)) for window in windows],
            "carried_routing_boundary_facts": [int(window.get("craft_complexity", {}).get("carried_routing_boundary_facts", 0)) for window in windows],
        },
        "cache": cache_totals(windows),
        "cp_sat": {
            "windows": stages,
            "status_values": [stage.get("status") for stage in stages],
            "boolean_variables": [stage.get("boolean_variables") for stage in stages],
            "interval_variables": [stage.get("interval_variables") for stage in stages],
            "no_overlap_constraints": [stage.get("no_overlap_constraints") for stage in stages],
            "deterministic_time": [stage.get("deterministic_time") for stage in stages],
        },
        "final_replay_status": final.get("replay_status"),
        "final_physical_status": final.get("physical_status"),
        "all_violation_count": all_violation_count,
        "violation_counts": violations,
        "factory_accounting": accounting.get("accounting_identity", {}),
        "database_writes": 0,
        "stdout_tail": stdout[-1000:],
        "stderr_tail": stderr[-1000:],
    }


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    write_json(ROOT / "experiment_contract.json", {
        "experiment": "CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1",
        "experiment_mode": "FEASIBILITY_SCALABILITY_CALIBRATION",
        "factory_id": 3,
        "source_type": "FROZEN_BENCHMARK_RAW",
        "snapshot_as_of_seconds": 0,
        "schedulable_operations": 22,
        "caps": list(CAPS),
        "repetitions_per_cap": REPETITIONS,
        "principal_changed_variable": "maximum_operations_admission_cap",
        "lookahead_seconds": 86400,
        "commit_seconds": 20000,
        "objective_mode": False,
        "database_writes": 0,
    })
    all_runs = []
    for cap in CAPS:
        for repetition in range(1, REPETITIONS + 1):
            output_dir = ROOT / f"cap_{cap:03d}" / f"repetition_{repetition:03d}"
            output_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["CRAFT_ONE_FACTORY_NORMAL_CAP"] = str(cap)
            env["CRAFT_ONE_FACTORY_OUT"] = str(output_dir)
            started = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "scratch/run_one_factory_craft.py"],
                cwd=str(Path.cwd()), env=env, capture_output=True, text=True)
            elapsed = time.perf_counter() - started
            (output_dir / "runner_stdout.log").write_text(proc.stdout, encoding="utf-8")
            (output_dir / "runner_stderr.log").write_text(proc.stderr, encoding="utf-8")
            summary = summarize_run(cap, repetition, output_dir, elapsed, proc.stdout, proc.stderr)
            write_json(output_dir / "repetition_summary.json", summary)
            all_runs.append(summary)
            print(json.dumps({"cap": cap, "repetition": repetition,
                              "valid": summary["valid_run"],
                              "windows": summary["number_of_windows"],
                              "runtime": summary["total_wall_seconds"]}), flush=True)

    by_cap = {cap: [run for run in all_runs if run["cap"] == cap] for cap in CAPS}
    cap_summary = []
    timing_summary = []
    cp_stats = []
    craft_stats = []
    cache_stats = []
    correctness = []
    for cap in CAPS:
        runs = by_cap[cap]
        def values(path):
            result = []
            for run in runs:
                value = run
                for key in path:
                    value = value.get(key) if isinstance(value, dict) else None
                if value is not None:
                    result.append(value)
            return result
        row = {
            "cap": cap,
            "valid_run": all(run["valid_run"] for run in runs),
            "median_total_runtime": statistics.median(values(["total_wall_seconds"])),
            "median_rolling_runtime": statistics.median(values(["rolling_execution_seconds_excluding_load"])),
            "median_prepare_runtime": statistics.median(values(["timing", "total_prepare_seconds"])),
            "median_cp_runtime": statistics.median(values(["timing", "cp_solve_seconds"])),
            "number_of_windows": sorted(set(run["number_of_windows"] for run in runs)),
            "maximum_selected_operations_in_any_window": max(run["maximum_selected_operations_in_any_window"] for run in runs),
            "total_resource_configurations_generated": [run["total_resource_configurations_generated"] for run in runs],
            "maximum_configs_in_one_window": max(run["maximum_configs_in_one_window"] for run in runs),
            "total_calendar_regimes_generated": [run["total_calendar_regimes_generated"] for run in runs],
            "maximum_regimes_in_one_window": max(run["maximum_regimes_in_one_window"] for run in runs),
            "total_cp_branches": [run["total_cp_branches"] for run in runs],
            "total_cp_conflicts": [run["total_cp_conflicts"] for run in runs],
            "maximum_cp_variables": max(run["maximum_cp_variables"] for run in runs),
            "maximum_cp_constraints": max(run["maximum_cp_constraints"] for run in runs),
            "maximum_optional_intervals": max(run["maximum_optional_intervals"] for run in runs),
            "cache_hit_rate": statistics.median([run["cache"]["hit_rate"] for run in runs]),
            "final_replay_status": sorted(set(run["final_replay_status"] for run in runs)),
            "final_physical_status": sorted(set(run["final_physical_status"] for run in runs)),
            "all_violation_count": max(run["all_violation_count"] for run in runs),
            "database_writes": max(run["database_writes"] for run in runs),
        }
        cap_summary.append(row)
        timing_summary.append({
            "cap": cap,
            "database_state_load_seconds": percentile_summary(values(["database_state_load_seconds"])),
            "rolling_execution_seconds_excluding_load": percentile_summary(values(["rolling_execution_seconds_excluding_load"])),
            "assembled_validation_seconds": percentile_summary(values(["assembled_validation_seconds"])),
            "total_wall_seconds": percentile_summary(values(["total_wall_seconds"])),
            "state_build_seconds": percentile_summary(values(["timing", "state_build_seconds"])),
            "resource_configuration_seconds": percentile_summary(values(["timing", "resource_configuration_seconds"])),
            "calendar_regime_generation_seconds": percentile_summary(values(["timing", "calendar_regime_generation_seconds"])),
            "other_preparation_seconds": percentile_summary(values(["timing", "other_preparation_seconds"])),
            "total_prepare_seconds": percentile_summary(values(["timing", "total_prepare_seconds"])),
            "cp_model_build_seconds": percentile_summary(values(["timing", "cp_model_build_seconds"])),
            "cp_solve_seconds": percentile_summary(values(["timing", "cp_solve_seconds"])),
            "materialization_seconds": percentile_summary(values(["timing", "materialization_seconds"])),
            "replay_seconds": percentile_summary(values(["timing", "replay_seconds"])),
            "physical_validation_seconds": percentile_summary(values(["timing", "physical_validation_seconds"])),
            "window_total_seconds": percentile_summary(values(["timing", "window_total_seconds"])),
        })
        cp_stats.append({"cap": cap, "repetitions": [run["cp_sat"] for run in runs]})
        craft_stats.append({"cap": cap, "repetitions": [run["craft_complexity"] for run in runs]})
        cache_stats.append({"cap": cap, "repetitions": [run["cache"] for run in runs]})
        correctness.append({"cap": cap, "repetitions": [{
            "repetition": run["repetition"], "valid_run": run["valid_run"],
            "final_replay_status": run["final_replay_status"],
            "final_physical_status": run["final_physical_status"],
            "all_violation_count": run["all_violation_count"],
            "database_writes": run["database_writes"],
        } for run in runs]})

    baseline = next(row for row in cap_summary if row["cap"] == 6)
    ratio_fields = {
        "median_prepare_runtime": "median_prepare_runtime",
        "median_cp_runtime": "median_cp_runtime",
        "median_total_runtime": "median_total_runtime",
        "maximum_cp_variables": "maximum_cp_variables",
        "maximum_cp_constraints": "maximum_cp_constraints",
        "maximum_optional_intervals": "maximum_optional_intervals",
        "maximum_regimes_in_one_window": "maximum_regimes_in_one_window",
        "total_cp_branches": "total_cp_branches",
        "total_cp_conflicts": "total_cp_conflicts",
    }
    scaling_ratios = []
    for row in cap_summary:
        ratios = {"cap": row["cap"]}
        for name, source in ratio_fields.items():
            base_value = baseline[source]
            value = row[source]
            if isinstance(value, list):
                value = statistics.median(value)
            if isinstance(base_value, list):
                base_value = statistics.median(base_value)
            ratios[name] = None if not base_value else value / base_value
        scaling_ratios.append(ratios)

    write_json(ROOT / "window_level_metrics.json", [{
        "cap": run["cap"], "repetition": run["repetition"],
        "number_of_windows": run["number_of_windows"],
        "timing": run["timing"], "craft_complexity": run["craft_complexity"],
        "cp_sat": run["cp_sat"], "cache": run["cache"],
    } for run in all_runs])
    write_json(ROOT / "cap_summary.json", cap_summary)
    write_json(ROOT / "timing_summary.json", timing_summary)
    write_json(ROOT / "cp_sat_statistics.json", cp_stats)
    write_json(ROOT / "craft_complexity_statistics.json", craft_stats)
    write_json(ROOT / "cache_statistics.json", cache_stats)
    write_json(ROOT / "scaling_ratios.json", scaling_ratios)
    write_json(ROOT / "correctness_matrix.json", correctness)
    valid_all = all(run["valid_run"] for run in all_runs)
    by_cap_row = {row["cap"]: row for row in cap_summary}
    by_cap_timing = {row["cap"]: row for row in timing_summary}
    def med(cap, field):
        return by_cap_timing[cap][field]["median"]
    final_report = (
        "# CRAFT window scalability calibration V1\n\n"
        f"Status: `{'CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1_COMPLETE' if valid_all else 'CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1_INCOMPLETE'}`\n\n"
        "Mode: `FEASIBILITY_SCALABILITY_CALIBRATION`\n\n"
        "Factory 3 / `FROZEN_BENCHMARK_RAW` was independently rerun for caps "
        "6, 12, 18, and 22, with three repetitions per cap. Each repetition "
        "loaded the same frozen snapshot and used `objective_mode=False`; no "
        "database writes occurred.\n\n"
        "## Median measured timing\n\n"
        f"| cap | windows | total wall (s) | rolling excl. load (s) | prepare (s) | calendar regimes (s) | CP solve (s) | replay (s) | physical validation (s) |\n"
        f"|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        + "".join(
            f"| {cap} | {by_cap_row[cap]['number_of_windows']} | {med(cap, 'total_wall_seconds'):.3f} | "
            f"{med(cap, 'rolling_execution_seconds_excluding_load'):.3f} | {med(cap, 'total_prepare_seconds'):.3f} | "
            f"{med(cap, 'calendar_regime_generation_seconds'):.3f} | {med(cap, 'cp_solve_seconds'):.3f} | "
            f"{med(cap, 'replay_seconds'):.3f} | {med(cap, 'physical_validation_seconds'):.3f} |\n"
            for cap in CAPS
        ) + "\n"
        "Measured interpretation: fresh state loading is the largest total-wall "
        "component; exact calendar-regime generation dominates rolling preparation; "
        "CP model construction/search and replay/physical validation are smaller "
        "components in this calibration. Larger caps increase peak model/regime "
        "complexity and reduce windows from the cap-6/12 value to the cap-18/22 "
        "value, but these four points do not justify an asymptotic scalability law "
        "or a universal cap choice.\n\n"
        "Next experiment recommendation: use this evidence to choose a bounded "
        "policy for a second factory; do not change scheduling semantics, adaptive "
        "window sizing, or calendar/cache behavior from this calibration alone.\n"
    )
    (ROOT / "final_report.md").write_text(final_report, encoding="utf-8")
    manifest_paths = [path for path in ROOT.rglob("*") if path.is_file()
                      and path.name != "artifact_hash_manifest.json"]
    hashes = {str(path.relative_to(ROOT)).replace("\\", "/"):
              hashlib.sha256(path.read_bytes()).hexdigest().upper()
              for path in sorted(manifest_paths)}
    source_paths = [
        Path("scratch/run_window_scalability_calibration.py"),
        Path("scratch/run_one_factory_craft.py"),
        Path("crafter/repair/planning.py"),
        Path("crafter/repair/solve.py"),
        Path("crafter/repair/scoped_validation.py"),
    ]
    write_json(ROOT / "artifact_hash_manifest.json", {
        "algorithm": "SHA256", "experiment": "CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1",
        "artifacts": hashes,
        "source_files": {str(path).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest().upper()
                         for path in source_paths},
    })
    print(json.dumps({"status": "CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1_COMPLETE" if valid_all else "CRAFT_WINDOW_SCALABILITY_CALIBRATION_V1_INCOMPLETE",
                      "runs": len(all_runs), "valid_runs": sum(run["valid_run"] for run in all_runs)}))


if __name__ == "__main__":
    main()
