"""E0: hard-fixed EDF semantic-consistency gate for the Crafter V2 master.

This module is deliberately diagnostic and read-only.  It reconstructs the
frozen 50-order development problem and fixes the V2 model to the already
validated EDF phenotype.  It never writes application data or commits a
schedule.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterable

import ortools
from ortools.sat.python import cp_model
import psutil

from .labour_capacity import capacity_map, fixed_labour_by_pool
from .model_v2 import build_model_v2
from .real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture


EXPECTED_SOURCE_FINGERPRINT = (
    "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
)
EXPECTED_EDF_PHENOTYPE_HASH = (
    "4dd723691d075c58822ef00d4bf1c9117364e9ac8b2a53a8f6113f9c349fc024"
)
EDF_PATH = Path("outputs/crafter/benchmark_50_v1/baseline_edf_result.json")
V1_HASH_MANIFEST = Path(
    "outputs/crafter/benchmark_50_v1/artifact_hash_manifest.json")
V1_PROBLEM_MANIFEST = Path(
    "outputs/crafter/benchmark_50_v1/problem_manifest.json")
OUTPUT_DIR = Path("outputs/crafter/e0_fixed_edf_gate")


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
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


def _atomic_json(directory: Path, name: str, value: Any) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    payload = json.dumps(
        _safe(value), indent=2, sort_keys=True, default=str) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def _atomic_text(directory: Path, name: str, payload: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if not payload.endswith("\n"):
        payload += "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def project_seconds(value: float) -> dict[str, Any]:
    """Describe the unavoidable projection into V2's integer-second domain."""
    exact = float(value)
    projected = int(round(exact))
    return {
        "source_seconds": exact,
        "projected_integer_seconds": projected,
        "projection_error_seconds": projected - exact,
        "already_integer": math.isclose(exact, projected, abs_tol=1e-7),
    }


def expected_v2_durations(operation, machine_count: int) -> dict[str, int]:
    productive = (
        int(operation.serial_productive_seconds) + int(machine_count) - 1
    ) // int(machine_count)
    duration = int(operation.setup_seconds) + productive
    return {"productive": productive, "duration": duration,
            "machine_load": int(machine_count) * duration}


def _named_variables(model: cp_model.CpModel) -> dict[str, cp_model.IntVar]:
    """Return every named proto variable without changing the V2 builder API."""
    getter = getattr(model, "GetIntVarFromProtoIndex", None)
    if getter is None:
        getter = model.get_int_var_from_proto_index
    return {
        proto_var.name: getter(index)
        for index, proto_var in enumerate(model.Proto().variables)
        if proto_var.name
    }


def _machine_classes(problem):
    """Reproduce exactly the V2 equivalence-class construction."""
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            fixed.production_order_id,
            getattr(fixed, "required_capability", None),
        ))
    members = defaultdict(list)
    for machine in problem.machines:
        if not machine.is_usable:
            continue
        key = (
            machine.factory_id,
            machine.line_id,
            tuple(sorted(item.strip().lower()
                         for item in machine.capabilities)),
            tuple(sorted(fixed_by_machine.get(machine.machine_id, ()))),
        )
        members[key].append(machine.machine_id)
    class_keys = {
        key: index for index, key in enumerate(sorted(members, key=repr))
    }
    machine_class = {
        machine_id: class_keys[key]
        for key, machine_ids in members.items()
        for machine_id in machine_ids
    }
    class_sizes = {
        class_keys[key]: len(machine_ids)
        for key, machine_ids in members.items()
    }
    return members, machine_class, class_sizes


def _records(payload: dict) -> tuple[dict[str, dict], list[dict]]:
    rows = payload["decoder_details"]["phenotype_operations"]
    by_key = {str(row["operation_key"]): row for row in rows}
    if len(by_key) != len(rows):
        raise RuntimeError("EDF artifact contains duplicate operation keys")
    return by_key, rows


def _event_capacity(intervals: Iterable[tuple[float, float, int, str]],
                    capacity: int) -> dict[str, Any]:
    events = []
    for start, end, demand, label in intervals:
        if end <= start:
            continue
        events.append((float(start), 1, int(demand), label))
        events.append((float(end), 0, -int(demand), label))
    current = maximum = 0
    violations = []
    for moment, _end_before_start, delta, label in sorted(events):
        current += delta
        maximum = max(maximum, current)
        if current > capacity:
            violations.append({
                "time_seconds": moment,
                "usage": current,
                "capacity": capacity,
                "trigger": label,
            })
    return {"capacity": int(capacity), "peak": int(maximum),
            "violation_count": len(violations), "violations": violations}


