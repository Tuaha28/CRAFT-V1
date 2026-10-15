"""E1.2 bounded model with selective exact physical-machine freedom.

F1 operations retain their incumbent machine IDs.  F2 operations choose
exactly k IDs from a small, deterministic, same-line candidate pool.  Worker
identities, line and k remain anchored for every operation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import os
import time

import psutil
from ortools.sat.python import cp_model

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds, authoritative_operation_work_minutes,
    authoritative_setup_seconds,
)
from .analysis import seconds
from .anchored_model import (
    FirstSolution, TimingOption, _candidate_starts, _crew_progress,
    machine_interval_feasible, outside_machine_reservations,
    outside_worker_reservations,
)


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss / 1024**2


@dataclass(frozen=True)
class SelectiveModelSize:
    operations: int
    f1_operations: int
    f2_operations: int
    timing_options: int
    variables: int
    constraints: int
    optional_intervals: int
    optional_machine_intervals: int
    optional_worker_segment_intervals: int
    fixed_boundary_intervals: int
    exact_machine_boolean_variables: int
    exact_machine_conflict_constraints: int
    build_time_seconds: float
    rss_increase_mb: float


@dataclass
class SelectiveArtifacts:
    model: cp_model.CpModel
    options: dict
    timing_literals: dict
    machine_literals: dict
    candidate_pools: dict
    freed_keys: tuple
    starts: dict
    ends: dict
    size: SelectiveModelSize
    diagnostics: dict


def build_machine_candidate_pools(problem, index, neighborhood,
                                  *, alternative_limit=5):
    """Return incumbent IDs plus bounded useful same-line exact alternatives."""
    mutable = set(neighborhood.operation_keys)
    reservations = outside_machine_reservations(problem, index, mutable)
    result, detail = {}, {}
    for key in sorted(mutable):
        op = problem.operation_by_key[key]
        row = index.by_key[key]
        incumbent = tuple(sorted({int(value) for value in row["machine_ids"]}))
        line_id = int(row["line_id"])
        factory_id = problem.order_by_id[op.order_id].factory_id
        alternatives = []
        for machine in problem.machines:
            if (machine.machine_id in incumbent or not machine.is_usable or
                    machine.factory_id != factory_id or
                    machine.line_id != line_id or
                    not machine.supports(op.required_capability)):
                continue
            rows = reservations.get(machine.machine_id, ())
            # Deterministic opportunity proxy: fewer frozen intervals first,
            # then smaller total frozen load and physical identity.
            frozen_load = sum(right-left for left, right, _order, _key in rows)
            alternatives.append((len(rows), frozen_load, machine.machine_id))
        alternatives.sort()
        chosen = tuple(value[2] for value in alternatives[:alternative_limit])
        pool = tuple(sorted(set(incumbent + chosen)))
        result[key] = pool
        detail[key] = {
            "incumbent_machine_ids": list(incumbent),
            "alternative_machine_ids": list(chosen),
            "candidate_machine_ids": list(pool),
            "eligible_same_line_alternative_count": len(alternatives),
            "candidate_pool_limit": alternative_limit,
            "factory_id": factory_id, "line_id": line_id,
            "capability": op.required_capability,
        }
    return result, detail


def _candidate_starts_with_pool(problem, index, neighborhood, key,
                                machine_res, worker_res, pool, maximum=20):
    base = set(_candidate_starts(
        problem, index, neighborhood, key, machine_res, worker_res,
        maximum=maximum))
    op = problem.operation_by_key[key]
    incumbent = seconds(index.by_key[key]["start_minutes"])
    for machine_id in pool:
        for _start, end, order_id, _other in machine_res.get(machine_id, ()):
            base.add(end + authoritative_changeover_seconds(order_id, op.order_id))
    low = max(0, problem.order_by_id[op.order_id].release_seconds)
    candidates = [value for value in base
                  if low <= value <= problem.horizon_seconds]
    candidates.sort(key=lambda value: (abs(value-incumbent), value))
    return sorted(set(candidates[:maximum]) | {incumbent})


def _compatible(left, right, left_order, right_order):
    if left.end_seconds <= right.start_seconds:
        return (left.end_seconds + authoritative_changeover_seconds(
            left_order, right_order) <= right.start_seconds)
    if right.end_seconds <= left.start_seconds:
        return (right.end_seconds + authoritative_changeover_seconds(
            right_order, left_order) <= left.start_seconds)
    return False


def build_selective_machine_model(problem, op_details, operator_index, index,
                                  neighborhood, freed_keys, candidate_pools):
    started, rss_before = time.perf_counter(), _rss_mb()
    model = cp_model.CpModel()
    mutable = set(neighborhood.operation_keys)
    freed = set(freed_keys)
    if not freed <= mutable:
        raise ValueError("F2 operations must belong to the neighborhood")
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = outside_worker_reservations(problem, index, mutable)
    options, timing_literals, machine_literals = {}, {}, {}
    starts, productive_starts, ends = {}, {}, {}
    machine_intervals, worker_intervals = defaultdict(list), defaultdict(list)
    rejected, exact_boolean_count = {}, 0
    optional_machine = optional_worker = fixed_boundary = 0

    for key in sorted(mutable):
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        incumbent_machines = tuple(sorted({int(v) for v in row["machine_ids"]}))
        workers = tuple(sorted(str(v) for v in row.get("employee_ids", ())))
        k = len(incumbent_machines)
        if len(workers) != k:
            raise ValueError(f"anchored worker/machine count mismatch for {key}")
        pool = tuple(candidate_pools[key] if key in freed else incumbent_machines)
        if not set(incumbent_machines) <= set(pool) or len(pool) < k:
            raise ValueError(f"candidate pool omits feasible incumbent for {key}")
        setup = authoritative_setup_seconds(op_details[key])
        _setup_minutes, productive_minutes = authoritative_operation_work_minutes(
            op_details[key], k)
        route_pred, route_succ = (index.route_predecessor.get(key),
                                  index.route_successor.get(key))
        earliest = (seconds(index.by_key[route_pred]["end_minutes"])
                    if route_pred and route_pred not in mutable else
                    max(0, problem.order_by_id[operation.order_id].release_seconds))
        latest_end = (seconds(index.by_key[route_succ]["start_minutes"])
                      if route_succ and route_succ not in mutable else
                      problem.horizon_seconds)
        candidate_options, failures = [], []
        if key in freed:
            starts_to_try = _candidate_starts_with_pool(
                problem, index, neighborhood, key, machine_res, worker_res, pool)
        else:
            starts_to_try = _candidate_starts(
                problem, index, neighborhood, key, machine_res, worker_res)
        for start in starts_to_try:
            productive_start = start + setup
            incumbent_start = seconds(row["start_minutes"])
            if start == incumbent_start:
                segment_map = row.get("worker_segments_by_employee", {})
                representative = segment_map.get(workers[0], ()) if workers else ()
                segments = tuple(
                    (datetime.fromisoformat(str(pair[0])),
                     datetime.fromisoformat(str(pair[1])))
                    if not isinstance(pair, dict) else
                    (datetime.fromisoformat(str(pair["start"])),
                     datetime.fromisoformat(str(pair["end"])))
                    for pair in representative)
                end = seconds(row["end_minutes"])
            else:
                try:
                    end, segments = _crew_progress(
                        problem, operator_index, workers, worker_res,
                        productive_start, productive_minutes)
                except Exception as exc:
                    failures.append({"start_seconds": start,
                                     "reason": f"crew calendar: {exc!r}"})
                    continue
            if start < earliest or end > latest_end:
                failures.append({"start_seconds": start, "end_seconds": end,
                                 "reason": "routing boundary"})
                continue
            feasible_machines = tuple(machine_id for machine_id in pool
                if machine_interval_feasible(machine_res[machine_id], start, end,
                                             operation.order_id))
            if len(feasible_machines) < k:
                failures.append({"start_seconds": start, "end_seconds": end,
                    "reason": "insufficient exact machines at outside boundary",
                    "required": k, "available": list(feasible_machines)})
                continue
            if key not in freed and set(feasible_machines) != set(incumbent_machines):
                # F1 must have every anchored ID feasible, not merely k IDs.
                failures.append({"start_seconds": start, "end_seconds": end,
                                 "reason": "anchored machine boundary"})
                continue
            option = TimingOption(
                key, len(candidate_options), start, productive_start, end,
                feasible_machines, workers, segments, start == incumbent_start)
            candidate_options.append(option)
        if not candidate_options:
            raise ValueError(f"no selective timing option for {key}: {failures[:5]}")
        options[key] = tuple(candidate_options)
        rejected[key] = failures
        op_timing = []
        for option in candidate_options:
            literal = model.NewBoolVar(f"timing_{key}_{option.option_id}")
            timing_literals[(key, option.option_id)] = literal
            op_timing.append(literal)
            duration = option.end_seconds-option.start_seconds
            if key in freed:
                machine_choices = []
                for machine_id in option.machine_ids:
                    use = model.NewBoolVar(
                        f"use_{key}_{option.option_id}_{machine_id}")
                    machine_literals[(key, option.option_id, machine_id)] = use
                    model.Add(use <= literal)
                    machine_choices.append(use)
                    machine_intervals[machine_id].append(
                        model.NewOptionalIntervalVar(
                            option.start_seconds, duration, option.end_seconds,
                            use, f"machine_{key}_{option.option_id}_{machine_id}"))
                    exact_boolean_count += 1; optional_machine += 1
                model.Add(sum(machine_choices) == k*literal)
            else:
                interval = model.NewOptionalIntervalVar(
                    option.start_seconds, duration, option.end_seconds, literal,
                    f"machine_{key}_{option.option_id}")
                for machine_id in incumbent_machines:
                    machine_intervals[machine_id].append(interval)
                optional_machine += 1
            for segment_no, (left, right) in enumerate(option.worker_segments):
                seg_start = int(round((left-problem.origin).total_seconds()))
                seg_size = int(round((right-left).total_seconds()))
                interval = model.NewOptionalFixedSizeIntervalVar(
                    seg_start, seg_size, literal,
                    f"worker_{key}_{option.option_id}_{segment_no}")
                for worker in workers:
                    worker_intervals[worker].append(interval)
                optional_worker += 1
        model.AddExactlyOne(op_timing)
        start_var = model.NewIntVar(
            min(v.start_seconds for v in candidate_options),
            max(v.start_seconds for v in candidate_options), f"start_{key}")
        productive_var = model.NewIntVar(
            min(v.productive_start_seconds for v in candidate_options),
            max(v.productive_start_seconds for v in candidate_options),
            f"productive_start_{key}")
        end_var = model.NewIntVar(
            min(v.end_seconds for v in candidate_options),
            max(v.end_seconds for v in candidate_options), f"end_{key}")
        model.Add(start_var == sum(v.start_seconds*timing_literals[(key, v.option_id)]
                                   for v in candidate_options))
        model.Add(productive_var == sum(
            v.productive_start_seconds*timing_literals[(key, v.option_id)]
            for v in candidate_options))
        model.Add(end_var == sum(v.end_seconds*timing_literals[(key, v.option_id)]
                                 for v in candidate_options))
        starts[key], productive_starts[key], ends[key] = (
            start_var, productive_var, end_var)

    # Frozen outside occupancy is explicitly represented for every resource
    # that the bounded model can touch.
    for machine_id in sorted(machine_intervals):
        intervals = machine_intervals[machine_id]
        for number, (left, right, _order, _key) in enumerate(
                machine_res.get(machine_id, ())):
            intervals.append(model.NewFixedSizeIntervalVar(
                left, right-left, f"outside_machine_{machine_id}_{number}"))
            fixed_boundary += 1
        model.AddNoOverlap(intervals)
    for worker in sorted(worker_intervals):
        intervals = worker_intervals[worker]
        for number, (left, right) in enumerate(worker_res.get(worker, ())):
            start = int(round((left-problem.origin).total_seconds()))
            size = int(round((right-left).total_seconds()))
            intervals.append(model.NewFixedSizeIntervalVar(
                start, size, f"outside_worker_{worker}_{number}"))
            fixed_boundary += 1
        model.AddNoOverlap(intervals)

    # Exact sequence-dependent gaps for every shared candidate physical ID.
    exact_conflicts = 0
    keys = sorted(mutable)
    for left_no, left in enumerate(keys):
        left_order = problem.operation_by_key[left].order_id
        for right in keys[left_no+1:]:
            right_order = problem.operation_by_key[right].order_id
            for left_opt in options[left]:
                for right_opt in options[right]:
                    if _compatible(left_opt, right_opt, left_order, right_order):
                        continue
                    shared = set(left_opt.machine_ids) & set(right_opt.machine_ids)
                    if shared and left not in freed and right not in freed:
                        # Both literals mean use of their complete incumbent
                        # sets. One incompatibility constraint is sufficient,
                        # regardless of how many IDs the sets share.
                        model.Add(
                            timing_literals[(left, left_opt.option_id)] +
                            timing_literals[(right, right_opt.option_id)] <= 1)
                        exact_conflicts += 1
                        continue
                    for machine_id in shared:
                        left_lit = machine_literals.get(
                            (left, left_opt.option_id, machine_id))
                        if left_lit is None:
                            left_lit = timing_literals[(left, left_opt.option_id)]
                        right_lit = machine_literals.get(
                            (right, right_opt.option_id, machine_id))
                        if right_lit is None:
                            right_lit = timing_literals[(right, right_opt.option_id)]
                        model.Add(left_lit+right_lit <= 1)
                        exact_conflicts += 1

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
        model.Add(value >= ends[final]-max(0, due))
        tardiness.append(value)
    max_end = model.NewIntVar(0, problem.horizon_seconds, "local_max_end")
    model.AddMaxEquality(max_end, list(ends.values()))
    model.Minimize((problem.horizon_seconds+1)*sum(tardiness)+max_end)

    proto = model.Proto()
    size = SelectiveModelSize(
        operations=len(mutable), f1_operations=len(mutable-freed),
        f2_operations=len(freed),
        timing_options=sum(len(value) for value in options.values()),
        variables=len(proto.variables), constraints=len(proto.constraints),
        optional_intervals=optional_machine+optional_worker,
        optional_machine_intervals=optional_machine,
        optional_worker_segment_intervals=optional_worker,
        fixed_boundary_intervals=fixed_boundary,
        exact_machine_boolean_variables=exact_boolean_count,
        exact_machine_conflict_constraints=exact_conflicts,
        build_time_seconds=time.perf_counter()-started,
        rss_increase_mb=max(0.0, _rss_mb()-rss_before),
    )
    return SelectiveArtifacts(
        model=model, options=options, timing_literals=timing_literals,
        machine_literals=machine_literals,
        candidate_pools={key: tuple(candidate_pools[key]) for key in mutable},
        freed_keys=tuple(sorted(freed)), starts=starts, ends=ends, size=size,
        diagnostics={"rejected_timing_options": rejected})


def solve_selective(artifacts, *, seed=42, seconds_limit=5.0, log_path=None):
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
                artifacts.timing_literals[(key, value.option_id)]))
            if key in artifacts.freed_keys:
                machines = tuple(sorted(machine_id for machine_id in option.machine_ids
                    if solver.Value(artifacts.machine_literals[(
                        key, option.option_id, machine_id)])))
            else:
                # F1 option.machine_ids is the anchored incumbent set; the
                # full pool is retained only for audit/fairness reporting.
                machines = tuple(sorted(option.machine_ids))
            selected[key] = {"option": option, "machine_ids": machines}
    response = solver.ResponseProto()
    return {
        "status": solver.StatusName(status),
        "wall_time_seconds": solver.WallTime(),
        "deterministic_time": getattr(response, "deterministic_time", None),
        "first_incumbent_seconds": callback.first,
        "solution_count": callback.count,
        "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
        "objective": solver.ObjectiveValue() if selected else None,
        "best_bound": solver.BestObjectiveBound() if selected else None,
        "model_size": asdict(artifacts.size),
    }, selected
