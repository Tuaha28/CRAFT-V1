"""Bounded Stage-1.5 audit runner; synthetic data and isolated outputs only."""
from __future__ import annotations

from dataclasses import asdict
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import time

import ortools
import psutil

from .data_adapter import build_scaling_fixture, build_smoke_fixture
from .model import (
    COMPACT_PAIRWISE, CONSERVATIVE_BUFFER, EXPANDED_PAIRWISE,
    SUPPORTED_FORMULATIONS, build_model, estimate_candidate_structure,
)
from .phenotype import validate
from .solver import solve
from .worker_bridge import allocate_exact_workers


LEVELS = (15, 50, 100, 200, 400)
# Avoid constructing formulations whose quadratic changeover block is already
# clearly unsuitable for the intended benchmark. The cheap estimate is still
# retained in the output, so skipped rows are evidence rather than omissions.
MAX_CHANGEOVER_CONSTRAINTS_TO_BUILD = 350_000


def _memory_mb():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024.0 * 1024.0)


def _size_row(size):
    row = asdict(size)
    row["rss_delta_mb"] = size.rss_after_mb - size.rss_before_mb
    return row


def _preflight():
    rows = []
    for operation_count in LEVELS:
        problem = build_scaling_fixture(operation_count)
        for formulation in SUPPORTED_FORMULATIONS:
            estimate = estimate_candidate_structure(problem, formulation)
            row = {
                "operations": operation_count,
                "formulation": formulation,
                "estimate": estimate,
            }
            if estimate["estimated_changeover_constraints"] > MAX_CHANGEOVER_CONSTRAINTS_TO_BUILD:
                row.update({
                    "built": False,
                    "skip_reason": "quadratic changeover block exceeds safe audit threshold",
                    "rss_mb": _memory_mb(),
                })
            else:
                artifact = build_model(problem, formulation=formulation)
                row.update({"built": True, "model": _size_row(artifact.size)})
                del artifact
                gc.collect()
            rows.append(row)
    return rows


def _micro_benchmarks():
    fixture = build_smoke_fixture()
    rows = []
    for formulation in SUPPORTED_FORMULATIONS:
        started = time.perf_counter()
        solution = solve(
            fixture.problem,
            formulation=formulation,
            time_limit_seconds=7.0,
            max_deterministic_time=0.75,
        )
        worker = allocate_exact_workers(
            fixture.problem, solution, fixture.operator_index)
        final = validate(fixture.problem, worker, fixture.operator_index)
        rows.append({
            "formulation": formulation,
            "elapsed_seconds": time.perf_counter() - started,
            "master_status": solution.status.value,
            "cp_status": solution.statistics.cp_status,
            "objective": solution.statistics.objective_value,
            "best_bound": solution.statistics.best_bound,
            "gap": solution.statistics.relative_gap,
            "solver_wall_seconds": solution.statistics.wall_time_seconds,
            "variables": solution.statistics.variables,
            "constraints": solution.statistics.constraints,
            "optional_intervals": solution.statistics.optional_intervals,
            "conflicts": solution.statistics.conflicts,
            "branches": solution.statistics.branches,
            "worker_status": worker.status.value,
            "validation_status": final.status.value,
            "validator_valid": bool(
                final.validation_report and final.validation_report.get("valid")),
            "phenotype_hash": final.phenotype_hash,
            "comparison_metrics": dict(final.comparison_metrics),
        })
    return rows


def run_audit():
    return {
        "audit": "CRAFTER_STAGE_1_5_MODEL_SIZE_SCALABILITY",
        "data_classification": "SYNTHETIC_NON_FINAL_NO_DATABASE_IO",
        "python": platform.python_version(),
        "ortools": ortools.__version__,
        "objective_equation": (
            "min 100*priority_weighted_tardiness_seconds + "
            "10000*late_order_count + 1*makespan_seconds + "
            "1*load_imbalance_machine_seconds"
        ),
        "compatibility_default": EXPANDED_PAIRWISE,
        "recommended_benchmark_formulation": CONSERVATIVE_BUFFER,
        "recommendation": "CRAFTER_50_ORDER_READY",
        "recommendation_scope": (
            "isolated architecture benchmark only; conservative changeover "
            "must be disclosed and exact post-materialization checks retained"
        ),
        "preflight_safe_changeover_constraint_threshold": (
            MAX_CHANGEOVER_CONSTRAINTS_TO_BUILD),
        "preflight": _preflight(),
        "micro_benchmarks": _micro_benchmarks(),
    }


def write_audit(output_dir="outputs/crafter"):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "crafter_stage_1_5_scalability_audit.json"
    payload = json.dumps(run_audit(), indent=2, sort_keys=True) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return path, digest


if __name__ == "__main__":
    artifact, sha256 = write_audit()
    print(json.dumps({"artifact": str(artifact), "sha256": sha256}, sort_keys=True))