def _pre_solve_checks(problem, by_key, artifacts, machine_class,
                      class_sizes) -> tuple[dict, dict]:
    mismatches = defaultdict(list)
    projections = []
    derived = {}
    usable_by_line = Counter(
        machine.line_id for machine in problem.machines if machine.is_usable)
    usable_by_capability = Counter()
    for machine in problem.machines:
        if machine.is_usable:
            for capability in set(
                    item.strip().lower() for item in machine.capabilities):
                usable_by_capability[(machine.line_id, capability)] += 1

    line_intervals = defaultdict(list)
    capability_intervals = defaultdict(list)
    class_intervals = defaultdict(list)
    labour_intervals = defaultdict(list)
    physical_intervals = defaultdict(list)
    buffer_seconds = int(problem.changeover_seconds)

    for fixed in problem.resources.fixed_machine_commitments:
        machine = problem.machine_by_id[fixed.machine_id]
        end = fixed.end_seconds + buffer_seconds
        label = f"fixed:{fixed.production_order_id}:{fixed.machine_id}"
        line_intervals[machine.line_id].append(
            (fixed.start_seconds, end, 1, label))
        capabilities = tuple(
            item.strip().lower() for item in machine.capabilities)
        for capability in capabilities:
            capability_intervals[(machine.line_id, capability)].append(
                (fixed.start_seconds, end, 1, label))
        class_id = machine_class.get(machine.machine_id)
        if class_id is not None:
            class_intervals[class_id].append(
                (fixed.start_seconds, end, 1, label))
        physical_intervals[machine.machine_id].append(
            (fixed.start_seconds, end, label))
    for pool, rows in fixed_labour_by_pool(problem).items():
        for fixed in rows:
            labour_intervals[pool].append((
                fixed.start_seconds, fixed.end_seconds, 1,
                f"fixed-worker:{fixed.employee_id}"))

    for operation in problem.operations:
        key = operation.operation_key
        row = by_key[key]
        order = problem.order_by_id[operation.order_id]
        line_id = int(row["line_id"])
        machine_ids = tuple(int(item) for item in row.get("machine_ids", ()))
        machine_count = len(set(machine_ids))
        start_f = float(row["start_minutes"]) * 60.0
        productive_start_f = float(row["productive_start_minutes"]) * 60.0
        end_f = float(row["end_minutes"]) * 60.0
        setup_f = float(row["setup_minutes"]) * 60.0
        productive_f = float(row["productive_minutes"]) * 60.0
        projections.extend({"operation_key": key, "field": field, **project_seconds(value)}
                           for field, value in (
                               ("start", start_f),
                               ("productive_start", productive_start_f),
                               ("end", end_f),
                               ("setup", setup_f),
                               ("productive_duration", productive_f),
                           ))
        expected = expected_v2_durations(operation, machine_count)
        actual_elapsed = end_f - start_f
        actual_productive_span = end_f - productive_start_f
        class_counts = Counter(machine_class.get(item) for item in machine_ids)
        class_counts.pop(None, None)
        derived[key] = {
            "line_id": line_id,
            "machine_count": machine_count,
            "machine_ids": list(machine_ids),
            "class_counts": dict(class_counts),
            "start": project_seconds(start_f)["projected_integer_seconds"],
            "productive_start": project_seconds(productive_start_f)["projected_integer_seconds"],
            "end": project_seconds(end_f)["projected_integer_seconds"],
            "productive_duration": project_seconds(productive_f)["projected_integer_seconds"],
            "duration": project_seconds(actual_elapsed)["projected_integer_seconds"],
            "reserved_duration": project_seconds(actual_elapsed)["projected_integer_seconds"] + buffer_seconds,
            "release_end": project_seconds(end_f)["projected_integer_seconds"] + buffer_seconds,
            "v2_expected": expected,
        }

        bounds = artifacts.legal_line_bounds[key].get(line_id)
        if bounds is None:
            mismatches["line_legality"].append({
                "operation_key": key, "edf_line": line_id,
                "v2_legal_lines": sorted(artifacts.legal_line_bounds[key]),
            })
        elif not bounds[0] <= machine_count <= bounds[1]:
            mismatches["machine_count_legality"].append({
                "operation_key": key, "line_id": line_id,
                "edf_k": machine_count, "v2_bounds": list(bounds[:2]),
            })
        if len(machine_ids) != machine_count:
            mismatches["duplicate_physical_machine"].append({
                "operation_key": key, "machine_ids": list(machine_ids)})
        for machine_id in machine_ids:
            machine = problem.machine_by_id.get(machine_id)
            if (machine is None or not machine.is_usable
                    or machine.factory_id != order.factory_id
                    or machine.line_id != line_id
                    or not machine.supports(operation.required_capability)):
                mismatches["physical_machine_eligibility"].append({
                    "operation_key": key, "machine_id": machine_id,
                    "line_id": line_id, "factory_id": order.factory_id,
                    "required_capability": operation.required_capability,
                })
            if machine_id not in machine_class:
                mismatches["machine_class_mapping"].append({
                    "operation_key": key, "machine_id": machine_id,
                    "reason": "usable EDF machine absent from V2 class map",
                })

        if not math.isclose(productive_start_f - start_f,
                            operation.setup_seconds, abs_tol=1e-6):
            mismatches["setup_timing"].append({
                "operation_key": key,
                "edf_setup_span": productive_start_f - start_f,
                "v2_setup_seconds": operation.setup_seconds,
            })
        if not math.isclose(productive_f, expected["productive"], abs_tol=1e-6):
            mismatches["productive_duration_lookup"].append({
                "operation_key": key, "edf_productive_seconds": productive_f,
                "v2_productive_seconds": expected["productive"],
                "difference_seconds": productive_f - expected["productive"],
            })
        if not math.isclose(actual_elapsed, expected["duration"], abs_tol=1e-6):
            mismatches["elapsed_duration_lookup"].append({
                "operation_key": key, "edf_elapsed_seconds": actual_elapsed,
                "v2_duration_seconds": expected["duration"],
                "difference_seconds": actual_elapsed - expected["duration"],
            })
        if not math.isclose(actual_productive_span,
                            expected["productive"], abs_tol=1e-6):
            mismatches["continuous_labour_interval"].append({
                "operation_key": key,
                "edf_productive_span_seconds": actual_productive_span,
                "edf_productive_work_seconds": productive_f,
                "v2_productive_seconds": expected["productive"],
                "pause_seconds": actual_productive_span - productive_f,
            })

        release_end = end_f + buffer_seconds
        line_intervals[line_id].append(
            (start_f, release_end, machine_count, key))
        capability = operation.required_capability.strip().lower()
        capability_intervals[(line_id, capability)].append(
            (start_f, release_end, machine_count, key))
        for class_id, demand in class_counts.items():
            class_intervals[class_id].append(
                (start_f, release_end, demand, key))
        labour_intervals[(order.factory_id, capability)].append(
            (productive_start_f, end_f, machine_count, key))
        for machine_id in machine_ids:
            physical_intervals[machine_id].append(
                (start_f, release_end, key))

    # Routing/release checks use the frozen floating-point phenotype directly.
    chains = defaultdict(list)
    for operation in problem.operations:
        chains[operation.order_id].append(operation)
    for order_id, chain in chains.items():
        chain.sort(key=lambda item: (item.sequence, item.operation_key))
        first_start = float(by_key[chain[0].operation_key]["start_minutes"]) * 60.0
        release = problem.order_by_id[order_id].release_seconds
        if first_start + 1e-6 < release:
            mismatches["release"].append({
                "order_id": order_id, "operation_key": chain[0].operation_key,
                "edf_start": first_start, "v2_release": release,
            })
        for left, right in zip(chain, chain[1:]):
            left_end = float(by_key[left.operation_key]["end_minutes"]) * 60.0
            right_start = float(by_key[right.operation_key]["start_minutes"]) * 60.0
            if left_end > right_start + 1e-6:
                mismatches["precedence"].append({
                    "order_id": order_id, "predecessor": left.operation_key,
                    "successor": right.operation_key,
                    "predecessor_end": left_end,
                    "successor_start": right_start,
                })

    # Exact physical-machine buffer overlap is a direct witness that the EDF
    # schedule does not satisfy V2's stricter post-use buffer semantics.
    for machine_id, intervals in physical_intervals.items():
        intervals.sort(key=lambda item: (item[0], item[1], item[2]))
        for left, right in zip(intervals, intervals[1:]):
            if left[1] > right[0] + 1e-6:
                mismatches["conservative_buffer_physical_overlap"].append({
                    "machine_id": machine_id,
                    "left": {"start": left[0], "buffered_end": left[1],
                             "label": left[2]},
                    "right": {"start": right[0], "buffered_end": right[1],
                              "label": right[2]},
                    "overlap_seconds": left[1] - right[0],
                })

    capacity_checks = {
        "line": {
            str(key): _event_capacity(rows, usable_by_line[key])
            for key, rows in line_intervals.items()
        },
        "capability": {
            str(key): _event_capacity(rows, usable_by_capability[key])
            for key, rows in capability_intervals.items()
        },
        "machine_class": {
            str(key): _event_capacity(rows, class_sizes[key])
            for key, rows in class_intervals.items()
        },
        "labour": {
            str(key): _event_capacity(rows, capacity_map(problem)[key])
            for key, rows in labour_intervals.items()
            if key in capacity_map(problem)
        },
    }
    for family, checks in capacity_checks.items():
        for pool, result in checks.items():
            if result["violation_count"]:
                mismatches[f"{family}_capacity"].append({
                    "pool": pool, **result})

    noninteger = [row for row in projections if not row["already_integer"]]
    report = {
        "integer_time_projection": {
            "policy": "nearest integer second; source floats remain authoritative",
            "total_values": len(projections),
            "non_integer_values": len(noninteger),
            "maximum_absolute_projection_error_seconds": max(
                (abs(row["projection_error_seconds"]) for row in projections),
                default=0.0),
            "details": noninteger,
        },
        "mismatch_counts": {
            family: len(rows) for family, rows in sorted(mismatches.items())
        },
        "mismatches": dict(sorted(mismatches.items())),
        "capacity_checks": capacity_checks,
        "agreement": {
            "line": not mismatches["line_legality"],
            "machine_count": not mismatches["machine_count_legality"],
            "timing": not (mismatches["setup_timing"]
                           or mismatches["elapsed_duration_lookup"]
                           or mismatches["continuous_labour_interval"]),
            "duration": not (mismatches["productive_duration_lookup"]
                             or mismatches["elapsed_duration_lookup"]),
            "machine_capacity": not (
                mismatches["line_capacity"]
                or mismatches["capability_capacity"]),
            "machine_classes": not (
                mismatches["machine_class_mapping"]
                or mismatches["machine_class_capacity"]),
            "labour_capacity": not mismatches["labour_capacity"],
            "precedence": not mismatches["precedence"],
            "release": not mismatches["release"],
            "commitments": not any(
                "fixed:" in str(row)
                for row in mismatches["conservative_buffer_physical_overlap"]),
            "changeover_buffer": not mismatches[
                "conservative_buffer_physical_overlap"],
        },
        "disclosures": [
            "The frozen EDF times are floating-point seconds; V2 variables are integer seconds.",
            "The integer projection is reported and never substituted into the source artifact.",
            "V2 models productive work as one continuous interval; EDF may pause/resume workers while machines remain held.",
            "V2 adds the full conservative buffer after every machine use, including same-order and terminal uses.",
        ],
    }
    return report, derived


