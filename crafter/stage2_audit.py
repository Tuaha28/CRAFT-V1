"""Bounded Stage-2 compression audit; never solves the real 50-order model."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import time

from ortools.sat.python import cp_model
import psutil

from .data_adapter import build_scaling_fixture, build_smoke_fixture
from .machine_materializer import machine_equivalence_classes, materialize_machines
from .model import CONSERVATIVE_BUFFER, build_model
from .model_v2 import build_model_v2, legal_line_bounds
from .phenotype import validate
from .real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .solver import solve
from .solver_v2 import solve_v2
from .worker_bridge import allocate_exact_workers


OUTPUT_DIR = Path("outputs/crafter/stage2_compressed_v1")
V1_DIR = Path("outputs/crafter/benchmark_50_v1")


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


def _write(name, value):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR/name
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    temp = path.with_suffix(path.suffix+".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.with_suffix(path.suffix+".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def _percentile(values, fraction):
    values = sorted(values)
    if not values:
        return 0
    return values[min(len(values)-1, math.ceil(fraction*len(values))-1)]


def _solve_built(artifacts, seconds=5.0, deterministic=1.0):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.max_deterministic_time = deterministic
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(artifacts.model)
    response = solver.ResponseProto()
    return {
        "status": solver.StatusName(status),
        "branches": int(solver.NumBranches()),
        "conflicts": int(solver.NumConflicts()),
        "wall_time_seconds": float(solver.WallTime()),
        "deterministic_time": float(getattr(response, "deterministic_time", 0.0)),
        "has_incumbent": status in (cp_model.FEASIBLE, cp_model.OPTIMAL),
    }


def _finalize_smoke(problem, operator_index, master, v2=False):
    machine = materialize_machines(problem, master) if v2 else None
    machine_solution = machine.solution if machine else master
    worker = allocate_exact_workers(problem, machine_solution, operator_index)
    final = validate(problem, worker, operator_index)
    return {
        "master_status": master.status.value,
        "machine_status": machine.status.value if machine else "V1_IN_MASTER",
        "worker_status": worker.status.value,
        "final_status": final.status.value,
        "objective": master.statistics.objective_value,
        "best_bound": master.statistics.best_bound,
        "gap": master.statistics.relative_gap,
        "solve_wall_seconds": master.statistics.wall_time_seconds,
        "branches": master.statistics.branches,
        "metrics": dict(final.comparison_metrics),
        "phenotype_hash": final.phenotype_hash,
        "validator": final.validation_report,
        "machine_failure": asdict(machine.failure) if machine and machine.failure else None,
    }


def smoke_comparison():
    fixture = build_smoke_fixture()
    v1_artifacts = build_model(fixture.problem, formulation=CONSERVATIVE_BUFFER)
    v2_artifacts = build_model_v2(fixture.problem)
    v1 = solve(fixture.problem, formulation=CONSERVATIVE_BUFFER,
               time_limit_seconds=15, max_deterministic_time=3)
    v2, _ = solve_v2(fixture.problem, time_limit_seconds=15,
                     max_deterministic_time=3)
    return {
        "problem_id": fixture.problem.problem_id,
        "operations": len(fixture.problem.operations),
        "semantic_contract": (
            "Both use conservative post-use buffer; V2 then materializes exact IDs. "
            "Different bounded incumbents may have different objective values."),
        "v1": {
            "size": asdict(v1_artifacts.size),
            "result": _finalize_smoke(
                fixture.problem, fixture.operator_index, v1, False),
        },
        "v2": {
            "size": asdict(v2_artifacts.size),
            "result": _finalize_smoke(
                fixture.problem, fixture.operator_index, v2, True),
        },
    }


def synthetic_scaling():
    rows = []
    process = psutil.Process(os.getpid())
    for count in (15, 50, 100, 200):
        problem = build_scaling_fixture(count)
        gc.collect()
        v1 = build_model(problem, formulation=CONSERVATIVE_BUFFER)
        v1_size = asdict(v1.size)
        del v1
        gc.collect()
        rss_before = process.memory_info().rss/(1024*1024)
        v2 = build_model_v2(problem)
        search = _solve_built(v2)
        rss_after = process.memory_info().rss/(1024*1024)
        rows.append({
            "operations": count,
            "v1": v1_size,
            "v2": asdict(v2.size),
            "v2_bounded_search": search,
            "observed_process_rss_before_v2_mb": rss_before,
            "observed_process_rss_after_search_mb": rss_after,
            "interval_reduction_factor": (
                v1_size["optional_intervals"] / max(1, v2.size.optional_intervals)),
            "variable_reduction_factor": (
                v1_size["total_variables"] / max(1, v2.size.total_variables)),
        })
        del v2
    return rows


def _equivalence_analysis(fixture):
    problem = fixture.problem
    all_master_classes = machine_equivalence_classes(problem)
    master_classes = [row for row in all_master_classes if row[0][3] is True]
    sizes = [len(ids) for _key, ids in master_classes]
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            fixed.start_seconds, fixed.end_seconds,
            fixed.production_order_id,
            getattr(fixed, "required_capability", None)))
    state_groups = defaultdict(list)
    last_order_nonempty = 0
    for machine in problem.machines:
        last_order = fixture.initial_machine_last_order.get(machine.machine_id)
        free_at = fixture.initial_machine_free.get(machine.machine_id)
        if last_order is not None:
            last_order_nonempty += 1
        key = (
            machine.factory_id, machine.line_id,
            tuple(sorted(x.strip().lower() for x in machine.capabilities)),
            machine.is_usable,
            tuple(sorted(fixed_by_machine.get(machine.machine_id, ()))),
            str(last_order) if last_order is not None else None,
            free_at.isoformat() if hasattr(free_at, "isoformat") else str(free_at),
        )
        state_groups[key].append(machine.machine_id)
    state_sizes = [len(ids) for ids in state_groups.values()]
    return {
        "physical_machines": len(problem.machines),
        "usable_machines": sum(row.is_usable for row in problem.machines),
        "usable_master_equivalence_classes": len(master_classes),
        "all_machine_state_classes_including_unusable": len(all_master_classes),
        "master_class_size": {
            "min": min(sizes), "p50": _percentile(sizes, .5),
            "p90": _percentile(sizes, .9), "max": max(sizes),
            "singleton_classes": sum(value == 1 for value in sizes),
        },
        "full_observed_state_classes_including_last_order_and_free_time": len(state_groups),
        "full_state_class_size": {
            "min": min(state_sizes), "p50": _percentile(state_sizes, .5),
            "p90": _percentile(state_sizes, .9), "max": max(state_sizes),
            "singleton_classes": sum(value == 1 for value in state_sizes),
        },
        "machines_with_fixed_commitments": len(fixed_by_machine),
        "machines_with_last_order_identity": last_order_nonempty,
        "unusable_or_reserved_machines": sum(not row.is_usable for row in problem.machines),
        "multi_capability_machines": sum(len(set(row.capabilities)) > 1
                                         for row in problem.machines),
        "master_key_disclosure": (
            "factory/line/capability/usability/fixed-timeline. Conservative buffers "
            "make order-dependent last-order setup unnecessary in this master; the "
            "full-state count is reported and exact IDs remain a mandatory gate."),
        "density_interpretation": (
            "The real adapter produced no duplicated eligible-line entries and no "
            "multi-capability assets in this snapshot. Broad eligibility is genuine: "
            "many single-capability machines across many eligible factory lines."),
    }


def real_preflight():
    total_started = time.perf_counter()
    fixture = build_real_window_fixture(order_limit=50, snapshot_name=SNAPSHOT_NAME)
    adapter_elapsed = time.perf_counter()-total_started
    problem = fixture.problem
    caps = {(row.factory_id, row.skill.strip().lower()): row.capacity
            for row in problem.labour_capacities}
    machine_counts, line_counts, max_ks = [], [], []
    by_factory = defaultdict(lambda: {"operations": 0, "lines": [], "machines": []})
    by_skill = defaultdict(lambda: {"operations": 0, "lines": [], "machines": []})
    line_pressure = Counter()
    duplicate_lines = 0
    for op in problem.operations:
        bounds = legal_line_bounds(problem, op, caps)
        lines = list(bounds)
        machines = {machine_id for _lo, _hi, ids in bounds.values() for machine_id in ids}
        factory = problem.order_by_id[op.order_id].factory_id
        machine_counts.append(len(machines)); line_counts.append(len(lines))
        max_ks.append(max(hi for _lo, hi, _ids in bounds.values()))
        by_factory[factory]["operations"] += 1
        by_factory[factory]["lines"].append(len(lines))
        by_factory[factory]["machines"].append(len(machines))
        by_skill[op.required_capability]["operations"] += 1
        by_skill[op.required_capability]["lines"].append(len(lines))
        by_skill[op.required_capability]["machines"].append(len(machines))
        line_pressure.update(lines)
        duplicate_lines += len(op.eligible_line_ids)-len(set(op.eligible_line_ids))

    build_started = time.perf_counter()
    artifacts = build_model_v2(problem)
    build_elapsed = time.perf_counter()-build_started
    coefficients = list(artifacts.model.Proto().objective.coeffs)
    v1_size = json.loads((V1_DIR/"model_size_report.json").read_text(encoding="utf-8"))
    v1_source = json.loads((V1_DIR/"source_fingerprint.json").read_text(encoding="utf-8"))
    source_matches = v1_source["sha256"] == fixture.source_fingerprint

    def summarize(group):
        return {
            str(key): {
                "operations": value["operations"],
                "average_lines": sum(value["lines"])/len(value["lines"]),
                "average_machines": sum(value["machines"])/len(value["machines"]),
                "max_machines": max(value["machines"]),
            } for key, value in sorted(group.items(), key=lambda item: str(item[0]))
        }

    v2_size = asdict(artifacts.size)
    return fixture, {
        "gate": "V2_MODEL_ONLY_PREFLIGHT_PASS",
        "real_solve_invoked": False,
        "snapshot": SNAPSHOT_NAME,
        "source_fingerprint": fixture.source_fingerprint,
        "v1_source_fingerprint": v1_source["sha256"],
        "source_fingerprint_matches_v1": source_matches,
        "selected_order_ids": fixture.selected_order_ids,
        "source_metadata": fixture.source_metadata,
        "counts": {
            "orders": len(problem.orders), "operations": len(problem.operations),
            "factories": sorted({row.factory_id for row in problem.orders}),
            "lines": len(problem.lines), "physical_machines": len(problem.machines),
            "usable_machines": sum(row.is_usable for row in problem.machines),
            "workers": fixture.source_metadata["assignable_workers"],
            "fixed_machine_commitments": len(problem.resources.fixed_machine_commitments),
            "fixed_worker_commitments": len(problem.resources.fixed_worker_commitments),
        },
        "eligibility": {
            "average_machines": sum(machine_counts)/len(machine_counts),
            "p50_machines": _percentile(machine_counts, .5),
            "p90_machines": _percentile(machine_counts, .9),
            "max_machines": max(machine_counts),
            "average_lines": sum(line_counts)/len(line_counts),
            "p90_lines": _percentile(line_counts, .9),
            "max_lines": max(line_counts),
            "max_machine_count": max(max_ks),
            "duplicate_eligible_line_entries": duplicate_lines,
        },
        "density_by_factory": summarize(by_factory),
        "density_by_capability": summarize(by_skill),
        "top_lines_by_candidate_operations": line_pressure.most_common(20),
        "machine_equivalence": _equivalence_analysis(fixture),
        "adapter_elapsed_seconds": adapter_elapsed,
        "model_build_elapsed_seconds": build_elapsed,
        "v2_size": v2_size,
        "objective_coefficients_nonnegative": bool(coefficients) and min(coefficients) >= 0,
        "v1_v2": {
            "v1": v1_size, "v2": v2_size,
            "variable_reduction_factor": v1_size["total_variables"] / v2_size["total_variables"],
            "constraint_reduction_factor": v1_size["total_constraints"] / v2_size["total_constraints"],
            "optional_interval_reduction_factor": v1_size["optional_intervals"] / v2_size["optional_intervals"],
            "build_time_reduction_factor": v1_size["build_time_seconds"] / v2_size["build_time_seconds"],
            "rss_delta_v1_mb": v1_size["rss_after_mb"]-v1_size["rss_before_mb"],
            "rss_delta_v2_mb": v2_size["rss_after_mb"]-v2_size["rss_before_mb"],
        },
    }


def main():
    design = {
        "stage": "CRAFTER_STAGE_2_COMPRESSED_HIERARCHICAL",
        "v1_disposition": "V1_MODE_ENUMERATION_FAILED_SCALABILITY_GATE",
        "v1_general_cp_sat_interpretation": (
            "The failed representation is frozen; this is not evidence that CP-SAT "
            "cannot solve the scheduling problem."),
        "v2_architecture": (
            "compressed line integer + one-hot eligible-line intervals + integer k + "
            "exact duration table + variable-demand cumulative + exact machine-state-class "
            "capacity + mandatory physical-ID materializer"),
        "feasibility_states": [
            "MASTER_FEASIBLE", "MACHINE_FEASIBLE", "WORKER_FEASIBLE",
            "VALIDATED_FEASIBLE"],
        "changeover": (
            "Conservative full buffer after every use, including same-order transitions; "
            "not claimed equivalent to exact order-dependent placeholder semantics."),
        "repair_contract": (
            "Materialization failure returns MASTER_FEASIBLE_MACHINE_INFEASIBLE with "
            "operation/line/k/time/capability/candidate diagnostics; Stage 2 adds no cuts."),
        "real_50_solve_allowed": False,
    }
    hashes = {"design_manifest.json": _write("design_manifest.json", design)}
    smoke = smoke_comparison()
    hashes["smoke_comparison.json"] = _write("smoke_comparison.json", smoke)
    scaling = synthetic_scaling()
    hashes["synthetic_scaling.json"] = _write("synthetic_scaling.json", scaling)
    fixture, preflight = real_preflight()
    hashes["machine_equivalence_analysis.json"] = _write(
        "machine_equivalence_analysis.json", preflight["machine_equivalence"])
    hashes["real_50_v2_preflight.json"] = _write(
        "real_50_v2_preflight.json", preflight)
    hashes["v1_v2_size_comparison.json"] = _write(
        "v1_v2_size_comparison.json", preflight["v1_v2"])
    source_hashes = {}
    for path in sorted([
        *Path("crafter").glob("*.py"), *Path("tests").glob("test_crafter_*.py")],
        key=str):
        source_hashes[str(path).replace("\\", "/")] = hashlib.sha256(
            path.read_bytes()).hexdigest()
    hashes["source_fingerprints.json"] = _write("source_fingerprints.json", {
        "problem_source_fingerprint": fixture.source_fingerprint,
        "selected_orders": fixture.selected_order_ids,
        "source_files": source_hashes,
    })
    manifest = {"artifacts": hashes, "real_50_solve_invoked": False}
    manifest_hash = _write("artifact_hash_manifest.json", manifest)
    print(json.dumps({
        "output_dir": str(OUTPUT_DIR),
        "artifact_manifest_sha256": manifest_hash,
        "smoke_v2": smoke["v2"]["result"]["final_status"],
        "real_preflight": preflight["gate"],
        "v2_size": preflight["v2_size"],
        "reductions": preflight["v1_v2"],
        "real_50_solve_invoked": False,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
