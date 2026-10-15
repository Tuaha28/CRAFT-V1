"""Isolated, bounded 50-order Crafter architecture benchmark."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import time

import ortools
import psutil

from ga.fitness import evaluate_schedule
from ga.heuristics import baseline_chromosomes

from .contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from .diagnostics import phenotype_dict, solution_dict
from .machine_model import legal_modes
from .model import CONSERVATIVE_BUFFER, build_model
from .objective import ObjectiveWeights
from .phenotype import comparison_metrics, validate
from .real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .solver import solve
from .worker_bridge import allocate_exact_workers


OUTPUT_DIR = Path("outputs/crafter/benchmark_50_v1")
SOLVER_CONFIG = {
    "formulation": CONSERVATIVE_BUFFER,
    "wall_time_seconds": 180.0,
    "max_deterministic_time": 60.0,
    "num_search_workers": 1,
    "random_seed": 42,
    "hint": None,
    "objective_weights": asdict(ObjectiveWeights()),
}
PREFLIGHT_LIMITS = {
    "variables": 250_000,
    "constraints": 500_000,
    "optional_intervals": 250_000,
    "candidate_machine_assignments": 125_000,
    "build_time_seconds": 60.0,
    "rss_delta_mb": 1024.0,
    "p90_eligible_machines": 1_000,
    "max_eligible_machines": 1_500,
}


def _safe(value):
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    return value


def _atomic_json(name, value):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / name
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return path, digest


def _percentile(values, fraction):
    values = sorted(values)
    if not values:
        return 0
    return values[min(len(values) - 1, int(math.ceil(fraction * len(values))) - 1)]


def _preflight_report(fixture, artifacts):
    problem = fixture.problem
    machine_density = []
    line_density = []
    max_counts = []
    dense_rows = []
    for operation in problem.operations:
        modes = artifacts.modes[operation.operation_key]
        machine_ids = sorted({machine for mode in modes for machine in mode.candidate_machine_ids})
        lines = sorted({mode.line_id for mode in modes})
        max_count = max(mode.machine_count for mode in modes)
        machine_density.append(len(machine_ids))
        line_density.append(len(lines))
        max_counts.append(max_count)
        dense_rows.append({
            "operation_key": operation.operation_key,
            "factory_id": problem.order_by_id[operation.order_id].factory_id,
            "skill": operation.required_capability,
            "eligible_lines": len(lines),
            "eligible_physical_machines": len(machine_ids),
            "candidate_modes": len(modes),
            "maximum_machine_count": max_count,
        })
    p90_machines = _percentile(machine_density, 0.9)
    dense_rows.sort(
        key=lambda row: (row["eligible_physical_machines"], row["candidate_modes"]),
        reverse=True,
    )
    proto_objective = artifacts.model.Proto().objective
    coefficients = [int(value) for value in proto_objective.coeffs]
    size = artifacts.size
    values = {
        "variables": size.total_variables,
        "constraints": size.total_constraints,
        "optional_intervals": size.optional_intervals,
        "candidate_machine_assignments": size.candidate_machine_assignments,
        "build_time_seconds": size.build_time_seconds,
        "rss_delta_mb": size.rss_after_mb - size.rss_before_mb,
        "p90_eligible_machines": p90_machines,
        "max_eligible_machines": max(machine_density),
    }
    violations = {
        key: {"actual": values[key], "limit": limit}
        for key, limit in PREFLIGHT_LIMITS.items() if values[key] > limit
    }
    hard_semantic_failures = []
    if len(problem.orders) != 50:
        hard_semantic_failures.append("selection is not exactly 50 orders")
    if any(not artifacts.modes[op.operation_key] for op in problem.operations):
        hard_semantic_failures.append("operation without a legal mode")
    if any(coefficient < 0 for coefficient in coefficients):
        hard_semantic_failures.append("negative generated objective coefficient")
    if len({op.operation_key for op in problem.operations}) != len(problem.operations):
        hard_semantic_failures.append("duplicate operation key")

    return {
        "gate_status": (
            "CRAFTER_50_ORDER_PREFLIGHT_BLOCKED"
            if violations or hard_semantic_failures else "PREFLIGHT_PASS"
        ),
        "gate_limits": PREFLIGHT_LIMITS,
        "gate_violations": violations,
        "semantic_failures": hard_semantic_failures,
        "source_fingerprint": fixture.source_fingerprint,
        "source_metadata": fixture.source_metadata,
        "selection": {
            "rule": fixture.source_metadata["selection_rule"],
            "order_ids": fixture.selected_order_ids,
        },
        "resource_counts": {
            "orders": len(problem.orders),
            "operations": len(problem.operations),
            "factories": sorted({order.factory_id for order in problem.orders}),
            "lines": len(problem.lines),
            "physical_machines": len(problem.machines),
            "usable_physical_machines": sum(machine.is_usable for machine in problem.machines),
            "workers": fixture.source_metadata["assignable_workers"],
            "skill_pools": len(problem.labour_capacities),
            "fixed_machine_commitments": len(problem.resources.fixed_machine_commitments),
            "fixed_worker_commitments": len(problem.resources.fixed_worker_commitments),
        },
        "eligibility_density": {
            "average_eligible_physical_machines": sum(machine_density) / len(machine_density),
            "p50_eligible_physical_machines": _percentile(machine_density, 0.5),
            "p90_eligible_physical_machines": p90_machines,
            "max_eligible_physical_machines": max(machine_density),
            "average_line_choices": sum(line_density) / len(line_density),
            "maximum_machine_count_mode": max(max_counts),
            "operations_with_one_feasible_line": sum(value == 1 for value in line_density),
            "dense_threshold_p90": p90_machines,
            "dense_operation_count": sum(value >= p90_machines for value in machine_density),
            "densest_operations": dense_rows[:25],
        },
        "model": asdict(size),
        "objective_verification": {
            "equation": (
                "min 100*priority_weighted_tardiness_seconds + "
                "10000*late_order_count + makespan_seconds + "
                "load_imbalance_machine_seconds"
            ),
            "coefficient_count": len(coefficients),
            "coefficient_min": min(coefficients),
            "coefficient_max": max(coefficients),
            "negative_coefficient_count": sum(value < 0 for value in coefficients),
            "coefficient_frequency": dict(sorted(Counter(coefficients).items())),
        },
        "formulation_disclosure": (
            "CONSERVATIVE_BUFFER reserves one full changeover after every machine use, "
            "including same-order transitions. It is safer and more restrictive than "
            "the exact placeholder changeover used by the GA."
        ),
    }


def run_preflight():
    started = time.perf_counter()
    fixture = build_real_window_fixture(50, SNAPSHOT_NAME)
    artifacts = build_model(
        fixture.problem, formulation=CONSERVATIVE_BUFFER)
    report = _preflight_report(fixture, artifacts)
    report["preflight_end_to_end_seconds"] = time.perf_counter() - started
    _atomic_json("source_fingerprint.json", {
        "sha256": fixture.source_fingerprint,
        "metadata": fixture.source_metadata,
    })
    _atomic_json("problem_manifest.json", {
        "problem": asdict(fixture.problem),
        "selected_order_ids": fixture.selected_order_ids,
        "source_fingerprint": fixture.source_fingerprint,
    })
    _atomic_json("preflight_report.json", report)
    _atomic_json("model_size_report.json", report["model"])
    _atomic_json("solver_configuration.json", SOLVER_CONFIG)
    return report


def _master_metrics(problem, solution):
    final_by_order = {}
    tardiness = []
    machine_seconds = 0
    for row in solution.assignments:
        sequence = problem.operation_by_key[row.operation_key].sequence
        current = final_by_order.get(row.order_id)
        if current is None or sequence > current[0]:
            final_by_order[row.order_id] = (sequence, row.end_seconds)
        machine_seconds += row.machine_count * (row.end_seconds - row.start_seconds)
    for order_id, (_sequence, completion) in final_by_order.items():
        deadline = problem.order_by_id[order_id].deadline_seconds
        if deadline is not None:
            tardiness.append(max(0, completion - max(0, deadline)))
    makespan = max((row.end_seconds for row in solution.assignments), default=0)
    usable = sum(machine.is_usable for machine in problem.machines)
    return {
        "operation_coverage": len(solution.assignments) / max(1, len(problem.operations)),
        "total_tardiness_seconds": sum(tardiness),
        "mean_tardiness_seconds": sum(tardiness) / max(1, len(tardiness)),
        "max_tardiness_seconds": max(tardiness, default=0),
        "late_order_count": sum(value > 0 for value in tardiness),
        "on_time_rate": 1.0 - sum(value > 0 for value in tardiness) / max(1, len(tardiness)),
        "makespan_seconds": makespan,
        "machine_busy_seconds": machine_seconds,
        "machine_utilization_from_origin": (
            machine_seconds / (usable * makespan) if usable and makespan else 0.0),
    }


def _conservative_buffer_cost(problem, solution):
    if solution.status != CrafterStatus.MASTER_FEASIBLE or not solution.assignments:
        return {
            "measurable": False,
            "machine_uses": None,
            "reserved_buffer_machine_seconds": None,
            "reserved_buffer_machine_hours": None,
            "observed_machine_transitions": None,
            "same_order_transitions_unnecessarily_buffered": None,
            "same_order_nominal_buffer_seconds": None,
            "same_order_transition_details": [],
            "disclosure": "Unavailable because CP-SAT returned no incumbent.",
        }
    by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        by_machine[fixed.machine_id].append((
            fixed.start_seconds, fixed.end_seconds,
            fixed.production_order_id, "fixed"))
    for row in solution.assignments:
        for machine_id in row.machine_ids:
            by_machine[machine_id].append((
                row.start_seconds, row.end_seconds, row.order_id, row.operation_key))
    same_order = []
    transitions = 0
    for machine_id, rows in by_machine.items():
        rows.sort(key=lambda value: (value[0], value[1], value[3]))
        for left, right in zip(rows, rows[1:]):
            transitions += 1
            if left[2] == right[2]:
                same_order.append({
                    "machine_id": machine_id,
                    "order_id": left[2],
                    "predecessor": left[3],
                    "successor": right[3],
                    "actual_gap_seconds": right[0] - left[1],
                    "conservative_buffer_seconds": problem.changeover_seconds,
                })
    machine_uses = sum(row.machine_count for row in solution.assignments)
    return {
        "measurable": True,
        "machine_uses": machine_uses,
        "reserved_buffer_machine_seconds": machine_uses * problem.changeover_seconds,
        "reserved_buffer_machine_hours": (
            machine_uses * problem.changeover_seconds / 3600.0),
        "observed_machine_transitions": transitions,
        "same_order_transitions_unnecessarily_buffered": len(same_order),
        "same_order_nominal_buffer_seconds": len(same_order) * problem.changeover_seconds,
        "same_order_transition_details": same_order,
        "disclosure": (
            "Buffers after final machine uses and buffers hidden inside naturally larger idle "
            "gaps need not extend makespan; reserved time is not equal to causal delay."
        ),
    }


def _baseline_phenotype(problem, details):
    operations = []
    for row in details.get("phenotype_operations", ()):
        assignment = OperationAssignment(
            operation_key=str(row["operation_key"]),
            order_id=str(row["production_order_id"]),
            operation_id=str(row["operation_id"]),
            line_id=int(row["line_id"]),
            machine_ids=tuple(int(value) for value in row["machine_ids"]),
            machine_count=len(row["machine_ids"]),
            # GA durations retain sub-second floating precision. Coercing
            # them to integer seconds makes an unchanged worker segment look
            # fractionally short to the independent validator. Preserve the
            # realized baseline phenotype exactly for comparison; Crafter's
            # own CP phenotype remains integer-second by contract.
            start_seconds=float(row["start_minutes"] * 60.0),
            productive_start_seconds=float(row["productive_start_minutes"] * 60.0),
            end_seconds=float(row["end_minutes"] * 60.0),
            setup_seconds=float(row["setup_minutes"] * 60.0),
            productive_seconds=float(row["productive_minutes"] * 60.0),
        )
        segment_map = row.get("worker_segments_by_employee", {})
        operations.append(PhenotypeOperation(
            assignment=assignment,
            employee_ids=tuple(str(value) for value in row.get("employee_ids", ())),
            worker_segments=tuple(
                (str(employee), tuple(tuple(pair) for pair in segments))
                for employee, segments in sorted(segment_map.items())
            ),
            sourcing_tier=str(row.get("operator_sourcing_tier", "unknown")),
        ))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE, tuple(operations))


def _run_baselines(fixture):
    scheduler = fixture.scheduler
    chromosomes = baseline_chromosomes(
        scheduler.factory, scheduler.op_details,
        scheduler.order_deadlines_minutes, scheduler.seq_lookup)
    results = {}
    for strategy in ("EDF", "CriticalRatio"):
        started = time.perf_counter()
        fitness, details = evaluate_schedule(
            chromosomes[strategy], return_details=True, **scheduler._eval_ctx)
        row = {
            "strategy": strategy,
            "runtime_seconds": time.perf_counter() - started,
            "legacy_fitness": float(fitness[0]),
            "decoder_details": details,
        }
        if details.get("execution_feasible") and details.get("phenotype_operations"):
            phenotype = _baseline_phenotype(fixture.problem, details)
            final = validate(fixture.problem, phenotype, fixture.operator_index)
            row.update({
                "status": final.status.value,
                "phenotype_hash": final.phenotype_hash,
                "comparison_metrics": dict(final.comparison_metrics),
                "validator_report": final.validation_report,
            })
        else:
            row.update({
                "status": "WORKER_OR_DECODER_INFEASIBLE",
                "phenotype_hash": None,
                "comparison_metrics": {},
                "validator_report": None,
            })
        results[strategy] = row
        _atomic_json(f"baseline_{strategy.lower()}_result.json", row)
    return results


def _write_hash_manifest():
    entries = {}
    for path in sorted(OUTPUT_DIR.iterdir()):
        if not path.is_file() or path.name in {
            "artifact_hash_manifest.json", "artifact_hash_manifest.json.sha256"}:
            continue
        entries[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _atomic_json("artifact_hash_manifest.json", entries)


def rerun_baselines_only():
    comparison_path = OUTPUT_DIR / "comparison_summary.json"
    if not comparison_path.exists():
        raise RuntimeError("comparison summary missing")
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    fixture = build_real_window_fixture(50, SNAPSHOT_NAME)
    if fixture.source_fingerprint != comparison["source_fingerprint"]:
        raise RuntimeError("source fingerprint changed before baseline replay")
    baselines = _run_baselines(fixture)
    comparison["baselines"] = {
        name: {
            "status": row["status"],
            "runtime_seconds": row["runtime_seconds"],
            "legacy_fitness": row["legacy_fitness"],
            "phenotype_hash": row["phenotype_hash"],
            "comparison_metrics": row["comparison_metrics"],
            "validator_valid": bool(
                row["validator_report"] and row["validator_report"].get("valid")),
        }
        for name, row in baselines.items()
    }
    comparison["baseline_replay_note"] = (
        "Replayed from the same frozen fingerprint with exact sub-second GA "
        "phenotype timings preserved for independent validation."
    )
    _atomic_json("comparison_summary.json", comparison)
    manifest, digest = _write_hash_manifest()
    return comparison, str(manifest), digest


def finalize_unknown_artifacts():
    """Replace mathematically misleading zero quality values after UNKNOWN."""
    master_path = OUTPUT_DIR / "crafter_master_solution.json"
    comparison_path = OUTPUT_DIR / "comparison_summary.json"
    master = json.loads(master_path.read_text(encoding="utf-8"))
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    if master["solution"]["status"] != CrafterStatus.MASTER_UNKNOWN.value:
        raise RuntimeError("artifact normalization is only valid for MASTER_UNKNOWN")
    unavailable = {
        "operation_coverage": 0.0,
        "total_tardiness_seconds": None,
        "mean_tardiness_seconds": None,
        "max_tardiness_seconds": None,
        "late_order_count": None,
        "on_time_rate": None,
        "makespan_seconds": None,
        "machine_busy_seconds": None,
        "machine_utilization_from_origin": None,
    }
    buffer_cost = {
        "measurable": False,
        "machine_uses": None,
        "reserved_buffer_machine_seconds": None,
        "reserved_buffer_machine_hours": None,
        "observed_machine_transitions": None,
        "same_order_transitions_unnecessarily_buffered": None,
        "same_order_nominal_buffer_seconds": None,
        "same_order_transition_details": [],
        "disclosure": "Unavailable because CP-SAT returned no incumbent.",
    }
    master["master_metrics"] = unavailable
    master["conservative_buffer_cost"] = buffer_cost
    comparison["crafter"]["quality_metrics"] = unavailable
    comparison["crafter"]["machine_utilization_from_origin"] = None
    comparison["crafter"]["worker_utilization_from_origin"] = None
    comparison["crafter"]["conservative_buffer_cost"] = buffer_cost
    comparison["unknown_result_note"] = (
        "Tardiness, on-time rate, makespan, utilization and buffer cost are "
        "unavailable—not zero—because no incumbent schedule exists."
    )
    _atomic_json("crafter_master_solution.json", master)
    _atomic_json("comparison_summary.json", comparison)
    manifest, digest = _write_hash_manifest()
    return comparison, str(manifest), digest


def run_benchmark():
    preflight_path = OUTPUT_DIR / "preflight_report.json"
    if not preflight_path.exists():
        raise RuntimeError("preflight report missing; run --preflight-only first")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if preflight["gate_status"] != "PREFLIGHT_PASS":
        raise RuntimeError("preflight did not pass; solver invocation is forbidden")

    end_to_end_start = time.perf_counter()
    fixture = build_real_window_fixture(50, SNAPSHOT_NAME)
    if fixture.source_fingerprint != preflight["source_fingerprint"]:
        raise RuntimeError("frozen source fingerprint changed between preflight and solve")

    solution = solve(
        fixture.problem,
        formulation=CONSERVATIVE_BUFFER,
        time_limit_seconds=SOLVER_CONFIG["wall_time_seconds"],
        max_deterministic_time=SOLVER_CONFIG["max_deterministic_time"],
        random_seed=SOLVER_CONFIG["random_seed"],
    )
    master_metrics = _master_metrics(fixture.problem, solution)
    buffer_cost = _conservative_buffer_cost(fixture.problem, solution)
    master_hash = hashlib.sha256(json.dumps(
        _safe([asdict(row) for row in solution.assignments]),
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    _atomic_json("crafter_master_solution.json", {
        "solution": solution_dict(solution),
        "master_metrics": master_metrics,
        "master_assignment_hash": master_hash,
        "conservative_buffer_cost": buffer_cost,
    })

    worker = allocate_exact_workers(
        fixture.problem, solution, fixture.operator_index)
    final = (
        validate(fixture.problem, worker, fixture.operator_index)
        if worker.status == CrafterStatus.WORKER_FEASIBLE else worker
    )
    _atomic_json("frozen_phenotype.json", phenotype_dict(final))
    _atomic_json("validator_report.json", final.validation_report or {
        "valid": False,
        "not_run_reason": "exact worker allocation did not produce a complete phenotype",
        "worker_failure": asdict(final.worker_failure) if final.worker_failure else None,
    })

    baselines = _run_baselines(fixture)
    final_metrics = (
        dict(final.comparison_metrics)
        if final.status == CrafterStatus.VALIDATED_FEASIBLE else master_metrics
    )
    worker_productive_seconds = final_metrics.get("employee_productive_seconds")
    makespan = master_metrics["makespan_seconds"]
    workers = fixture.source_metadata["assignable_workers"]
    worker_utilization = (
        worker_productive_seconds / (workers * makespan)
        if worker_productive_seconds is not None and workers and makespan else None
    )
    comparison = {
        "experiment": "ISOLATED_50_ORDER_ARCHITECTURE_BENCHMARK",
        "source_fingerprint": fixture.source_fingerprint,
        "formulation": CONSERVATIVE_BUFFER,
        "crafter": {
            "master_status": solution.status.value,
            "worker_status": worker.status.value,
            "final_status": final.status.value,
            "cp_statistics": asdict(solution.statistics),
            "objective_components": dict(solution.objective_components),
            "quality_metrics": final_metrics,
            "machine_utilization_from_origin": master_metrics[
                "machine_utilization_from_origin"],
            "worker_utilization_from_origin": worker_utilization,
            "phenotype_hash": final.phenotype_hash,
            "master_assignment_hash": master_hash,
            "worker_failure": (
                asdict(final.worker_failure) if final.worker_failure else None),
            "validator_valid": bool(
                final.validation_report and final.validation_report.get("valid")),
            "conservative_buffer_cost": buffer_cost,
        },
        "baselines": {
            name: {
                "status": row["status"],
                "runtime_seconds": row["runtime_seconds"],
                "legacy_fitness": row["legacy_fitness"],
                "phenotype_hash": row["phenotype_hash"],
                "comparison_metrics": row["comparison_metrics"],
                "validator_valid": bool(
                    row["validator_report"] and row["validator_report"].get("valid")),
            }
            for name, row in baselines.items()
        },
        "bounded_ga": {
            "run": False,
            "reason": (
                "Not run: this architecture gate does not require GA search, and a safely "
                "interruptible exact-same-instance GA wall-clock wrapper is not present. "
                "EDF and Critical Ratio provide deterministic comparison arms."
            ),
        },
        "end_to_end_runtime_seconds": time.perf_counter() - end_to_end_start,
        "process_peak_rss_mb": solution.statistics.peak_rss_mb,
        "software": {
            "python": platform.python_version(),
            "ortools": ortools.__version__,
            "psutil": psutil.__version__,
        },
        "calendar_realism_debt": fixture.source_metadata["calendar_semantics"],
        "conservative_buffer_disclosure": (
            "Crafter reserves a changeover after every machine use, including consecutive "
            "operations of the same order. Deterministic GA baselines use the repository's "
            "exact placeholder gap only when order identity changes, so this comparison is "
            "conservative against Crafter."
        ),
    }
    if final.status == CrafterStatus.VALIDATED_FEASIBLE:
        recommendation = "CRAFTER_50_BENCHMARK_PASS"
    elif solution.status == CrafterStatus.MASTER_FEASIBLE:
        recommendation = "CRAFTER_50_BENCHMARK_MIXED"
    else:
        recommendation = "CRAFTER_50_BENCHMARK_FAIL"
    comparison["recommendation"] = recommendation
    _atomic_json("comparison_summary.json", comparison)
    _atomic_json("run_log.json", {
        "events": [
            "targeted tests passed before benchmark",
            "read-only frozen snapshot adapted",
            "model-only preflight passed",
            "primary one-worker no-hint Crafter solve completed",
            "exact worker bridge executed",
            "independent validator executed only if worker-complete",
            "EDF and Critical Ratio deterministic baselines evaluated",
            "no GA search executed",
        ],
        "recommendation": recommendation,
    })
    manifest_path, manifest_hash = _write_hash_manifest()
    return comparison, str(manifest_path), manifest_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--solve", action="store_true")
    parser.add_argument("--baselines-only", action="store_true")
    parser.add_argument("--finalize-artifacts", action="store_true")
    args = parser.parse_args()
    if sum((args.preflight_only, args.solve, args.baselines_only,
            args.finalize_artifacts)) != 1:
        parser.error("choose exactly one benchmark action")
    if args.preflight_only:
        report = run_preflight()
        print(json.dumps({
            "gate_status": report["gate_status"],
            "model": report["model"],
            "eligibility_density": report["eligibility_density"],
        }, sort_keys=True, default=str))
    elif args.solve:
        comparison, manifest, digest = run_benchmark()
        print(json.dumps({
            "recommendation": comparison["recommendation"],
            "crafter": comparison["crafter"],
            "manifest": manifest,
            "manifest_sha256": digest,
        }, sort_keys=True, default=str))
    elif args.baselines_only:
        comparison, manifest, digest = rerun_baselines_only()
        print(json.dumps({
            "baselines": comparison["baselines"],
            "manifest": manifest,
            "manifest_sha256": digest,
        }, sort_keys=True, default=str))
    else:
        comparison, manifest, digest = finalize_unknown_artifacts()
        print(json.dumps({
            "recommendation": comparison["recommendation"],
            "manifest": manifest,
            "manifest_sha256": digest,
        }, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