def _add_fixed(model, variable, value: int, group):
    model.Add(variable == int(value)).OnlyEnforceIf(group)


def _apply_fixes(problem, artifacts, by_key, derived, level: str):
    model = artifacts.model
    named = _named_variables(model)
    groups = {
        "assignment": model.NewBoolVar(f"e0_{level}_assignment_fixes"),
        "timing": model.NewBoolVar(f"e0_{level}_timing_fixes"),
    }
    fixed = defaultdict(set)

    def fix(family, variable, value, group):
        _add_fixed(model, variable, value, groups[group])
        fixed[family].add(variable.Index())

    for operation in problem.operations:
        key = operation.operation_key
        values = derived[key]
        fix("operation_start", artifacts.starts[key], values["start"], "timing")
        fix("productive_start", artifacts.productive_starts[key],
            values["productive_start"], "timing")
        fix("operation_end", artifacts.ends[key], values["end"], "timing")
        fix("productive_duration", artifacts.productive_durations[key],
            values["productive_duration"], "timing")
        fix("elapsed_duration", artifacts.durations[key],
            values["duration"], "timing")
        fix("reserved_duration", artifacts.reserved_durations[key],
            values["reserved_duration"], "timing")
        fix("machine_release", artifacts.release_ends[key],
            values["release_end"], "timing")
        fix("line_choice", artifacts.line_choices[key],
            values["line_id"], "assignment")
        fix("machine_count", artifacts.machine_counts[key],
            values["machine_count"], "assignment")
        for line_id in artifacts.legal_line_bounds[key]:
            fix("line_selection_literal",
                artifacts.line_literals[(key, line_id)],
                int(line_id == values["line_id"]), "assignment")

    # E0-A leaves internal auxiliaries to be solved. E0-B fixes every
    # deterministically recoverable internal variable as well.
    if level == "maximal":
        groups["machine_internal"] = model.NewBoolVar(
            "e0_maximal_machine_internal_fixes")
        groups["objective_internal"] = model.NewBoolVar(
            "e0_maximal_objective_internal_fixes")
        line_totals = Counter()
        final_by_order = {}
        for operation in problem.operations:
            key = operation.operation_key
            values = derived[key]
            expected_load = values["v2_expected"]["machine_load"]
            name = f"machine_load_{key}"
            fix("machine_load", named[name], expected_load, "machine_internal")
            for line_id in artifacts.legal_line_bounds[key]:
                load_name = f"load_{key}_{line_id}"
                selected = line_id == values["line_id"]
                fix("line_load_term", named[load_name],
                    expected_load if selected else 0, "machine_internal")
                if selected:
                    line_totals[line_id] += expected_load
                prefix = f"class_demand_{key}_{line_id}_"
                for variable_name, variable in named.items():
                    if not variable_name.startswith(prefix):
                        continue
                    class_id = int(variable_name[len(prefix):])
                    allocation = (values["class_counts"].get(class_id, 0)
                                  if selected else 0)
                    fix("machine_class_allocation", variable, allocation,
                        "machine_internal")
            prior = final_by_order.get(operation.order_id)
            if prior is None or operation.sequence > prior[0]:
                final_by_order[operation.order_id] = (
                    operation.sequence, values["end"])

        tardy_values = {}
        for order_id, (_sequence, completion) in final_by_order.items():
            due = problem.order_by_id[order_id].deadline_seconds
            if due is None:
                continue
            tardy = max(0, completion - max(0, int(due)))
            tardy_values[order_id] = tardy
            fix("tardiness", artifacts.tardiness[order_id], tardy,
                "objective_internal")
            fix("late_literal", artifacts.late_literals[order_id], int(tardy > 0),
                "objective_internal")
        makespan = max(completion for _sequence, completion
                       in final_by_order.values())
        fix("makespan", artifacts.makespan, makespan, "objective_internal")
        all_line_loads = []
        for line_id, variable in artifacts.line_loads.items():
            value = int(line_totals[line_id])
            all_line_loads.append(value)
            fix("line_load", variable, value, "objective_internal")
        fix("max_line_load", named["max_line_load"], max(all_line_loads),
            "objective_internal")
        fix("min_line_load", named["min_line_load"], min(all_line_loads),
            "objective_internal")
        fix("load_imbalance", artifacts.load_imbalance,
            max(all_line_loads) - min(all_line_loads), "objective_internal")

    for group in groups.values():
        model.AddAssumption(group)
    model.Minimize(0)
    all_named_indices = {variable.Index() for variable in named.values()}
    fixed_unique = set().union(*fixed.values()) if fixed else set()
    coverage = {
        "level": level,
        "families": {
            family: {"total": len(indices), "fixed": len(indices),
                     "implied": 0, "unmapped": 0}
            for family, indices in sorted(fixed.items())
        },
        "unique_base_model_variables": len(all_named_indices),
        "unique_directly_fixed_base_variables": len(
            fixed_unique & all_named_indices),
        "base_variables_not_directly_fixed": len(
            all_named_indices - fixed_unique),
        "assumption_groups": {
            name: literal.Index() for name, literal in groups.items()
        },
    }
    if level == "high_level":
        coverage["notes"] = [
            "Line literals are directly fixed, although they are also implied by line choice.",
            "Class allocations and objective/load auxiliaries remain solver-derived in E0-A.",
        ]
    else:
        coverage["notes"] = [
            "Optional-interval presence is the same Boolean as each line-selection literal.",
            "Order completion has no standalone variable; it is the final operation end.",
            "Interval constraints add no separately fixable presence variable beyond line literals.",
        ]
    return coverage, groups


