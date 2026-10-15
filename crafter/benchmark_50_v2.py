"""Isolated real 50-order V2 benchmark.  No database or production writes."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import time

from ortools.sat.python import cp_model
import psutil

from .contracts import CrafterStatus
from .diagnostics import phenotype_dict, solution_dict
from .machine_materializer import materialize_machines
from .model_v2 import build_model_v2
from .objective import ObjectiveWeights
from .phenotype import validate
from .real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .solver_v2 import solve_built_v2
from .worker_bridge import allocate_exact_workers


EXPECTED_FINGERPRINT = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
OUTPUT_DIR = Path("outputs/crafter/benchmark_50_v2")
V1_DIR = Path("outputs/crafter/benchmark_50_v1")

ARM_CONFIG = {
    "primary": {
        "wall_time_seconds": 180.0, "max_deterministic_time": 60.0,
        "num_search_workers": 1, "random_seed": 42, "hint": None,
    },
    "diagnostic_edf_hint": {
        "wall_time_seconds": 180.0, "max_deterministic_time": 60.0,
        "num_search_workers": 1, "random_seed": 42, "hint": "EDF",
    },
    "diagnostic_8worker": {
        "wall_time_seconds": 180.0, "max_deterministic_time": 60.0,
        "num_search_workers": 8, "random_seed": 42, "hint": None,
    },
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


def _write(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/name
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    temp = path.with_suffix(path.suffix+".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, path)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.with_suffix(path.suffix+".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


class FirstIncumbent(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.started = time.perf_counter()
        self.time_seconds = None
        self.solver_wall_seconds = None
        self.objective = None
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        if self.time_seconds is None:
            self.time_seconds = time.perf_counter()-self.started
            self.solver_wall_seconds = float(self.WallTime())
            self.objective = float(self.ObjectiveValue())


def _apply_edf_hint(problem, artifacts):
    path = V1_DIR/"baseline_edf_result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload["decoder_details"]["phenotype_operations"]
    by_key = {row["operation_key"]: row for row in records}
    accepted, rejected = 0, []
    for op in problem.operations:
        row = by_key.get(op.operation_key)
        if row is None:
            rejected.append({"operation_key": op.operation_key, "reason": "missing"})
            continue
        line_id = int(row["line_id"])
        count = len(set(int(x) for x in row.get("machine_ids", ())))
        bound = artifacts.legal_line_bounds[op.operation_key].get(line_id)
        if bound is None or not bound[0] <= count <= bound[1]:
            rejected.append({
                "operation_key": op.operation_key, "reason": "illegal-line-or-count",
                "line_id": line_id, "machine_count": count})
            continue
        start = max(0, int(round(float(row["start_minutes"])*60.0)))
        productive = (op.serial_productive_seconds+count-1)//count
        duration = op.setup_seconds+productive
        model = artifacts.model
        model.AddHint(artifacts.starts[op.operation_key], start)
        model.AddHint(artifacts.productive_starts[op.operation_key], start+op.setup_seconds)
        model.AddHint(artifacts.ends[op.operation_key], start+duration)
        model.AddHint(artifacts.machine_counts[op.operation_key], count)
        model.AddHint(artifacts.productive_durations[op.operation_key], productive)
        model.AddHint(artifacts.durations[op.operation_key], duration)
        model.AddHint(artifacts.reserved_durations[op.operation_key],
                      duration+problem.changeover_seconds)
        model.AddHint(artifacts.release_ends[op.operation_key],
                      start+duration+problem.changeover_seconds)
        model.AddHint(artifacts.line_choices[op.operation_key], line_id)
        for candidate_line in artifacts.legal_line_bounds[op.operation_key]:
            model.AddHint(artifacts.line_literals[(op.operation_key, candidate_line)],
                          int(candidate_line == line_id))
        accepted += 1
    return {
        "source": "validated V1 EDF frozen phenotype",
        "source_path": str(path),
        "source_phenotype_hash": payload["phenotype_hash"],
        "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_operations": len(problem.operations),
        "mapped_legal_operations": accepted,
        "coverage": accepted/max(1, len(problem.operations)),
        "rejected": rejected,
        "disclosure": (
            "EDF timing is only a hint. V2 exact durations and conservative buffers "
            "remain authoritative; CP-SAT may ignore or repair hinted values."),
    }


def _quality(problem, phenotype, fixture):
    if phenotype.status != CrafterStatus.VALIDATED_FEASIBLE:
        return None
    assignments = [item.assignment for item in phenotype.operations]
    final_by_order = {}
    for row in assignments:
        sequence = problem.operation_by_key[row.operation_key].sequence
        if row.order_id not in final_by_order or sequence > final_by_order[row.order_id][0]:
            final_by_order[row.order_id] = (sequence, row.end_seconds)
    tardiness = []
    for order_id, (_sequence, completion) in final_by_order.items():
        due = problem.order_by_id[order_id].deadline_seconds
        if due is not None:
            tardiness.append(max(0, completion-max(0, due)))
    makespan = max(row.end_seconds for row in assignments)
    machine_busy = sum((row.end_seconds-row.start_seconds)*row.machine_count
                       for row in assignments)
    productive_machine = sum(row.productive_seconds*row.machine_count
                             for row in assignments)
    setup_machine = sum(row.setup_seconds*row.machine_count for row in assignments)
    buffer_machine = sum(problem.changeover_seconds*row.machine_count
                         for row in assignments)
    engaged = {machine_id for row in assignments for machine_id in row.machine_ids}
    worker_seconds = sum(
        (end-start).total_seconds()
        for item in phenotype.operations
        for _employee, segments in item.worker_segments
        for start, end in segments)
    by_machine = defaultdict(list)
    for row in assignments:
        for machine_id in row.machine_ids:
            by_machine[machine_id].append(row)
    same_order = 0
    for rows in by_machine.values():
        rows.sort(key=lambda row: (row.start_seconds, row.end_seconds, row.operation_key))
        same_order += sum(left.order_id == right.order_id
                          for left, right in zip(rows, rows[1:]))
    usable = sum(machine.is_usable for machine in problem.machines)
    concurrent_workers = int(fixture.source_metadata["concurrent_operator_capacity"])
    assignable_workers = int(fixture.source_metadata["assignable_workers"])
    loads = defaultdict(int)
    for row in assignments:
        loads[row.line_id] += (row.end_seconds-row.start_seconds)*row.machine_count
    all_loads = [loads[line.line_id] for line in problem.lines]
    return {
        "operation_coverage": len(assignments)/len(problem.operations),
        "operations": len(assignments),
        "total_tardiness_seconds": sum(tardiness),
        "mean_tardiness_seconds_all_orders": sum(tardiness)/len(tardiness),
        "mean_tardiness_seconds_late_orders": (
            sum(tardiness)/sum(value > 0 for value in tardiness)
            if any(tardiness) else 0.0),
        "max_tardiness_seconds": max(tardiness, default=0),
        "late_order_count": sum(value > 0 for value in tardiness),
        "on_time_rate": 1.0-sum(value > 0 for value in tardiness)/len(tardiness),
        "makespan_seconds": makespan,
        "line_load_imbalance_machine_seconds": max(all_loads)-min(all_loads),
        "engaged_physical_machines": len(engaged),
        "machine_busy_seconds": machine_busy,
        "productive_machine_seconds": productive_machine,
        "setup_machine_seconds": setup_machine,
        "scheduled_buffer_machine_seconds": buffer_machine,
        "fixed_commitment_buffer_machine_seconds": (
            len(problem.resources.fixed_machine_commitments)*problem.changeover_seconds),
        "machine_utilization_excluding_buffer": (
            machine_busy/(usable*makespan) if usable and makespan else 0.0),
        "machine_reserved_utilization_including_buffer": (
            (machine_busy+buffer_machine)/(usable*makespan)
            if usable and makespan else 0.0),
        "worker_productive_seconds": worker_seconds,
        "worker_productive_minutes": worker_seconds/60.0,
        "worker_utilization_vs_concurrent_capacity": (
            worker_seconds/(concurrent_workers*makespan)
            if concurrent_workers and makespan else 0.0),
        "worker_utilization_vs_assignable_roster": (
            worker_seconds/(assignable_workers*makespan)
            if assignable_workers and makespan else 0.0),
        "same_order_physical_machine_transitions_buffered": same_order,
        "same_order_unnecessary_buffer_machine_seconds": (
            same_order*problem.changeover_seconds),
        "buffer_disclosure": (
            "Every scheduled physical-machine use reserves the full buffer, including "
            "same-order transitions and terminal uses. The same-order figure measures "
            "only directly observed adjacent same-order transitions, not a counterfactual "
            "rescheduled makespan/tardiness improvement."),
    }


def _baseline_comparison(source_fingerprint):
    v1_source = json.loads((V1_DIR/"source_fingerprint.json").read_text(encoding="utf-8"))
    if v1_source["sha256"] != source_fingerprint:
        raise RuntimeError("V1 baseline source fingerprint mismatch")
    out = {}
    for label, name in (("EDF", "baseline_edf_result.json"),
                        ("CriticalRatio", "baseline_criticalratio_result.json")):
        path = V1_DIR/name
        payload = json.loads(path.read_text(encoding="utf-8"))
        lateness_hours = list(payload["decoder_details"].get(
            "order_lateness_hours", {}).values())
        out[label] = {
            "status": payload["status"],
            "phenotype_hash": payload["phenotype_hash"],
            "source_file": str(path),
            "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "comparison_metrics": payload["comparison_metrics"],
            "mean_tardiness_seconds_all_orders": (
                sum(lateness_hours)*3600/max(1, len(lateness_hours))),
            "mean_tardiness_seconds_late_orders": (
                sum(lateness_hours)*3600/max(1, sum(value > 0 for value in lateness_hours))),
            "max_tardiness_seconds": max(lateness_hours, default=0)*3600,
            "machines_engaged": payload["decoder_details"].get("machines_engaged"),
            "labour_utilization_rate": payload["decoder_details"].get(
                "labor_utilization_rate"),
        }
    return out


def run_arm(arm):
    config = ARM_CONFIG[arm]
    directory = OUTPUT_DIR if arm == "primary" else OUTPUT_DIR/arm
    run_started = time.perf_counter()
    fixture = build_real_window_fixture(order_limit=50, snapshot_name=SNAPSHOT_NAME)
    if fixture.source_fingerprint != EXPECTED_FINGERPRINT:
        mismatch = {
            "status": "CRAFTER_V2_SOURCE_MISMATCH",
            "expected": EXPECTED_FINGERPRINT, "actual": fixture.source_fingerprint,
            "solver_invoked": False,
        }
        _write(directory, "source_mismatch.json", mismatch)
        print(json.dumps(mismatch, indent=2)); return mismatch

    artifacts = build_model_v2(fixture.problem, ObjectiveWeights())
    hint_report = None
    if config["hint"] == "EDF":
        hint_report = _apply_edf_hint(fixture.problem, artifacts)
    callback = FirstIncumbent()
    master, _solver = solve_built_v2(
        fixture.problem, artifacts,
        time_limit_seconds=config["wall_time_seconds"],
        max_deterministic_time=config["max_deterministic_time"],
        random_seed=config["random_seed"],
        num_search_workers=config["num_search_workers"],
        solution_callback=callback)

    machine_started = time.perf_counter()
    machine = materialize_machines(
        fixture.problem, master, time_limit_seconds=60, random_seed=42)
    machine_seconds = time.perf_counter()-machine_started
    worker_started = time.perf_counter()
    worker = allocate_exact_workers(
        fixture.problem, machine.solution, fixture.operator_index)
    worker_seconds = time.perf_counter()-worker_started
    validator_started = time.perf_counter()
    final = validate(fixture.problem, worker, fixture.operator_index)
    validator_seconds = time.perf_counter()-validator_started
    quality = _quality(fixture.problem, final, fixture)
    baselines = _baseline_comparison(fixture.source_fingerprint)

    result = {
        "arm": arm,
        "experimental_only": True,
        "database_writes": False,
        "production_exports": False,
        "source_fingerprint": fixture.source_fingerprint,
        "selected_order_ids": fixture.selected_order_ids,
        "operations": len(fixture.problem.operations),
        "configuration": config,
        "model_size": asdict(artifacts.size),
        "first_incumbent": {
            "found": callback.time_seconds is not None,
            "elapsed_seconds": callback.time_seconds,
            "solver_wall_seconds": callback.solver_wall_seconds,
            "objective": callback.objective,
            "solution_callback_count": callback.count,
        },
        "master_status": master.status.value,
        "machine_status": machine.status.value,
        "worker_status": worker.status.value,
        "validator_status": final.status.value,
        "quality": quality,
        "machine_materialization_seconds": machine_seconds,
        "worker_allocation_seconds": worker_seconds,
        "validator_seconds": validator_seconds,
        "end_to_end_seconds": time.perf_counter()-run_started,
        "process_peak_rss_mb": master.statistics.peak_rss_mb,
        "hint_report": hint_report,
        "baseline_comparison": baselines,
        "failure_stage": (
            None if final.status == CrafterStatus.VALIDATED_FEASIBLE
            else "MASTER" if master.status != CrafterStatus.MASTER_FEASIBLE
            else "MACHINE" if machine.status != CrafterStatus.MACHINE_FEASIBLE
            else "WORKER" if worker.status != CrafterStatus.WORKER_FEASIBLE
            else "VALIDATOR"),
        "recommendation": (
            "CRAFTER_V2_50_BENCHMARK_PASS"
            if final.status == CrafterStatus.VALIDATED_FEASIBLE
            else "CRAFTER_V2_50_BENCHMARK_MIXED"
            if callback.time_seconds is not None or master.statistics.branches > 0
            else "CRAFTER_V2_50_BENCHMARK_FAIL"),
    }

    hashes = {}
    hashes["source_fingerprint.json"] = _write(directory, "source_fingerprint.json", {
        "expected": EXPECTED_FINGERPRINT, "actual": fixture.source_fingerprint,
        "verified": True, "metadata": fixture.source_metadata})
    hashes["problem_manifest.json"] = _write(directory, "problem_manifest.json", {
        "problem": asdict(fixture.problem),
        "selected_order_ids": fixture.selected_order_ids})
    hashes["model_size_report.json"] = _write(
        directory, "model_size_report.json", asdict(artifacts.size))
    hashes["solver_parameters.json"] = _write(
        directory, "solver_parameters.json", config)
    hashes[f"{arm}_result.json"] = _write(directory, f"{arm}_result.json", result)
    hashes["master_solution.json"] = _write(
        directory, "master_solution.json", solution_dict(master))
    hashes["machine_materialization.json"] = _write(directory, "machine_materialization.json", {
        "status": machine.status, "failure": asdict(machine.failure) if machine.failure else None,
        "diagnostics": machine.diagnostics,
        "solution": solution_dict(machine.solution)})
    hashes["worker_result.json"] = _write(
        directory, "worker_result.json", phenotype_dict(worker))
    hashes["validator_report.json"] = _write(
        directory, "validator_report.json", final.validation_report or {
            "valid": False, "not_run_reason": final.status.value})
    hashes["frozen_phenotype.json"] = _write(
        directory, "frozen_phenotype.json", phenotype_dict(final))
    hashes["comparison_summary.json"] = _write(directory, "comparison_summary.json", {
        "crafter": quality, "baselines": baselines,
        "fairness": "All metrics are read from frozen realized phenotypes; no GA re-decode."})
    hashes["run_log.json"] = _write(directory, "run_log.json", {
        "states": [master.status, machine.status, worker.status, final.status],
        "first_incumbent": result["first_incumbent"],
        "timings": {
            "model_build": artifacts.size.build_time_seconds,
            "solver": master.statistics.wall_time_seconds,
            "machine": machine_seconds, "worker": worker_seconds,
            "validator": validator_seconds, "end_to_end": result["end_to_end_seconds"]},
        "real_50_solve_invoked": True})
    manifest_hash = _write(directory, "artifact_hash_manifest.json", {
        "arm": arm, "artifacts": hashes,
        "source_fingerprint": fixture.source_fingerprint})
    print(json.dumps({
        "arm": arm, "status": final.status.value,
        "master_cp_status": master.statistics.cp_status,
        "branches": master.statistics.branches,
        "objective": master.statistics.objective_value,
        "first_incumbent": result["first_incumbent"],
        "failure_stage": result["failure_stage"],
        "recommendation": result["recommendation"],
        "artifact_manifest_sha256": manifest_hash,
    }, indent=2))
    return result


def summarize_existing():
    locations = {
        "primary": OUTPUT_DIR,
        "diagnostic_edf_hint": OUTPUT_DIR/"diagnostic_edf_hint",
        "diagnostic_8worker": OUTPUT_DIR/"diagnostic_8worker",
    }
    arms = {}
    manifest_hashes = {}
    for arm, directory in locations.items():
        result_path = directory/f"{arm}_result.json"
        master_path = directory/"master_solution.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        master = json.loads(master_path.read_text(encoding="utf-8"))
        arms[arm] = {
            "configuration": result["configuration"],
            "source_fingerprint": result["source_fingerprint"],
            "model_size": result["model_size"],
            "cp_statistics": master["statistics"],
            "first_incumbent": result["first_incumbent"],
            "master_status": result["master_status"],
            "machine_status": result["machine_status"],
            "worker_status": result["worker_status"],
            "validator_status": result["validator_status"],
            "failure_stage": result["failure_stage"],
            "hint_report": result["hint_report"],
            "end_to_end_seconds": result["end_to_end_seconds"],
        }
        manifest = directory/"artifact_hash_manifest.json"
        manifest_hashes[str(manifest)] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    summary = {
        "status": "CRAFTER_V2_50_BENCHMARK_FAIL",
        "source_verified_all_arms": all(
            row["source_fingerprint"] == EXPECTED_FINGERPRINT for row in arms.values()),
        "arms": arms,
        "interpretation": (
            "V2 substantially reduced construction size but all real-window arms "
            "remained in presolve/startup: no incumbent, bound, branch, or conflict. "
            "This is a formulation scalability failure at 431 real operations, not "
            "evidence of mathematical infeasibility and not a machine/worker failure."),
        "pipeline_reached": "MASTER only",
        "machine_materialization_invoked_with_incumbent": False,
        "worker_allocation_invoked_with_machine_feasible_solution": False,
        "independent_validator_invoked_with_complete_phenotype": False,
        "baseline_comparison": json.loads(
            (OUTPUT_DIR/"comparison_summary.json").read_text(encoding="utf-8"))["baselines"],
        "production_integration": False,
    }
    summary_hash = _write(OUTPUT_DIR, "diagnostic_summary.json", summary)
    index = {
        "summary_sha256": summary_hash,
        "arm_manifest_sha256": manifest_hashes,
        "source_fingerprint": EXPECTED_FINGERPRINT,
        "recommendation": summary["status"],
    }
    index_hash = _write(OUTPUT_DIR, "benchmark_artifact_index.json", index)
    print(json.dumps({
        "status": summary["status"], "summary_sha256": summary_hash,
        "artifact_index_sha256": index_hash,
    }, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=tuple(ARM_CONFIG), default="primary")
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    summarize_existing() if args.summarize else run_arm(args.arm)


if __name__ == "__main__":
    main()
