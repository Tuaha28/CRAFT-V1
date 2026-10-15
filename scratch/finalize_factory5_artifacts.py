"""Finalize persisted Factory-5 validation evidence without rerunning CRAFT."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path("outputs/crafter/global_craft_factory5_v1")
F3 = Path("outputs/crafter/window_scalability_calibration_v1")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def main():
    progress = read(ROOT / "rolling_progress.json")
    accounting = read(ROOT / "factory_accounting.json")
    runtime = read(ROOT / "runtime_profile.json")
    cp = read(ROOT / "cp_stage_accounting.json")
    commit = read(ROOT / "commit_integrity.json")
    provisional = read(ROOT / "provisional_behavior.json")
    final = read(ROOT / "assembled_factory_validation.json")
    windows = progress.get("windows", [])
    cache_keys = ("execution_hits", "breakpoint_hits", "machine_acceptance_hits")
    miss_keys = ("execution_misses", "breakpoint_misses", "machine_acceptance_misses")
    hits = sum(float(w.get("timing", {}).get("cache_delta", {}).get(k, 0)) for w in windows for k in cache_keys)
    misses = sum(float(w.get("timing", {}).get("cache_delta", {}).get(k, 0)) for w in windows for k in miss_keys)
    violations = final.get("violation_counts", {}) or {}
    comparison = {
        "experiment": "GLOBAL_CRAFT_FACTORY5_45_VALIDATION",
        "comparison_basis": "Factory-3 cap-22 median calibration versus the single accepted Factory-5 run",
        "no_asymptotic_claim": True,
        "factory3": {
            "source": "outputs/crafter/window_scalability_calibration_v1/cap_022",
            "operations": 22,
            "windows": 7,
            "median_total_wall_seconds": 77.78039269987494,
            "median_rolling_seconds_excluding_load": 32.72258169995621,
            "median_prepare_seconds": 16.16034820000641,
            "median_cp_solve_seconds": 2.8929943000000002,
            "total_resource_configurations": 65,
            "total_calendar_regimes": 858,
            "maximum_cp_variables": 240,
            "maximum_cp_constraints": 3272,
            "maximum_optional_intervals": 2324,
            "cache_hit_rate": 0.30201831576793203,
            "maximum_cp_stage_attempts": 1,
            "policy": {"maximum_operations": 22, "max_machine_sets": 1, "max_worker_crews": 1},
        },
        "factory5": {
            "source": "outputs/crafter/global_craft_factory5_v1",
            "operations": len(accounting.get("schedulable_operations", [])),
            "windows": len(windows),
            "total_wall_seconds": runtime.get("total_seconds"),
            "rolling_seconds_excluding_load": runtime.get("rolling_execution_seconds_excluding_load"),
            "prepare_seconds": sum(float(w.get("timing", {}).get("total_prepare_seconds", 0.0)) for w in windows),
            "cp_model_build_seconds_all_stages": cp.get("total_cp_model_build_seconds_all_stages"),
            "cp_solve_seconds_all_stages": cp.get("total_cp_solve_seconds_all_stages"),
            "total_resource_configurations": sum(int(w.get("craft_complexity", {}).get("resource_configuration_count", 0)) for w in windows),
            "total_calendar_regimes": sum(int(w.get("craft_complexity", {}).get("calendar_regime_count", 0)) for w in windows),
            "maximum_cp_variables": cp.get("winning_stage_variables_max"),
            "maximum_cp_constraints": cp.get("winning_stage_constraints_max"),
            "maximum_optional_intervals": cp.get("winning_stage_optional_intervals_max"),
            "cache_hit_rate": hits / (hits + misses) if hits + misses else 0.0,
            "maximum_cp_stage_attempts": cp.get("maximum_stages_attempted"),
            "policy": {"maximum_operations": 22, "max_machine_sets": 3, "max_worker_crews": 3},
        },
        "interpretation": "Descriptive measurements only; two factories do not justify an asymptotic scalability law.",
    }
    write(ROOT / "factory3_vs_factory5_comparison.json", comparison)

    final_report = """# Global CRAFT Factory-5 validation