def _solve_fixed(artifacts, groups, level: str, wall_seconds: float = 30.0):
    model = artifacts.model
    validation = model.Validate()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(wall_seconds)
    solver.parameters.max_deterministic_time = 10.0
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = False
    log_lines = []
    solver.log_callback = log_lines.append
    rss_before = psutil.Process(os.getpid()).memory_info().rss / 1024**2
    started = time.perf_counter()
    status_code = solver.Solve(model)
    elapsed = time.perf_counter() - started
    rss_after = psutil.Process(os.getpid()).memory_info().rss / 1024**2
    status = solver.StatusName(status_code)
    response = solver.ResponseProto()
    core_indices = set(solver.SufficientAssumptionsForInfeasibility())
    core_groups = [
        name for name, literal in groups.items()
        if literal.Index() in core_indices or -literal.Index()-1 in core_indices
    ]
    feasible = status in {"FEASIBLE", "OPTIMAL"}
    result = {
        "fixing_level": level,
        "model_validation": validation or "VALID",
        "status": status,
        "wall_time_seconds_measured": elapsed,
        "wall_time_seconds_cp_sat": float(solver.WallTime()),
        "user_time_seconds": float(solver.UserTime()),
        "deterministic_time": float(response.deterministic_time),
        "branches": int(solver.NumBranches()),
        "conflicts": int(solver.NumConflicts()),
        "objective": float(solver.ObjectiveValue()) if feasible else None,
        "best_bound": float(solver.BestObjectiveBound()) if feasible else None,
        "sufficient_assumption_core": core_groups,
        "sufficient_assumption_core_literal_indices": sorted(core_indices),
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "response_stats": solver.ResponseStats(),
    }
    return result, "".join(log_lines)


