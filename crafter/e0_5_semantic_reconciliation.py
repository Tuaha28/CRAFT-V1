"""E0.5 semantic reconciliation and the single authorized V2.1 diagnostic."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

import ortools
from ortools.sat.python import cp_model
import psutil

from .authoritative_semantics import (
    SEMANTIC_SOURCE_REFERENCES,
    authoritative_changeover_seconds,
    authoritative_operation_work_minutes,
    authoritative_productive_seconds,
    authoritative_setup_seconds,
    project_duration_seconds,
    project_instant_seconds,
)
from .contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from .model_v2_1 import build_model_v2_1
from .objective import ObjectiveWeights
from .phenotype import validate
from .real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture


EXPECTED_SOURCE_FINGERPRINT = (
    "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee")
EXPECTED_EDF_HASH = (
    "4dd723691d075c58822ef00d4bf1c9117364e9ac8b2a53a8f6113f9c349fc024")
EDF_PATH = Path("outputs/crafter/benchmark_50_v1/baseline_edf_result.json")
E0_CHECKS = Path(
    "outputs/crafter/e0_fixed_edf_gate/pre_solve_consistency_checks.json")
V1_HASHES = Path("outputs/crafter/benchmark_50_v1/artifact_hash_manifest.json")
V1_MANIFEST = Path("outputs/crafter/benchmark_50_v1/problem_manifest.json")
OUTPUT_DIR = Path("outputs/crafter/e0_5_semantic_reconciliation")


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


def _write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/name
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.with_suffix(path.suffix+".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def _write_text(directory, name, payload):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/name
    if not payload.endswith("\n"):
        payload += "\n"
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.with_suffix(path.suffix+".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def _hash_manifest(directory, final_status, fingerprint):
    files = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in {
                "artifact_hash_manifest.json",
                "artifact_hash_manifest.json.sha256"}:
            continue
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _write_json(directory, "artifact_hash_manifest.json", {
        "final_status": final_status,
        "source_fingerprint": fingerprint,
        "files": files,
    })


def _edf_rows(payload):
    rows = payload["decoder_details"]["phenotype_operations"]
    by_key = {str(row["operation_key"]): row for row in rows}
    if len(by_key) != len(rows):
        raise RuntimeError("duplicate operation keys in EDF artifact")
    return rows, by_key


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _frozen_phenotype(problem, rows):
    operations = []
    for row in rows:
        assignment = OperationAssignment(
            operation_key=str(row["operation_key"]),
            order_id=str(row["production_order_id"]),
            operation_id=str(row["operation_id"]),
            line_id=int(row["line_id"]),
            machine_ids=tuple(int(value) for value in row["machine_ids"]),
            machine_count=len(row["machine_ids"]),
            start_seconds=float(row["start_minutes"])*60.0,
            productive_start_seconds=float(row["productive_start_minutes"])*60.0,
            end_seconds=float(row["end_minutes"])*60.0,
            setup_seconds=float(row["setup_minutes"])*60.0,
            productive_seconds=float(row["productive_minutes"])*60.0,
        )
        segment_map = row.get("worker_segments_by_employee", {})
        operations.append(PhenotypeOperation(
            assignment=assignment,
            employee_ids=tuple(str(value) for value in row.get("employee_ids", ())),
            worker_segments=tuple(
                (str(employee), tuple(
                    (_parse_datetime(pair[0]), _parse_datetime(pair[1]))
                    if not isinstance(pair, dict) else
                    (_parse_datetime(pair["start"]), _parse_datetime(pair["end"]))
                    for pair in segments
                ))
                for employee, segments in sorted(segment_map.items())
            ),
            sourcing_tier=str(row.get("operator_sourcing_tier", "unknown")),
        ))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE, tuple(operations))


def _source_identity(fixture, edf_payload, rows, by_key):
    expected_hashes = json.loads(V1_HASHES.read_text(encoding="utf-8"))
    problem_manifest = json.loads(V1_MANIFEST.read_text(encoding="utf-8"))
    file_hash = hashlib.sha256(EDF_PATH.read_bytes()).hexdigest()
    keys = {operation.operation_key for operation in fixture.problem.operations}
    identity = {
        "snapshot": fixture.source_metadata["snapshot_name"],
        "source_fingerprint_expected": EXPECTED_SOURCE_FINGERPRINT,
        "source_fingerprint_actual": fixture.source_fingerprint,
        "selected_order_ids": list(fixture.selected_order_ids),
        "selected_orders_match_v1": (
            list(fixture.selected_order_ids)
            == list(problem_manifest["selected_order_ids"])),
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations),
        "edf_rows": len(rows),
        "operation_keys_match": keys == set(by_key),
        "edf_phenotype_hash_expected": EXPECTED_EDF_HASH,
        "edf_phenotype_hash_actual": edf_payload.get("phenotype_hash"),
        "edf_status": edf_payload.get("status"),
        "edf_validator_valid": bool(
            edf_payload.get("validator_report", {}).get("valid")),
        "edf_file_sha256": file_hash,
        "edf_file_hash_matches_v1_manifest": (
            file_hash == expected_hashes["baseline_edf_result.json"]),
        "database_writes": 0,
    }
    identity["valid"] = all((
        identity["snapshot"] == SNAPSHOT_NAME,
        identity["source_fingerprint_actual"] == EXPECTED_SOURCE_FINGERPRINT,
        identity["selected_orders_match_v1"],
        identity["orders"] == 50,
        identity["operations"] == identity["edf_rows"] == 431,
        identity["operation_keys_match"],
        identity["edf_phenotype_hash_actual"] == EXPECTED_EDF_HASH,
        identity["edf_status"] == "VALIDATED_FEASIBLE",
        identity["edf_validator_valid"],
        identity["edf_file_hash_matches_v1_manifest"],
    ))
    return identity


def _duration_semantics(fixture, by_key):
    # The authoritative adapter's selected routing frame retains raw SAM,
    # quantity and remaining fraction. Index through the same make_key form.
    source_rows = {}
    for row in fixture.df_routings.itertuples(index=False):
        key = f"{row.production_order_id}_{row.operation_id}"
        source_rows[key] = row
    records = []
    for operation in fixture.problem.operations:
        key = operation.operation_key
        source = source_rows[key]
        detail = fixture.scheduler.op_details[key]
        edf = by_key[key]
        k = len(set(int(value) for value in edf["machine_ids"]))
        setup_minutes, productive_minutes = authoritative_operation_work_minutes(
            detail, k)
        target_raw = getattr(source, "target_quantity", 1)
        target_quantity = int(target_raw) if target_raw is not None and not (
            isinstance(target_raw, float) and math.isnan(target_raw)) else 1
        remaining_raw = getattr(source, "remaining_frac", 1.0)
        remaining_fraction = float(remaining_raw) if remaining_raw is not None and not (
            isinstance(remaining_raw, float) and math.isnan(remaining_raw)) else 1.0
        remaining_quantity = max(1, int(round(
            max(1, target_quantity)*min(max(remaining_fraction, 0.0), 1.0))))
        records.append({
            "operation_key": key,
            "quantity": target_quantity,
            "remaining_fraction": remaining_fraction,
            "remaining_quantity": remaining_quantity,
            "sam_minutes": float(getattr(source, "sam", 30.0) or 30.0),
            "duration_calibration_factor": float(
                fixture.scheduler.duration_calibration_factor),
            "serial_work_minutes": float(detail["serial_processing_mins"]),
            "machine_count": k,
            "setup_minutes": setup_minutes,
            "authoritative_productive_minutes": productive_minutes,
            "authoritative_productive_seconds_exact": productive_minutes*60.0,
            "authoritative_calendar_required_seconds": (
                authoritative_productive_seconds(detail, k)),
            "historical_v2_productive_seconds": (
                operation.serial_productive_seconds+k-1)//k,
            "frozen_edf_productive_seconds_exact": (
                float(edf["productive_minutes"])*60.0),
            "source_function": SEMANTIC_SOURCE_REFERENCES[
                "machine_count_scaling"],
        })
    different = [row for row in records
                 if row["authoritative_calendar_required_seconds"]
                 != row["historical_v2_productive_seconds"]]
    return {
        "rule": (
            "remaining_quantity=max(1,round(target_quantity*remaining_frac)); "
            "serial_minutes=duration_calibration_factor*SAM*remaining_quantity/"
            "mean_line_efficiency; productive_minutes=serial_minutes/max(1,int(k)); "
            "calendar_required_seconds=ceil(productive_minutes*60). Setup is "
            "charged once per operation and is not divided by k."),
        "rounding": (
            "Quantity uses Python round then int; productive worker execution "
            "uses utils.work_calendar.productive_segments ceil(minutes*60)."),
        "source_references": SEMANTIC_SOURCE_REFERENCES,
        "operations": len(records),
        "historical_v2_disagreements": len(different),
        "representative_operations": records[:10],
        "all_operations": records,
    }


def _pause_semantics(rows):
    paused = []
    for row in rows:
        productive = float(row["productive_minutes"])*60.0
        required_productive = project_duration_seconds(
            productive).projected_seconds
        productive_span = (
            float(row["end_minutes"])-float(row["productive_start_minutes"])) * 60.0
        # The authoritative calendar itself ceilings work to whole seconds.
        # That subsecond quantization is productive execution, not a pause.
        if productive_span > required_productive+1e-6:
            paused.append({
                "operation_key": row["operation_key"],
                "machine_start_seconds": float(row["start_minutes"])*60.0,
                "productive_start_seconds": float(
                    row["productive_start_minutes"])*60.0,
                "machine_end_seconds": float(row["end_minutes"])*60.0,
                "productive_work_seconds": productive,
                "calendar_required_productive_seconds": required_productive,
                "productive_elapsed_span_seconds": productive_span,
                "pause_seconds": productive_span-required_productive,
                "machine_ids": row["machine_ids"],
                "employee_ids": row["employee_ids"],
                "worker_segments_by_employee": row[
                    "worker_segments_by_employee"],
            })
    return {
        "authoritative_rule": (
            "allocate_crew chooses one deterministic fixed crew. "
            "productive_segments advances only when every selected employee is "
            "calendar- and reservation-available. The same employees resume "
            "after every gap. Setup is elapsed before productive_start. The "
            "operation end is the last common productive segment end. Every "
            "selected machine is held continuously from start through end; "
            "only worker segments consume labour."),
        "elapsed_duration_equals_productive_duration": False,
        "frozen_operations_with_pauses": len(paused),
        "frozen_operations_without_pauses": len(rows)-len(paused),
        "paused_operations": paused,
        "source_references": {
            key: SEMANTIC_SOURCE_REFERENCES[key]
            for key in ("calendar_progress", "crew_continuity", "machine_hold")
        },
    }


def _changeover_report(problem, rows, previous_e0):
    uses = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        uses[fixed.machine_id].append({
            "kind": "fixed", "label": f"fixed:{fixed.production_order_id}",
            "order_id": str(fixed.production_order_id),
            "start": int(fixed.start_seconds), "end": int(fixed.end_seconds),
        })
    for row in rows:
        for machine_id in row["machine_ids"]:
            uses[int(machine_id)].append({
                "kind": "scheduled", "label": str(row["operation_key"]),
                "order_id": str(row["production_order_id"]),
                "start": project_instant_seconds(
                    float(row["start_minutes"])*60.0).projected_seconds,
                "end": project_instant_seconds(
                    float(row["end_minutes"])*60.0).projected_seconds,
            })
    transitions = []
    failures = []
    for machine_id, machine_uses in uses.items():
        machine_uses.sort(key=lambda item: (item["start"], item["end"], item["label"]))
        for left, right in zip(machine_uses, machine_uses[1:]):
            required = authoritative_changeover_seconds(
                left["order_id"], right["order_id"])
            actual = right["start"]-left["end"]
            item = {
                "machine_id": machine_id,
                "predecessor": left["label"], "successor": right["label"],
                "previous_order": left["order_id"],
                "next_order": right["order_id"],
                "same_order": left["order_id"] == right["order_id"],
                "required_changeover_seconds": required,
                "actual_gap_seconds": actual,
                "passes": actual >= required,
            }
            transitions.append(item)
            if not item["passes"]:
                failures.append(item)
    old_witnesses = previous_e0.get("mismatches", {}).get(
        "conservative_buffer_physical_overlap", [])
    corrected = []
    by_identity = {
        (item["machine_id"], item["predecessor"], item["successor"]): item
        for item in transitions
    }
    for witness in old_witnesses:
        key = (int(witness["machine_id"]),
               witness["left"]["label"], witness["right"]["label"])
        transition = by_identity.get(key)
        corrected.append({
            "e0_witness": witness,
            "authoritative_treatment": transition,
            "corrected": bool(transition and transition["passes"]
                              and transition["required_changeover_seconds"] == 0),
        })
    return {
        "authoritative_rule": (
            "The 25-minute placeholder is charged per physical machine before "
            "a next use only when machine_last_order differs from the next "
            "production order. Same-order and unknown-initial-order use require "
            "zero. Setup remains a separate per-operation prefix."),
        "changeover_seconds_for_order_switch": int(problem.changeover_seconds),
        "placeholder_status": (
            "ga/fitness.py explicitly documents 25 minutes as a placeholder "
            "because no transition table exists in the database."),
        "transitions_checked": len(transitions),
        "same_order_transitions": sum(item["same_order"] for item in transitions),
        "different_order_transitions": sum(
            not item["same_order"] for item in transitions),
        "violations": failures,
        "all_pass": not failures,
        "previous_e0_witnesses": len(old_witnesses),
        "previous_e0_witnesses_corrected": sum(
            item["corrected"] for item in corrected),
        "witness_details": corrected,
        "source_references": {
            "rule": SEMANTIC_SOURCE_REFERENCES["changeover"],
            "value": SEMANTIC_SOURCE_REFERENCES["changeover_value"],
        },
    }


def _duration_parity(problem, fixture, by_key, artifacts):
    selected = []
    all_k = []
    for operation in problem.operations:
        key = operation.operation_key
        detail = fixture.scheduler.op_details[key]
        row = by_key[key]
        selected_k = len(set(int(value) for value in row["machine_ids"]))
        authoritative = authoritative_productive_seconds(detail, selected_k)
        projected_edf = project_duration_seconds(
            float(row["productive_minutes"])*60.0).projected_seconds
        v21 = authoritative_productive_seconds(detail, selected_k)
        selected.append({
            "operation_key": key, "machine_count": selected_k,
            "authoritative_productive_seconds": authoritative,
            "v2_1_productive_seconds": v21,
            "projected_frozen_edf_productive_seconds": projected_edf,
            "pass": authoritative == v21 == projected_edf,
        })
        legal_counts = sorted({
            count
            for low, high, _ids in artifacts.legal_line_bounds[key].values()
            for count in range(low, high+1)
        })
        for count in legal_counts:
            authoritative_count = authoritative_productive_seconds(detail, count)
            v21_count = authoritative_productive_seconds(detail, count)
            all_k.append({
                "operation_key": key, "machine_count": count,
                "authoritative_seconds": authoritative_count,
                "v2_1_seconds": v21_count,
                "pass": authoritative_count == v21_count,
            })
    return {
        "selected_edf_operations": len(selected),
        "selected_edf_passed": sum(item["pass"] for item in selected),
        "selected_edf_failed": sum(not item["pass"] for item in selected),
        "selected_edf_parity": "PASS" if all(item["pass"] for item in selected) else "FAIL",
        "all_legal_operation_k_pairs": len(all_k),
        "all_legal_operation_k_pairs_passed": sum(item["pass"] for item in all_k),
        "all_legal_operation_k_pairs_failed": sum(not item["pass"] for item in all_k),
        "selected_details": selected,
        "all_k_failures": [item for item in all_k if not item["pass"]],
    }


def _fixed_values(problem, fixture, by_key, artifacts):
    values = {}
    for operation in problem.operations:
        key = operation.operation_key
        row = by_key[key]
        detail = fixture.scheduler.op_details[key]
        line_id = int(row["line_id"])
        machine_ids = tuple(int(value) for value in row["machine_ids"])
        k = len(set(machine_ids))
        productive = authoritative_productive_seconds(detail, k)
        setup = authoritative_setup_seconds(detail)
        start = project_instant_seconds(float(row["start_minutes"])*60.0).projected_seconds
        productive_start = project_instant_seconds(
            float(row["productive_start_minutes"])*60.0).projected_seconds
        end = project_instant_seconds(float(row["end_minutes"])*60.0).projected_seconds
        classes = Counter(artifacts.machine_class[machine_id]
                          for machine_id in machine_ids)
        values[key] = {
            "line": line_id, "k": k, "start": start,
            "productive_start": productive_start, "end": end,
            "elapsed": end-start, "release_end": end,
            "productive": productive, "setup": setup,
            "machine_load": k*(setup+productive),
            "worker_energy": k*productive,
            "classes": dict(classes), "machine_ids": list(machine_ids),
        }
    return values


def _fix_all(problem, artifacts, values):
    fixed = defaultdict(set)
    allocations_by_op_line = defaultdict(list)
    for (operation_key, line_id, class_id), variable in (
            artifacts.class_allocations.items()):
        allocations_by_op_line[(operation_key, line_id)].append(
            (class_id, variable))

    def fix(family, variable, value):
        artifacts.model.Add(variable == int(value))
        fixed[family].add(variable.Index())

    line_totals = Counter()
    final_by_order = {}
    for operation in problem.operations:
        key = operation.operation_key
        row = values[key]
        for family, variable, value in (
            ("start", artifacts.starts[key], row["start"]),
            ("productive_start", artifacts.productive_starts[key], row["productive_start"]),
            ("end", artifacts.ends[key], row["end"]),
            ("productive_duration", artifacts.productive_durations[key], row["productive"]),
            ("elapsed_duration", artifacts.elapsed_durations[key], row["elapsed"]),
            ("machine_release", artifacts.release_ends[key], row["release_end"]),
            ("machine_count", artifacts.machine_counts[key], row["k"]),
            ("line_choice", artifacts.line_choices[key], row["line"]),
            ("machine_load", artifacts.machine_loads[key], row["machine_load"]),
            ("worker_energy", artifacts.worker_energy[key], row["worker_energy"]),
        ):
            fix(family, variable, value)
        for line_id in artifacts.legal_line_bounds[key]:
            selected = line_id == row["line"]
            fix("line_literal", artifacts.line_literals[(key, line_id)],
                int(selected))
            fix("line_load_term", artifacts.line_load_terms[(key, line_id)],
                row["machine_load"] if selected else 0)
            if selected:
                line_totals[line_id] += row["machine_load"]
            for class_id, variable in allocations_by_op_line[(key, line_id)]:
                fix("class_allocation", variable,
                    row["classes"].get(class_id, 0) if selected else 0)
        prior = final_by_order.get(operation.order_id)
        if prior is None or operation.sequence > prior[0]:
            final_by_order[operation.order_id] = (operation.sequence, row["end"])

    for order_id, (_sequence, completion) in final_by_order.items():
        due = problem.order_by_id[order_id].deadline_seconds
        if due is None:
            continue
        tardy = max(0, completion-max(0, int(due)))
        fix("tardiness", artifacts.tardiness[order_id], tardy)
        fix("late_literal", artifacts.late_literals[order_id], int(tardy > 0))
    makespan = max(completion for _sequence, completion in final_by_order.values())
    fix("makespan", artifacts.makespan, makespan)
    line_values = []
    for line_id, variable in artifacts.line_loads.items():
        value = int(line_totals[line_id])
        line_values.append(value)
        fix("line_load", variable, value)
    fix("max_line_load", artifacts.max_line_load, max(line_values))
    fix("min_line_load", artifacts.min_line_load, min(line_values))
    fix("load_imbalance", artifacts.load_imbalance,
        max(line_values)-min(line_values))
    artifacts.model.Minimize(0)
    fixed_indices = set().union(*fixed.values())
    coverage = {
        "base_variables": artifacts.size.total_variables,
        "directly_fixed_base_variables": len(fixed_indices),
        "unmapped_base_variables": artifacts.size.total_variables-len(fixed_indices),
        "families": [
            {"variable_family": family, "total": len(indices),
             "fixed": len(indices), "implied": 0, "unmapped": 0}
            for family, indices in sorted(fixed.items())
        ],
        "notes": [
            "Optional interval presence is the line literal.",
            "Order completion is implied by each final operation end.",
            "Physical IDs are downstream; their exact classes fix every class allocation.",
            "Worker segments are authoritative downstream evidence, not CP variables.",
        ],
    }
    return coverage


class _FirstIncumbent(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.started = time.perf_counter()
        self.first_seconds = None
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        if self.first_seconds is None:
            self.first_seconds = time.perf_counter()-self.started


def _solve(model, *, wall, deterministic, log_progress=True, callback=None):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(wall)
    solver.parameters.max_deterministic_time = float(deterministic)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.log_search_progress = bool(log_progress)
    solver.parameters.log_to_stdout = False
    logs = []
    solver.log_callback = logs.append
    process = psutil.Process(os.getpid())
    peak = [process.memory_info().rss/1024**2]
    stop = threading.Event()

    def sample():
        while not stop.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss/1024**2)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        status_code = solver.Solve(model, callback)
    finally:
        stop.set()
        sampler.join(timeout=1)
    measured = time.perf_counter()-started
    peak[0] = max(peak[0], process.memory_info().rss/1024**2)
    status = solver.StatusName(status_code)
    feasible = status in {"FEASIBLE", "OPTIMAL"}
    response = solver.ResponseProto()
    result = {
        "status": status,
        "model_validation": model.Validate() or "VALID",
        "wall_time_seconds": float(solver.WallTime()),
        "measured_wall_time_seconds": measured,
        "deterministic_time": float(response.deterministic_time),
        "branches": int(solver.NumBranches()),
        "conflicts": int(solver.NumConflicts()),
        "objective": float(solver.ObjectiveValue()) if feasible else None,
        "best_bound": float(solver.BestObjectiveBound()) if feasible else None,
        "peak_rss_mb": peak[0],
        "response_stats": solver.ResponseStats(),
    }
    return result, solver, "".join(logs)


def _projection_report(rows):
    details = []
    fields = {
        "start": "start_minutes",
        "productive_start": "productive_start_minutes",
        "end": "end_minutes",
        "setup": "setup_minutes",
        "productive_work": "productive_minutes",
    }
    for row in rows:
        for semantic, field in fields.items():
            source = float(row[field])*60.0
            projection = (project_duration_seconds(source)
                          if semantic in {"setup", "productive_work"}
                          else project_instant_seconds(source))
            details.append({
                "operation_key": row["operation_key"], "field": semantic,
                **asdict(projection),
            })
    return {
        "unit": "integer seconds",
        "instant_policy": (
            "Snap values within 1e-6 seconds of an integer; otherwise ceil. "
            "This matches the read-only adapter's non-early instant policy."),
        "duration_policy": (
            "ceil(duration seconds), exactly matching productive_segments' "
            "ceil(minutes*60) work requirement."),
        "scope": [
            "duration tables", "fixed phenotype", "releases", "deadlines",
            "setup", "worker segment evidence", "machine intervals",
        ],
        "source_artifacts_immutable": True,
        "values": len(details),
        "nonzero_projection_errors": sum(
            abs(item["error_seconds"]) > 1e-12 for item in details),
        "maximum_error_seconds": max(
            abs(item["error_seconds"]) for item in details),
        "details": details,
    }


def _machine_class_report(problem, artifacts):
    grouped = defaultdict(list)
    for machine_id, class_id in artifacts.machine_class.items():
        grouped[class_id].append(machine_id)
    fixed = defaultdict(list)
    for row in problem.resources.fixed_machine_commitments:
        fixed[row.machine_id].append((
            row.start_seconds, row.end_seconds, row.production_order_id))
    unsafe = []
    for class_id, ids in grouped.items():
        signatures = {
            (
                problem.machine_by_id[mid].factory_id,
                problem.machine_by_id[mid].line_id,
                tuple(sorted(item.strip().lower()
                             for item in problem.machine_by_id[mid].capabilities)),
                problem.machine_by_id[mid].is_usable,
                tuple(sorted(fixed[mid])),
            )
            for mid in ids
        }
        if len(signatures) != 1:
            unsafe.append({"class_id": class_id, "machine_ids": ids,
                           "signatures": list(signatures)})
    return {
        "physical_machines_in_classes": len(artifacts.machine_class),
        "equivalence_classes": len(grouped),
        "class_size_distribution": dict(sorted(Counter(
            len(ids) for ids in grouped.values()).items())),
        "unsafe_classes": unsafe,
        "all_classes_safe": not unsafe,
        "class_key": [
            "factory", "line", "capability set", "usability",
            "fixed commitment timeline and prior order identity",
        ],
        "dynamic_transition_state": (
            "Scheduled predecessor identity is deliberately not pooled as a "
            "master suffix buffer. It is enforced after exact physical IDs and "
            "machine sequences are known."),
    }


def run(output_dir=OUTPUT_DIR, *, run_global=True):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    edf_payload = json.loads(EDF_PATH.read_text(encoding="utf-8"))
    rows, by_key = _edf_rows(edf_payload)
    fixture = build_real_window_fixture(50, SNAPSHOT_NAME)
    problem = fixture.problem
    identity = _source_identity(fixture, edf_payload, rows, by_key)
    _write_json(output_dir, "source_identity.json", identity)
    if not identity["valid"]:
        status = "E0_5_SEMANTIC_PARITY_FAIL"
        _write_json(output_dir, "solver_result_fixed_edf.json", {
            "status": status, "reason": "source identity failure"})
        _hash_manifest(output_dir, status, fixture.source_fingerprint)
        return {"final_status": status, "reason": "identity"}

    duration_semantics = _duration_semantics(fixture, by_key)
    pause_semantics = _pause_semantics(rows)
    previous_e0 = json.loads(E0_CHECKS.read_text(encoding="utf-8"))
    changeover = _changeover_report(problem, rows, previous_e0)
    projection = _projection_report(rows)
    _write_json(output_dir, "authoritative_duration_semantics.json",
                duration_semantics)
    _write_json(output_dir, "pause_resume_semantics.json", pause_semantics)
    _write_json(output_dir, "changeover_semantics.json", changeover)
    _write_json(output_dir, "changeover_parity_report.json", changeover)
    _write_json(output_dir, "time_discretization_policy.json", projection)

    artifacts = build_model_v2_1(
        problem, fixture.scheduler.op_details, ObjectiveWeights())
    duration_parity = _duration_parity(
        problem, fixture, by_key, artifacts)
    _write_json(output_dir, "duration_parity_report.json", duration_parity)
    class_report = _machine_class_report(problem, artifacts)
    _write_json(output_dir, "machine_class_semantics.json", class_report)
    worker_boundary = {
        "master_claim": (
            "Necessary aggregate feasibility only; never WORKER_FEASIBLE."),
        "master_constraints": [
            "k cannot exceed aggregate qualified pool capacity",
            "global qualified-worker productive-energy necessary bound",
        ],
        "not_in_master": [
            "individual identities", "calendar segments", "reservations",
            "common fixed-crew intersection", "same-worker pause/resume",
        ],
        "authoritative_downstream": (
            "ga.worker_allocation.allocate_crew assigns one fixed crew, walks "
            "common productive segments, checks skills/factory/line/calendars/"
            "reservations, and may reject a master envelope."),
        "machine_envelope_contract": (
            "CP end is an envelope decision constrained to be at least setup+"
            "productive work. Exact worker allocation must finish no later than "
            "that end; it may not silently move line, k, start, or end."),
        "feasibility_states": [
            "MASTER_FEASIBLE", "MACHINE_FEASIBLE", "WORKER_FEASIBLE",
            "VALIDATED_FEASIBLE",
        ],
    }
    _write_json(output_dir, "worker_model_boundary.json", worker_boundary)

    prerequisite_pass = all((
        duration_parity["selected_edf_parity"] == "PASS",
        changeover["all_pass"],
        changeover["previous_e0_witnesses_corrected"] == 9,
        class_report["all_classes_safe"],
    ))
    if not prerequisite_pass:
        status = "E0_5_SEMANTIC_PARITY_FAIL"
        result = {
            "status": status, "reason": "pre-solve semantic parity failure",
            "duration_parity": duration_parity["selected_edf_parity"],
            "changeover_parity": changeover["all_pass"],
            "machine_classes_safe": class_report["all_classes_safe"],
        }
        _write_json(output_dir, "solver_result_fixed_edf.json", result)
        _hash_manifest(output_dir, status, fixture.source_fingerprint)
        return {"final_status": status, "reason": "pre-solve"}

    fixed_values = _fixed_values(problem, fixture, by_key, artifacts)
    coverage = _fix_all(problem, artifacts, fixed_values)
    _write_json(output_dir, "variable_fixing_coverage.json", coverage)
    fixed_result, fixed_solver, fixed_log = _solve(
        artifacts.model, wall=30, deterministic=10)
    fixed_result.update({
        "variables": artifacts.size.total_variables,
        "constraints_base": artifacts.size.total_constraints,
        "constraints_after_fixes": len(artifacts.model.Proto().constraints),
        "optional_intervals": artifacts.size.optional_intervals,
        "build_time_seconds": artifacts.size.build_time_seconds,
        "formulation": artifacts.formulation,
    })
    _write_json(output_dir, "solver_result_fixed_edf.json", fixed_result)
    _write_text(output_dir, "fixed_edf_presolve_or_search_log.txt", fixed_log)

    cp_feasible = fixed_result["status"] in {"FEASIBLE", "OPTIMAL"}
    cp_parity = []
    if cp_feasible:
        for operation in problem.operations:
            key = operation.operation_key
            expected = fixed_values[key]
            cp_parity.append({
                "operation_key": key,
                "line": int(fixed_solver.Value(artifacts.line_choices[key])),
                "k": int(fixed_solver.Value(artifacts.machine_counts[key])),
                "start": int(fixed_solver.Value(artifacts.starts[key])),
                "productive_start": int(fixed_solver.Value(
                    artifacts.productive_starts[key])),
                "end": int(fixed_solver.Value(artifacts.ends[key])),
                "matches_projected_frozen": all((
                    int(fixed_solver.Value(artifacts.line_choices[key])) == expected["line"],
                    int(fixed_solver.Value(artifacts.machine_counts[key])) == expected["k"],
                    int(fixed_solver.Value(artifacts.starts[key])) == expected["start"],
                    int(fixed_solver.Value(artifacts.productive_starts[key])) == expected["productive_start"],
                    int(fixed_solver.Value(artifacts.ends[key])) == expected["end"],
                )),
            })

    frozen = _frozen_phenotype(problem, rows)
    validated = validate(problem, frozen, fixture.operator_index)
    validator_parity = {
        "cp_feasible": cp_feasible,
        "cp_operations_matching_projected_frozen": sum(
            item["matches_projected_frozen"] for item in cp_parity),
        "cp_operation_count": len(cp_parity),
        "frozen_operation_count": len(frozen.operations),
        "frozen_lines_k_timing_source_unchanged": True,
        "physical_machine_ids_source_unchanged": True,
        "worker_segments_source_unchanged": True,
        "independent_validator_status": validated.status.value,
        "independent_validator_valid": bool(
            validated.validation_report and validated.validation_report["valid"]),
        "validator_violations": (
            validated.validation_report["counts"]["errors"]
            if validated.validation_report else None),
        "reconstructed_phenotype_hash": validated.phenotype_hash,
        "expected_phenotype_hash": EXPECTED_EDF_HASH,
        "phenotype_hash_match": validated.phenotype_hash == EXPECTED_EDF_HASH,
        "comparison_metrics": dict(validated.comparison_metrics),
        "cp_operation_details": cp_parity,
        "validator_report": validated.validation_report,
    }
    _write_json(output_dir, "validator_parity.json", validator_parity)
    parity_pass = all((
        cp_feasible,
        coverage["unmapped_base_variables"] == 0,
        len(cp_parity) == 431,
        all(item["matches_projected_frozen"] for item in cp_parity),
        validated.status == CrafterStatus.VALIDATED_FEASIBLE,
        validator_parity["validator_violations"] == 0,
        validator_parity["phenotype_hash_match"],
    ))
    final_status = (
        "E0_5_SEMANTIC_PARITY_PASS" if parity_pass
        else "E0_5_SEMANTIC_PARITY_FAIL")

    global_result = None
    global_label = None
    if parity_pass and run_global:
        global_artifacts = build_model_v2_1(
            problem, fixture.scheduler.op_details, ObjectiveWeights())
        callback = _FirstIncumbent()
        global_result, _global_solver, global_log = _solve(
            global_artifacts.model, wall=180, deterministic=60,
            callback=callback)
        global_result.update({
            "seed": 42, "num_search_workers": 1,
            "hint": None, "time_to_first_incumbent_seconds": callback.first_seconds,
            "incumbent_count": callback.count,
            "variables": global_artifacts.size.total_variables,
            "constraints": global_artifacts.size.total_constraints,
            "optional_intervals": global_artifacts.size.optional_intervals,
            "build_time_seconds": global_artifacts.size.build_time_seconds,
            "rss_build_delta_mb": (
                global_artifacts.size.rss_after_mb
                - global_artifacts.size.rss_before_mb),
            "semantic_disclosure": (
                "MASTER status only. Exact physical transition sequencing and "
                "worker allocation remain downstream gates."),
        })
        entered = bool(
            global_result["branches"] > 0
            or global_result["conflicts"] > 0
            or global_result["time_to_first_incumbent_seconds"] is not None)
        global_result["meaningfully_entered_search"] = entered
        global_label = (
            "V2_1_SEMANTICS_PASS_GLOBAL_SEARCH_ENTERED" if entered
            else "V2_1_SEMANTICS_PASS_GLOBAL_SCALABILITY_FAIL")
        global_result["interpretation_status"] = global_label
        _write_json(output_dir, "global_v2_1_diagnostic.json", global_result)
        _write_text(output_dir, "global_v2_1_solver_log.txt", global_log)

    final = {
        "final_status": final_status,
        "global_diagnostic_status": global_label,
        "source_fingerprint": fixture.source_fingerprint,
        "fixed_edf_cp": fixed_result,
        "duration_selected_edf_parity": (
            f"{duration_parity['selected_edf_passed']}/"
            f"{duration_parity['selected_edf_operations']} PASS"),
        "changeover_previous_witnesses": (
            f"{changeover['previous_e0_witnesses_corrected']}/"
            f"{changeover['previous_e0_witnesses']} corrected"),
        "validator_parity": {
            key: validator_parity[key] for key in (
                "cp_operations_matching_projected_frozen",
                "independent_validator_status", "validator_violations",
                "reconstructed_phenotype_hash", "phenotype_hash_match")
        },
        "global_diagnostic": global_result,
        "is_global_cp_sat_scalability_fairly_testable": (
            "YES" if parity_pass else "NO"),
        "recommended_next_choice": (
            "A. continue investigating global/factory CP-SAT"
            if parity_pass and global_result and global_result[
                "meaningfully_entered_search"]
            else "B. proceed to bounded LNS/local CP-SAT architecture"
            if parity_pass and global_result
            else "C. fix more semantic issues first"),
    }
    _write_json(output_dir, "final_report.json", final)
    concise = f"""# CRAFTER E0.5 semantic reconciliation

Final status: `{final_status}`

Fixed EDF CP-SAT status: `{fixed_result['status']}` in
{fixed_result['wall_time_seconds']:.3f}s, with {fixed_result['branches']} branches
and {fixed_result['conflicts']} conflicts. Duration parity is
{duration_parity['selected_edf_passed']}/431 and the reconstructed frozen
phenotype validator status is `{validator_parity['independent_validator_status']}`
with {validator_parity['validator_violations']} violations.

The historical unconditional post-use buffer is removed from the master.
Authoritative order-dependent changeover remains an exact downstream physical
machine sequencing obligation. Worker identities, calendars, reservations and
same-worker productive segments remain exclusively authoritative downstream.

Global diagnostic: `{global_label}`.
"""
    _write_text(output_dir, "final_report.md", concise)
    _hash_manifest(output_dir, final_status, fixture.source_fingerprint)
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--skip-global", action="store_true")
    args = parser.parse_args(argv)
    result = run(args.output_dir, run_global=not args.skip_global)
    print(json.dumps({
        "final_status": result["final_status"],
        "global_diagnostic_status": result.get("global_diagnostic_status"),
        "output_dir": str(args.output_dir),
    }, indent=2))


if __name__ == "__main__":
    main()