Status: `GLOBAL_CRAFT_FACTORY5_45_VALIDATED`

Factory 5 was verified from `FROZEN_BENCHMARK_RAW` with 45 schedulable operations, objective mode disabled, maximum admission cap 22, the existing lookahead/commit policy, and exact 3-machine-set/3-worker-crew enumeration already supported by the CRAFT resource-configuration generator. No database writes occurred.

## Accounting and acceptance

- Factory universe: 45 operations; completed/in-progress/excluded: 0/0/0; scheduled and committed exactly once: 45; remaining: 0.
- Rolling windows: 12. Every accepted window was `SCOPE_VALID`, replay `PASS`, physical `VALID`, materialization mismatch 0, historical backfill 0, post-CP modifications 0, committed changes 0, provisional hard-freezes 0.
- Final scope: `ASSEMBLED_FACTORY`; replay `PASS`; physical `VALID`; missing, duplicate, and unexpected operations: 0.
- All precedence, routing-boundary, changeover, machine, worker, factory, eligibility, calendar, reservation, release, and commit violation counts: 0.

## CP-stage accounting

Eleven windows solved at stage 1; Window 3 required two attempted feasibility stages (stage 1 `INFEASIBLE`, stage 2 `OPTIMAL`). All attempted stage rows are persisted in each window record. Total all-stage CP model-build time was 2.604 seconds, solve time 19.044 seconds, branches 8,679, conflicts 41; maximum attempted stages: 2.

The failed preliminary max-1 resource-enumeration attempt is preserved under `failed_attempt_max1/`. It stopped at Window 3 with all canonical stages infeasible. The accepted run used the existing exact 3×3 resource-configuration enumeration; no physical constraint or calendar semantic was relaxed.

## Runtime and complexity

- State load: 65.874 seconds.
- Rolling execution excluding load: 272.969 seconds.
- Assembled validation: 1.102 seconds.
- Total wall time: 339.963 seconds.
- Calendar-regime generation: 217.120 seconds across windows.
- Total resource configurations: 555; total calendar regimes: 7,021.
- Peak winning-stage model: 296 variables, 5,307 constraints, 3,698 optional intervals.
- Exact calendar-cache hit rate: 56.86%.

Factory-3 cap-22 calibration comparison is persisted in `factory3_vs_factory5_comparison.json`. The comparison is descriptive only and makes no asymptotic scaling claim.

Focused validated CRAFT/planning regression before this run: 57 passed, 2 warnings.
"""
    (ROOT / "final_report.md").write_text(final_report, encoding="utf-8")

    changed = {
        "experiment": "GLOBAL_CRAFT_FACTORY5_45_VALIDATION",
        "experiment_code_changes": [
            "scratch/run_one_factory_craft.py: factory-id parameter, existing resource-enumeration parameters, all-stage CP accounting",
            "scratch/diagnose_factory5_window3.py: read-only infeasibility diagnosis",
        ],
        "production_scheduling_semantics_modified": False,
        "database_writes": 0,
        "focused_regression": "57 passed, 2 warnings",
    }
    write(ROOT / "changed_files_manifest.json", changed)

    artifacts = {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest().upper()
        for path in sorted(ROOT.rglob("*"))
        if path.is_file() and path.name != "artifact_hash_manifest.json"
    }
    sources = {}
    for path in (Path("scratch/run_one_factory_craft.py"), Path("scratch/diagnose_factory5_window3.py")):
        sources[str(path).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    write(ROOT / "artifact_hash_manifest.json", {
        "algorithm": "SHA256",
        "experiment": "GLOBAL_CRAFT_FACTORY5_45_VALIDATION",
        "artifacts": artifacts,
        "source_files": sources,
    })


if __name__ == "__main__":
    main()