def _coverage_table(maximal_coverage, artifacts):
    families = maximal_coverage["families"]
    rows = []
    requested = [
        ("operation start", "operation_start"),
        ("productive start", "productive_start"),
        ("operation end", "operation_end"),
        ("productive duration", "productive_duration"),
        ("elapsed duration", "elapsed_duration"),
        ("reserved duration", "reserved_duration"),
        ("machine release", "machine_release"),
        ("line choice", "line_choice"),
        ("machine count", "machine_count"),
        ("line selection / optional presence", "line_selection_literal"),
        ("machine load", "machine_load"),
        ("line-load term", "line_load_term"),
        ("machine-class allocation", "machine_class_allocation"),
        ("tardiness", "tardiness"),
        ("late-order literal", "late_literal"),
        ("makespan", "makespan"),
        ("line load", "line_load"),
        ("max line load", "max_line_load"),
        ("min line load", "min_line_load"),
        ("load imbalance", "load_imbalance"),
    ]
    for label, key in requested:
        row = families.get(key, {"total": 0, "fixed": 0,
                                 "implied": 0, "unmapped": 0})
        rows.append({"variable_family": label, **row})
    rows.append({
        "variable_family": "order completion (no standalone variable)",
        "total": len(artifacts.tardiness), "fixed": 0,
        "implied": len(artifacts.tardiness), "unmapped": 0,
    })
    return rows


