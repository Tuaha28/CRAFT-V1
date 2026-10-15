"""Bounded E1.4 line+k local CP model.

This module is intentionally separate from the historical E1.1/E1.2 models.
It builds a small option model for one 30-operation neighborhood.  Options
are generated with the authoritative duration/calendar primitives, while
physical-machine choices are represented only for the released operations and
are checked again by the transaction materializer.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import os
import time

import psutil
from ortools.sat.python import cp_model

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds,
    authoritative_operation_work_minutes,
    authoritative_setup_seconds,
)
from ga.worker_allocation import allocate_crew, normalize_reservations

from .analysis import seconds
from .anchored_model import outside_machine_reservations, outside_worker_reservations
from .transaction import _machine_accepts, _outside_worker_state


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss / 1024.0**2


@dataclass(frozen=True)
class LineKOption:
    operation_key: str
    option_id: int
    line_id: int
    machine_count: int
    start_seconds: int
    productive_start_seconds: int
    end_seconds: int
    setup_seconds: int
    productive_seconds: int
    candidate_machine_ids: tuple[int, ...]
    preview_worker_ids: tuple[str, ...]
    preview_segments: tuple
    incumbent: bool = False


@dataclass(frozen=True)
class LineKModelSize:
    operations: int
    released_operations: int
    fixed_mode_operations: int
    options: int
    variables: int
    constraints: int
    optional_intervals: int
    exact_machine_boolean_variables: int
    exact_machine_conflict_constraints: int
    fixed_boundary_intervals: int
    build_time_seconds: float
    rss_increase_mb: float


@dataclass
class LineKArtifacts:
    model: cp_model.CpModel
    options: dict
    option_literals: dict
    machine_literals: dict
    starts: dict
    line_vars: dict
    k_vars: dict
    productive_starts: dict
    ends: dict
    size: LineKModelSize
    diagnostics: dict


class FirstSolution(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.first = None
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        if self.first is None:
            self.first = self.WallTime()


def _available_machine_pool(problem, operation, line_id):
    factory_id = problem.order_by_id[operation.order_id].factory_id
    return tuple(sorted(machine.machine_id for machine in problem.machines
        if machine.is_usable and machine.factory_id == factory_id
        and machine.line_id == int(line_id)
        and machine.supports(operation.required_capability)))


def _event_starts(problem, index, key, line_id, machine_pool, machine_res,
                  worker_res, maximum=12):
    operation = problem.operation_by_key[key]
    row = index.by_key[key]
    predecessor = index.route_predecessor.get(key)
    successor = index.route_successor.get(key)
    release = max(0, int(problem.order_by_id[operation.order_id].release_seconds))
    values = {seconds(row["start_minutes"]), release}
    if predecessor and predecessor not in index.operation_features:
        values.add(seconds(index.by_key[predecessor]["end_minutes"]))
    elif predecessor:
        # The caller freezes all operations outside the neighborhood.  The
        # explicit test below is corrected by the local builder's boundary.
        values.add(seconds(index.by_key[predecessor]["end_minutes"]))
    if successor:
        values.add(seconds(index.by_key[successor]["start_minutes"]))
    setup = authoritative_setup_seconds(index._op_details[key]) if hasattr(index, "_op_details") else 0
    for machine_id in machine_pool:
        for _left, right, previous_order, _other in machine_res.get(machine_id, ()):
            values.add(right + authoritative_changeover_seconds(
                previous_order, operation.order_id))
    for intervals in worker_res.values():
        for left, right in intervals:
            values.add(max(0, int(round((right-problem.origin).total_seconds()))-setup))
            values.add(max(0, int(round((left-problem.origin).total_seconds()))-setup))
    low = release
    values = sorted(set(int(value) for value in values
                        if low <= int(value) <= problem.horizon_seconds))
    incumbent = seconds(row["start_minutes"])
    values.sort(key=lambda value: (abs(value-incumbent), value))
    return tuple(sorted(set(values[:maximum]) | {incumbent}))


def _candidate_starts(problem, index, op_details, key, mutable, machine_pool,
                      machine_res, worker_res, maximum=14):
    operation = problem.operation_by_key[key]
    row = index.by_key[key]
    predecessor = index.route_predecessor.get(key)
    successor = index.route_successor.get(key)
    release = max(0, int(problem.order_by_id[operation.order_id].release_seconds))
    values = {seconds(row["start_minutes"]), release}
    if predecessor and predecessor not in mutable:
        values.add(seconds(index.by_key[predecessor]["end_minutes"]))
    if successor and successor not in mutable:
        values.add(seconds(index.by_key[successor]["start_minutes"]))
    setup = authoritative_setup_seconds(op_details[key])
    for machine_id in machine_pool:
        for _left, right, previous_order, _other in machine_res.get(machine_id, ()):
            values.add(right + authoritative_changeover_seconds(
                previous_order, operation.order_id))
    for employee_intervals in worker_res.values():
        for _left, right in employee_intervals:
            values.add(max(0, int(round((right-problem.origin).total_seconds()))-setup))
    # The incumbent is always retained.  Keep a small event-aligned domain;
    # this is a bounded repair model, not a second-level time grid.
    values = [int(v) for v in values if release <= int(v) <= problem.horizon_seconds]
    incumbent = seconds(row["start_minutes"])
    values.sort(key=lambda value: (abs(value-incumbent), value))
    return tuple(sorted(set(values[:maximum]) | {incumbent}))


def _preview_worker(problem, operator_index, operation, line_id, k,
                    productive_start, productive_minutes, reservations):
    result = allocate_crew(
        operator_index, operation.required_capability, int(line_id), int(k),
        problem.origin + timedelta(seconds=int(productive_start)),
        float(productive_minutes), reservations, None, priority=0.5,
        state_is_normalized=True)
    if result.get("shortage") is not None:
        return None, result.get("shortage")
    end = int(round((result["end"] - problem.origin).total_seconds()))
    return (end, tuple(str(v) for v in result["employees"]),
            tuple(result["segments"])), None


def _mode_candidates(problem, op_details, operation, row, key, released,
                     candidate_modes, incumbent_line, incumbent_k):
    modes = []
    if not released:
        modes.append({"line_id": int(incumbent_line), "k": int(incumbent_k),
                      "incumbent": True})
    else:
        seen = set()
        for item in candidate_modes:
            pair = (int(item["line_id"]), int(item["k"]))
            if pair not in seen:
                modes.append({"line_id": pair[0], "k": pair[1],
                              "incumbent": pair == (int(incumbent_line), int(incumbent_k))})
                seen.add(pair)
        if (int(incumbent_line), int(incumbent_k)) not in seen:
            modes.insert(0, {"line_id": int(incumbent_line), "k": int(incumbent_k),
                             "incumbent": True})
    return modes


def build_line_k_model(problem, op_details, operator_index, index,
                       neighborhood, released_keys, candidate_domains,
                       *, max_starts=10, machine_pool_limit=30,
                       forbidden_options=(), exact_machine_sets=None):
    """Build a bounded line+k option model for one factory-local region."""
    started, rss_before = time.perf_counter(), _rss_mb()
    mutable = set(neighborhood.operation_keys)
    released = set(released_keys)
    if not released <= mutable or len(released) > 5:
        raise ValueError("released operations must be a subset of <=5 local operations")
    model = cp_model.CpModel()
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = outside_worker_reservations(problem, index, mutable)
    options, option_literals, machine_literals = {}, {}, {}
    starts, productive_starts, ends = {}, {}, {}
    line_vars, k_vars = {}, {}
    machine_intervals = defaultdict(list)
    worker_intervals = defaultdict(list)
    fixed_boundary = 0
    optional_intervals = 0
    exact_machine_bools = 0
    rejected = {}
    forbidden_options = {tuple(item) for item in forbidden_options}
    exact_machine_sets = exact_machine_sets or {}

    for key in sorted(mutable):
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        incumbent_line = int(row["line_id"])
        incumbent_machines = tuple(sorted(int(v) for v in row["machine_ids"]))
        incumbent_k = len(incumbent_machines)
        modes = _mode_candidates(problem, op_details[key], operation, row, key,
                                 key in released, candidate_domains.get(key, ()),
                                 incumbent_line, incumbent_k)
        candidate_options, failures = [], []
        for mode in modes:
            line_id, k = int(mode["line_id"]), int(mode["k"])
            if line_id not in operation.eligible_line_ids or k < 1:
                failures.append({"mode": mode, "reason": "illegal line or k"})
                continue
            pool = _available_machine_pool(problem, operation, line_id)
            if len(pool) < k:
                failures.append({"mode": mode, "reason": "insufficient usable machines",
                                 "available": len(pool)})
                continue
            if not (key in released):
                pool = incumbent_machines
            elif len(pool) > machine_pool_limit:
                # Keep incumbent IDs first, then deterministic low-conflict IDs.
                pool = tuple(sorted(pool, key=lambda mid: (
                    0 if mid in incumbent_machines else 1,
                    len(machine_res.get(mid, ())), mid))[:max(k, machine_pool_limit)])
            starts_to_try = _candidate_starts(
                problem, index, op_details, key, mutable, pool,
                machine_res, worker_res, maximum=max_starts)
            setup = authoritative_setup_seconds(op_details[key])
            _setup_minutes, productive_minutes = authoritative_operation_work_minutes(
                op_details[key], k)
            productive_seconds = int(__import__('math').ceil(float(productive_minutes)*60.0))
            mode_sets = exact_machine_sets.get(key, {}).get((line_id, k), []) if key in released else None
            if key in released and mode_sets is not None:
                mode_sets = [tuple(sorted(int(v) for v in values)) for values in mode_sets]
                mode_sets = list(dict.fromkeys(mode_sets))
            else:
                mode_sets = [None]
            for start in starts_to_try:
                productive_start = int(start + setup)
                # Use the exact worker calendar primitive for a bounded preview.
                # A fresh reservation map prevents one option from contaminating
                # another; the final transaction allocates crews jointly.
                reservations = normalize_reservations({
                    str(employee): list(intervals)
                    for employee, intervals in worker_res.items()})
                preview, shortage = _preview_worker(
                    problem, operator_index, operation, line_id, k,
                    productive_start, productive_minutes, reservations)
                if preview is None:
                    failures.append({"mode": mode, "start_seconds": start,
                                     "reason": "worker preview", "detail": str(shortage)})
                    continue
                end, preview_workers, preview_segments = preview
                if key not in released and start == seconds(row["start_minutes"]):
                    # Preserve the authoritative incumbent envelope and segments
                    # exactly for the incumbent option.
                    end = seconds(row["end_minutes"])
                    segment_map = row.get("worker_segments_by_employee", {})
                    if incumbent_k and segment_map:
                        representative = segment_map.get(str(row["employee_ids"][0]), ())
                        preview_workers = tuple(str(v) for v in row.get("employee_ids", ()))
                        preview_segments = tuple(
                            (datetime.fromisoformat(str(pair[0])),
                             datetime.fromisoformat(str(pair[1])))
                            if not isinstance(pair, dict) else
                            (datetime.fromisoformat(str(pair["start"])),
                             datetime.fromisoformat(str(pair["end"])))
                            for pair in representative)
                if start < max(0, int(problem.order_by_id[operation.order_id].release_seconds)):
                    continue
                predecessor = index.route_predecessor.get(key)
                successor = index.route_successor.get(key)
                if predecessor and predecessor not in mutable and start < seconds(index.by_key[predecessor]["end_minutes"]):
                    continue
                if successor and successor not in mutable and end > seconds(index.by_key[successor]["start_minutes"]):
                    continue
                feasible = [mid for mid in pool if _machine_accepts(
                    machine_res.get(mid, ()), start, end, operation.order_id)]
                if len(feasible) < k:
                    failures.append({"mode": mode, "start_seconds": start,
                                     "reason": "outside machine boundary",
                                     "available": feasible, "required": k})
                    continue
                # For the incumbent mode preserve exact IDs; released modes
                # retain a bounded pool and CP chooses exactly k IDs.
                if key not in released and not set(incumbent_machines).issubset(feasible):
                    continue
                for exact_set in mode_sets:
                    selected_machines = tuple(sorted(exact_set if exact_set is not None else feasible))
                    if len(selected_machines) != k or not set(selected_machines).issubset(feasible):
                        failures.append({"mode": mode, "start_seconds": start,
                                         "reason": "exact machine set unavailable",
                                         "machine_ids": list(selected_machines)})
                        continue
                    option = LineKOption(
                        key, len(candidate_options), line_id, k, int(start),
                        productive_start, int(end), setup, productive_seconds,
                        selected_machines, tuple(preview_workers),
                        tuple(preview_segments), bool(mode.get("incumbent") and
                                                     line_id == incumbent_line and k == incumbent_k and
                                                     int(start) == seconds(row["start_minutes"])))
                    # E1.5 feedback cuts are deliberately timing/option specific.
                    fingerprint = (key, int(line_id), int(k), int(start), int(end))
                    if fingerprint in forbidden_options:
                        failures.append({"mode": mode, "start_seconds": start,
                                         "end_seconds": end,
                                         "reason": "e1.5 timing feedback cut"})
                        continue
                    candidate_options.append(option)
        if not candidate_options:
            raise ValueError(f"no line+k timing options for {key}: {failures[:4]}")
        options[key] = tuple(candidate_options)
        rejected[key] = failures
        timing_lits = []
        for option in candidate_options:
            lit = model.NewBoolVar(f"option_{key}_{option.option_id}")
            option_literals[(key, option.option_id)] = lit
            timing_lits.append(lit)
            if key in released and key not in exact_machine_sets:
                machine_lits = []
                for machine_id in option.candidate_machine_ids:
                    use = model.NewBoolVar(f"machine_{key}_{option.option_id}_{machine_id}")
                    machine_literals[(key, option.option_id, machine_id)] = use
                    model.Add(use <= lit)
                    machine_lits.append(use)
                    machine_intervals[machine_id].append(
                        model.NewOptionalIntervalVar(
                            option.start_seconds,
                            option.end_seconds-option.start_seconds,
                            option.end_seconds, use,
                            f"hold_{key}_{option.option_id}_{machine_id}"))
                    exact_machine_bools += 1
                    optional_intervals += 1
                model.Add(sum(machine_lits) == option.machine_count*lit)
            elif key in released:
                # E1.6: the option literal itself owns a complete exact
                # machine set.  There are no per-machine choice literals.
                for machine_id in option.candidate_machine_ids:
                    machine_intervals[machine_id].append(
                        model.NewOptionalIntervalVar(
                            option.start_seconds,
                            option.end_seconds-option.start_seconds,
                            option.end_seconds, lit,
                            f"hold_exact_{key}_{option.option_id}_{machine_id}"))
                    optional_intervals += 1
            else:
                interval = model.NewOptionalIntervalVar(
                    option.start_seconds,
                    option.end_seconds-option.start_seconds,
                    option.end_seconds, lit, f"hold_{key}_{option.option_id}")
                for machine_id in incumbent_machines:
                    machine_intervals[machine_id].append(interval)
                optional_intervals += 1
            # Worker segments come from the same authoritative allocator used
            # by the final realization.  They are a preview envelope, not a
            # worker identity decision; CP uses them to avoid choosing local
            # options whose crews overlap each other or frozen reservations.
            for segment_no, (left, right) in enumerate(option.preview_segments):
                seg_start = int(round((left-problem.origin).total_seconds()))
                seg_size = int(round((right-left).total_seconds()))
                if seg_size <= 0:
                    continue
                segment = model.NewOptionalFixedSizeIntervalVar(
                    seg_start, seg_size, lit,
                    f"worker_{key}_{option.option_id}_{segment_no}")
                for employee in option.preview_worker_ids:
                    worker_intervals[str(employee)].append(segment)
                optional_intervals += 1
        model.AddExactlyOne(timing_lits)
        starts[key] = model.NewIntVar(
            min(v.start_seconds for v in candidate_options),
            max(v.start_seconds for v in candidate_options), f"start_{key}")
        line_vars[key] = model.NewIntVar(
            min(v.line_id for v in candidate_options),
            max(v.line_id for v in candidate_options), f"line_{key}")
        k_vars[key] = model.NewIntVar(
            min(v.machine_count for v in candidate_options),
            max(v.machine_count for v in candidate_options), f"k_{key}")
        model.Add(line_vars[key] == sum(v.line_id*option_literals[(key, v.option_id)]
                                        for v in candidate_options))
        model.Add(k_vars[key] == sum(v.machine_count*option_literals[(key, v.option_id)]
                                     for v in candidate_options))
        productive_starts[key] = model.NewIntVar(
            min(v.productive_start_seconds for v in candidate_options),
            max(v.productive_start_seconds for v in candidate_options),
            f"productive_start_{key}")
        ends[key] = model.NewIntVar(
            min(v.end_seconds for v in candidate_options),
            max(v.end_seconds for v in candidate_options), f"end_{key}")
        model.Add(starts[key] == sum(v.start_seconds*option_literals[(key, v.option_id)] for v in candidate_options))
        model.Add(productive_starts[key] == sum(v.productive_start_seconds*option_literals[(key, v.option_id)] for v in candidate_options))
        model.Add(ends[key] == sum(v.end_seconds*option_literals[(key, v.option_id)] for v in candidate_options))

    # Frozen machine boundaries and exact local candidate-machine capacity.
    for machine_id, intervals in sorted(machine_intervals.items()):
        for number, (left, right, _order, _key) in enumerate(machine_res.get(machine_id, ())):
            intervals.append(model.NewFixedSizeIntervalVar(left, right-left,
                f"outside_machine_{machine_id}_{number}"))
            fixed_boundary += 1
        model.AddNoOverlap(intervals)
    for employee, intervals in sorted(worker_intervals.items()):
        for number, (left, right) in enumerate(worker_res.get(employee, ())):
            start = int(round((left-problem.origin).total_seconds()))
            size = int(round((right-left).total_seconds()))
            intervals.append(model.NewFixedSizeIntervalVar(
                start, size, f"outside_worker_{employee}_{number}"))
            fixed_boundary += 1
        model.AddNoOverlap(intervals)

    # Pairwise transition exclusions for fixed option envelopes.  This keeps
    # local machine identity exact without expanding the global problem.
    exact_conflicts = 0
    keys = sorted(mutable)
    for left_no, left in enumerate(keys):
        left_order = problem.operation_by_key[left].order_id
        for right in keys[left_no+1:]:
            right_order = problem.operation_by_key[right].order_id
            for lo in options[left]:
                for ro in options[right]:
                    shared = set(lo.candidate_machine_ids) & set(ro.candidate_machine_ids)
                    if not shared:
                        continue
                    compatible = False
                    if lo.end_seconds <= ro.start_seconds:
                        compatible = lo.end_seconds + authoritative_changeover_seconds(left_order, right_order) <= ro.start_seconds
                    elif ro.end_seconds <= lo.start_seconds:
                        compatible = ro.end_seconds + authoritative_changeover_seconds(right_order, left_order) <= lo.start_seconds
                    if compatible:
                        continue
                    for machine_id in shared:
                        ll = machine_literals.get((left, lo.option_id, machine_id), option_literals[(left, lo.option_id)])
                        rr = machine_literals.get((right, ro.option_id, machine_id), option_literals[(right, ro.option_id)])
                        model.Add(ll + rr <= 1)
                        exact_conflicts += 1

    # Routing constraints inside the local region; outside boundaries were
    # filtered into each operation's option domain.
    for left, right in index.route_successor.items():
        if left in mutable and right in mutable:
            model.Add(ends[left] <= starts[right])

    tardiness = []
    for order_id in neighborhood.order_ids:
        final = max(index.order_operations[order_id],
                    key=lambda key: problem.operation_by_key[key].sequence)
        if final not in mutable:
            continue
        due = problem.order_by_id[order_id].deadline_seconds
        if due is None:
            continue
        value = model.NewIntVar(0, problem.horizon_seconds, f"tardy_{order_id}")
        model.Add(value >= ends[final] - max(0, due))
        tardiness.append(value)
    max_end = model.NewIntVar(0, problem.horizon_seconds, "local_max_end")
    model.AddMaxEquality(max_end, list(ends.values()))
    model.Minimize((problem.horizon_seconds+1)*sum(tardiness) + max_end)
    proto = model.Proto()
    size = LineKModelSize(
        operations=len(mutable), released_operations=len(released),
        fixed_mode_operations=len(mutable-released),
        options=sum(len(v) for v in options.values()),
        variables=len(proto.variables), constraints=len(proto.constraints),
        optional_intervals=optional_intervals,
        exact_machine_boolean_variables=exact_machine_bools,
        exact_machine_conflict_constraints=exact_conflicts,
        fixed_boundary_intervals=fixed_boundary,
        build_time_seconds=time.perf_counter()-started,
        rss_increase_mb=max(0.0, _rss_mb()-rss_before),
    )
    return LineKArtifacts(
        model, options, option_literals, machine_literals, starts,
        line_vars, k_vars, productive_starts, ends, size,
        {"rejected_options": rejected, "released_keys": sorted(released),
         "exact_machine_sets": bool(exact_machine_sets)},
    )


def solve_line_k(artifacts, *, seed=42, seconds_limit=30.0, log_path=None):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds_limit)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = int(seed)
    solver.parameters.log_search_progress = True
    logs = []
    solver.log_callback = logs.append
    callback = FirstSolution()
    status = solver.Solve(artifacts.model, callback)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("".join(logs), encoding="utf-8")
    selected = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        selected = {}
        for key, values in artifacts.options.items():
            option = next(value for value in values if solver.Value(
                artifacts.option_literals[(key, value.option_id)]))
            machines = []
            if key in set(artifacts.diagnostics["released_keys"]):
                if artifacts.diagnostics.get("exact_machine_sets"):
                    machines = list(option.candidate_machine_ids)
                else:
                    for machine_id in option.candidate_machine_ids:
                        literal = artifacts.machine_literals[(key, option.option_id, machine_id)]
                        if solver.Value(literal):
                            machines.append(int(machine_id))
            else:
                machines = list(option.candidate_machine_ids)
            selected[key] = {
                "line_id": int(option.line_id),
                "machine_count": int(option.machine_count),
                "start_seconds": int(option.start_seconds),
                "productive_start_seconds": int(option.productive_start_seconds),
                "end_seconds": int(option.end_seconds),
                "productive_seconds": int(option.productive_seconds),
                "setup_seconds": int(option.setup_seconds),
                "machine_ids": tuple(sorted(machines)),
                "preview_worker_ids": option.preview_worker_ids,
                "preview_segments": option.preview_segments,
                "option_id": option.option_id,
            }
    response = solver.ResponseProto()
    return {
        "status": solver.StatusName(status),
        "wall_time_seconds": solver.WallTime(),
        "deterministic_time": getattr(response, "deterministic_time", None),
        "first_incumbent_seconds": callback.first,
        "solution_count": callback.count,
        "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
        "objective": solver.ObjectiveValue() if selected is not None else None,
        "best_bound": solver.BestObjectiveBound() if selected is not None else None,
        "model_size": asdict(artifacts.size),
    }, selected