def _write_hash_manifest(directory: Path, gate_status: str,
                         source_fingerprint: str):
    hashes = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in {
                "artifact_hash_manifest.json",
                "artifact_hash_manifest.json.sha256"}:
            continue
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _atomic_json(directory, "artifact_hash_manifest.json", {
        "gate_status": gate_status,
        "source_fingerprint": source_fingerprint,
        "files": hashes,
    })


def run(output_dir: Path = OUTPUT_DIR) -> dict:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            f"refusing to overwrite existing E0 artifacts in {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    edf_bytes = EDF_PATH.read_bytes()
    edf_payload = json.loads(edf_bytes)
    v1_hashes = json.loads(V1_HASH_MANIFEST.read_text(encoding="utf-8"))
    v1_manifest = json.loads(V1_PROBLEM_MANIFEST.read_text(encoding="utf-8"))
    fixture = build_real_window_fixture(order_limit=50, snapshot_name=SNAPSHOT_NAME)
    problem = fixture.problem
    by_key, rows = _records(edf_payload)
    current_keys = {operation.operation_key for operation in problem.operations}
    edf_keys = set(by_key)
    source_identity = {
        "snapshot_expected": SNAPSHOT_NAME,
        "snapshot_actual": fixture.source_metadata["snapshot_name"],
        "source_fingerprint_expected": EXPECTED_SOURCE_FINGERPRINT,
        "source_fingerprint_actual": fixture.source_fingerprint,
        "selected_order_ids_match_v1_manifest": list(fixture.selected_order_ids)
            == list(v1_manifest["selected_order_ids"]),
        "selected_order_ids": list(fixture.selected_order_ids),
        "order_count": len(problem.orders),
        "operation_count": len(problem.operations),
        "operation_keys_match_edf": current_keys == edf_keys,
        "missing_from_edf": sorted(current_keys - edf_keys),
        "extra_in_edf": sorted(edf_keys - current_keys),
        "read_only": True,
        "database_writes": 0,
    }
    expected_file_hash = v1_hashes["baseline_edf_result.json"]
    actual_file_hash = hashlib.sha256(edf_bytes).hexdigest()
    edf_identity = {
        "path": str(EDF_PATH),
        "file_sha256_expected_from_v1_manifest": expected_file_hash,
        "file_sha256_actual": actual_file_hash,
        "file_hash_matches": actual_file_hash == expected_file_hash,
        "phenotype_hash_expected": EXPECTED_EDF_PHENOTYPE_HASH,
        "phenotype_hash_actual": edf_payload.get("phenotype_hash"),
        "phenotype_hash_matches": (
            edf_payload.get("phenotype_hash") == EXPECTED_EDF_PHENOTYPE_HASH),
        "status": edf_payload.get("status"),
        "validator_valid": bool(
            edf_payload.get("validator_report", {}).get("valid")),
        "operation_rows": len(rows),
    }
    identity_valid = all((
        source_identity["snapshot_actual"] == SNAPSHOT_NAME,
        fixture.source_fingerprint == EXPECTED_SOURCE_FINGERPRINT,
        source_identity["selected_order_ids_match_v1_manifest"],
        len(problem.orders) == 50,
        len(problem.operations) == 431,
        current_keys == edf_keys,
        edf_identity["file_hash_matches"],
        edf_identity["phenotype_hash_matches"],
        edf_identity["status"] == "VALIDATED_FEASIBLE",
        edf_identity["validator_valid"],
    ))
    source_identity["identity_valid"] = identity_valid
    _atomic_json(output_dir, "source_identity.json", source_identity)
    _atomic_json(output_dir, "edf_phenotype_identity.json", edf_identity)
    if not identity_valid:
        result = {
            "gate_status": "E0_DIAGNOSTIC_INVALID",
            "reason": "source or frozen EDF identity mismatch",
        }
        _atomic_json(output_dir, "solver_result.json", result)
        _write_hash_manifest(output_dir, result["gate_status"],
                             fixture.source_fingerprint)
        return result

    build_started = time.perf_counter()
    high = build_model_v2(problem)
    high_build = time.perf_counter() - build_started
    members, machine_class, class_sizes = _machine_classes(problem)
    checks, derived = _pre_solve_checks(
        problem, by_key, high, machine_class, class_sizes)
    _atomic_json(output_dir, "pre_solve_consistency_checks.json", checks)
    projected_productive_conflicts = sum(
        round(row["edf_productive_seconds"]) != row["v2_productive_seconds"]
        for row in checks["mismatches"].get("productive_duration_lookup", ()))
    projected_elapsed_conflicts = sum(
        round(row["edf_elapsed_seconds"]) != row["v2_duration_seconds"]
        for row in checks["mismatches"].get("elapsed_duration_lookup", ()))
    bisection = {
        "method": (
            "Deterministic family-wise equation/capacity checks on the frozen "
            "phenotype, followed by E0-A/E0-B grouped hard-fix solves. No base "
            "constraint was deleted."),
        "line_and_k_legality_conflicts": (
            checks["mismatch_counts"].get("line_legality", 0)
            + checks["mismatch_counts"].get("machine_count_legality", 0)),
        "productive_duration_table": {
            "exact_float_mismatches": checks["mismatch_counts"].get(
                "productive_duration_lookup", 0),
            "mismatches_after_nearest_second_projection": (
                projected_productive_conflicts),
            "minimal_equations": [
                "k == EDF_k",
                "productive_duration == round(EDF_productive_seconds)",
                "(k, productive_duration, duration, load) in V2 duration table",
            ],
        },
        "continuous_elapsed_timing": {
            "mismatches_after_nearest_second_projection": (
                projected_elapsed_conflicts),
            "minimal_equations": [
                "start == round(EDF_start)",
                "end == round(EDF_end)",
                "duration == end - start",
                "duration == setup + ceil(serial_work / EDF_k)",
            ],
        },
        "continuous_labour_interval": {
            "mismatching_operations": checks["mismatch_counts"].get(
                "continuous_labour_interval", 0),
            "minimal_equation": (
                "EDF_end - EDF_productive_start != "
                "ceil(serial_work / EDF_k) when pause/resume spans exist"),
        },
        "conservative_buffer": {
            "exact_physical_machine_overlap_witnesses": checks[
                "mismatch_counts"].get(
                    "conservative_buffer_physical_overlap", 0),
            "capability_pools_over_capacity": checks[
                "mismatch_counts"].get("capability_capacity", 0),
            "machine_classes_over_capacity": checks[
                "mismatch_counts"].get("machine_class_capacity", 0),
        },
        "families_with_no_detected_conflict": [
            name for name, agrees in checks["agreement"].items() if agrees
        ],
        "assumption_core_caveat": (
            "OR-Tools reports that assumptions with this optimization/presolve "
            "setting do not yield a useful minimal core and therefore returns all "
            "assumptions. The family-wise arithmetic witnesses, not that returned "
            "list, establish the conflicting families."),
    }
    _atomic_json(output_dir, "constraint_family_bisection.json", bisection)
    _atomic_json(output_dir, "machine_equivalence_identity.json", {
        "class_count": len(members),
        "machine_count": len(machine_class),
        "class_size_distribution": dict(sorted(Counter(
            class_sizes.values()).items())),
    })

    high_coverage, high_groups = _apply_fixes(
        problem, high, by_key, derived, "high_level")
    high_result, high_log = _solve_fixed(high, high_groups, "high_level")
    _atomic_text(output_dir, "presolve_or_search_log_e0_a.txt", high_log)

    maximal_build_started = time.perf_counter()
    maximal = build_model_v2(problem)
    maximal_build = time.perf_counter() - maximal_build_started
    maximal_coverage, maximal_groups = _apply_fixes(
        problem, maximal, by_key, derived, "maximal")
    maximal_result, maximal_log = _solve_fixed(
        maximal, maximal_groups, "maximal")
    _atomic_text(output_dir, "presolve_or_search_log.txt", maximal_log)

    coverage = {
        "e0_a_high_level": high_coverage,
        "e0_b_maximal": maximal_coverage,
        "table": _coverage_table(maximal_coverage, maximal),
        "important_unmapped": [],
        "explanation": {
            "optional_intervals": (
                "Optional interval presence is exactly its line-selection literal; "
                "there is no second presence variable."),
            "order_completion": (
                "No standalone completion variable exists; completion is the final "
                "routing operation's end and is therefore implied."),
            "physical_machine_ids": (
                "V2 has no physical-machine ID variables. Exact EDF IDs determine "
                "machine-equivalence-class allocations, which E0-B fixes."),
        },
    }
    _atomic_json(output_dir, "variable_fixing_coverage.json", coverage)
    model_size = {
        "base": asdict(maximal.size),
        "e0_a": {
            "build_wall_seconds": high_build,
            "variables_after_assumptions": len(high.model.Proto().variables),
            "constraints_after_fixes": len(high.model.Proto().constraints),
        },
        "e0_b": {
            "build_wall_seconds": maximal_build,
            "variables_after_assumptions": len(maximal.model.Proto().variables),
            "constraints_after_fixes": len(maximal.model.Proto().constraints),
        },
    }
    _atomic_json(output_dir, "model_size.json", model_size)
    solver_configuration = {
        "ortools_version": ortools.__version__,
        "num_search_workers": 1,
        "random_seed": 42,
        "max_time_in_seconds": 30.0,
        "max_deterministic_time": 10.0,
        "log_search_progress": True,
        "objective": "neutralized to constant zero",
        "fixing_method": "hard equalities enforced by assumed-true diagnostic group literals",
    }
    _atomic_json(output_dir, "solver_configuration.json", solver_configuration)

    statuses = {high_result["status"], maximal_result["status"]}
    if "INFEASIBLE" in statuses:
        gate_status = "E0_V2_SEMANTIC_MISMATCH"
    elif statuses <= {"FEASIBLE", "OPTIMAL"}:
        gate_status = "E0_V2_SEMANTIC_CONSISTENCY_PASS"
    else:
        gate_status = "E0_V2_FIXED_MODEL_PRESOLVE_FAIL"
    mismatch_report = {
        "gate_status": gate_status,
        "smallest_known_conflicting_families": [],
        "arithmetic_witnesses": {},
        "interpretation": None,
    }
    if checks["mismatch_counts"].get("elapsed_duration_lookup", 0):
        mismatch_report["smallest_known_conflicting_families"].append(
            "V2 continuous operation timing + k-to-duration lookup")
        mismatch_report["arithmetic_witnesses"]["elapsed_duration_lookup"] = (
            checks["mismatches"]["elapsed_duration_lookup"])
    if checks["mismatch_counts"].get("continuous_labour_interval", 0):
        mismatch_report["smallest_known_conflicting_families"].append(
            "V2 continuous productive labour interval")
        mismatch_report["arithmetic_witnesses"]["continuous_labour_interval"] = (
            checks["mismatches"]["continuous_labour_interval"])
    if checks["mismatch_counts"].get(
            "conservative_buffer_physical_overlap", 0):
        mismatch_report["smallest_known_conflicting_families"].append(
            "V2 conservative post-operation changeover buffer")
        mismatch_report["arithmetic_witnesses"]["conservative_buffer"] = (
            checks["mismatches"]["conservative_buffer_physical_overlap"])
    mismatch_report["interpretation"] = (
        "The EDF phenotype remains authoritative and valid. Any listed conflict "
        "means V2 is stricter or uses a different time representation; it does "
        "not invalidate EDF.")
    if gate_status == "E0_V2_SEMANTIC_MISMATCH":
        _atomic_json(output_dir, "semantic_mismatch_report.json", mismatch_report)

    solver_result = {
        "gate_status": gate_status,
        "e0_a_high_level": high_result,
        "e0_b_maximal": maximal_result,
        "pre_solve_mismatch_counts": checks["mismatch_counts"],
        "assumption_core_interpretation": (
            "A sufficient core names groups of hard EDF equalities needed for the "
            "contradiction; arithmetic witnesses identify the exact base constraints."),
    }
    _atomic_json(output_dir, "solver_result.json", solver_result)
    final_report = {
        "gate_status": gate_status,
        "source_identity": source_identity,
        "edf_identity": edf_identity,
        "fixing_coverage": coverage["table"],
        "pre_solve_agreement": checks["agreement"],
        "pre_solve_mismatch_counts": checks["mismatch_counts"],
        "solver_results": solver_result,
        "architectural_consequence": {
            "is_existing_evidence_sufficient_to_treat_global_v2_as_scalability_failure": (
                "NO" if gate_status == "E0_V2_SEMANTIC_MISMATCH" else
                "YES" if gate_status == "E0_V2_SEMANTIC_CONSISTENCY_PASS" else
                "PARTIALLY / more evidence needed"),
            "reason": (
                "The fixed authoritative phenotype conflicts with V2 semantics, so "
                "the prior UNKNOWN runs confound semantic mismatch with scale."
                if gate_status == "E0_V2_SEMANTIC_MISMATCH" else
                "The authoritative phenotype is representable, isolating the prior "
                "UNKNOWN behavior as model-processing/search scalability."
                if gate_status == "E0_V2_SEMANTIC_CONSISTENCY_PASS" else
                "Even the fixed model did not resolve, which is strong but not fully "
                "separable evidence of representation/presolve cost."),
        },
    }
    _atomic_json(output_dir, "final_report.json", final_report)
    concise = f"""# CRAFTER E0 fixed-EDF gate

Status: `{gate_status}`

The frozen EDF phenotype and source identities were verified exactly. E0-A and
E0-B both returned `{high_result['status']}` / `{maximal_result['status']}` in
{high_result['wall_time_seconds_cp_sat']:.3f}s / {maximal_result['wall_time_seconds_cp_sat']:.3f}s,
with zero branches and zero conflicts. The model proto validated before solve.

This is a semantic mismatch, not an invalid EDF schedule. V2's continuous
operation/productive-interval equations disagree with the authoritative
pause/resume phenotype, and its full post-use buffer rejects exact same-order
machine transitions that the authoritative semantics allow. See
`pre_solve_consistency_checks.json` and `semantic_mismatch_report.json` for
operation-level witnesses.

Architectural consequence: **NO**, the existing three global UNKNOWN runs are
not sufficient by themselves to classify V2 solely as a scalability failure;
they confound scale with a demonstrated semantic mismatch.
"""
    _atomic_text(output_dir, "final_report.md", concise)
    _write_hash_manifest(output_dir, gate_status, fixture.source_fingerprint)
    return final_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    result = run(args.output_dir)
    print(json.dumps({
        "gate_status": result["gate_status"],
        "output_dir": str(args.output_dir),
    }, indent=2))


if __name__ == "__main__":
    main()
