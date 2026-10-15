"""Resource-anchored E1.1 local model.

Each timing option is materialized before CP construction against the exact
incumbent machine IDs and exact incumbent fixed crew. CP selects only among
options with authoritative worker segments and machine boundaries already
known to be feasible against the frozen outside schedule.
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
    authoritative_changeover_seconds, authoritative_operation_work_minutes,
    authoritative_setup_seconds, walk_fixed_crew,
)
from ga.worker_allocation import operator_available_at
from .analysis import seconds


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss/1024**2


def _free(intervals, when):
    return all(not (start <= when < end) for start, end in intervals)


@dataclass(frozen=True)
class TimingOption:
    operation_key: str
    option_id: int
    start_seconds: int
    productive_start_seconds: int
    end_seconds: int
    machine_ids: tuple[int, ...]
    employee_ids: tuple[str, ...]
    worker_segments: tuple
    is_incumbent_start: bool


@dataclass(frozen=True)
class AnchoredModelSize:
    operations: int
    timing_options: int
    variables: int
    constraints: int
    optional_machine_intervals: int
    optional_worker_segment_intervals: int
    fixed_boundary_intervals: int
    build_time_seconds: float
    rss_increase_mb: float


@dataclass
class AnchoredArtifacts:
    model: cp_model.CpModel
    options: dict
    literals: dict
    starts: dict
    productive_starts: dict
    ends: dict
    size: AnchoredModelSize
    machine_boundaries: dict
    worker_boundaries: dict


def outside_worker_reservations(problem, index, mutable):
    result = defaultdict(list)
    for row in index.rows:
        if str(row["operation_key"]) in mutable:
            continue
        for employee, segments in row.get("worker_segments_by_employee", {}).items():
            for pair in segments:
                start, end = pair if not isinstance(pair, dict) else (
                    pair["start"], pair["end"])
                from datetime import datetime
                result[str(employee)].append((
                    datetime.fromisoformat(str(start)),
                    datetime.fromisoformat(str(end))))
    for fixed in problem.resources.fixed_worker_commitments:
        result[str(fixed.employee_id)].append((
            problem.origin+timedelta(seconds=fixed.start_seconds),
            problem.origin+timedelta(seconds=fixed.end_seconds)))
    for intervals in result.values():
        intervals.sort()
    return result


def outside_machine_reservations(problem, index, mutable):
    result = defaultdict(list)
    for row in index.rows:
        key = str(row["operation_key"])
        if key in mutable:
            continue
        for machine_id in row["machine_ids"]:
            result[int(machine_id)].append((
                seconds(row["start_minutes"]), seconds(row["end_minutes"]),
                str(row["production_order_id"]), key))
    for fixed in problem.resources.fixed_machine_commitments:
        result[int(fixed.machine_id)].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            str(fixed.production_order_id), f"fixed:{fixed.production_order_id}"))
    for intervals in result.values():
        intervals.sort()
    return result


def machine_interval_feasible(intervals, start, end, order_id):
    for left, right, other_order, _key in intervals:
        if start < right and end > left:
            return False
        if right <= start and right+authoritative_changeover_seconds(
                other_order, order_id) > start:
            return False
        if end <= left and end+authoritative_changeover_seconds(
                order_id, other_order) > left:
            return False
    return True


def _crew_progress(problem, operator_index, crew, reservations,
                   productive_ready_seconds, productive_minutes):
    ready = problem.origin+timedelta(seconds=int(productive_ready_seconds))
    crew = tuple(str(value) for value in crew)

    def available(when):
        return all(
            operator_available_at(
                operator_index, int(employee) if employee.isdigit() else employee,
                when)
            and _free(reservations.get(employee, ()), when)
            for employee in crew)

    def next_change(when):
        values = []
        for employee in crew:
            for start, end in reservations.get(employee, ()):
                if start > when:
                    values.append(start)
                if end > when:
                    values.append(end)
        return min(values) if values else None

    _start, end, segments = walk_fixed_crew(
        ready, productive_minutes, available,
        next_availability_change=next_change)
    return int(round((end-problem.origin).total_seconds())), tuple(segments)


def _candidate_starts(problem, index, neighborhood, key, machine_res, worker_res,
                      *, maximum=16):
    row = index.by_key[key]
    operation = problem.operation_by_key[key]
    incumbent = seconds(row["start_minutes"])
    setup = int(round(float(row["setup_minutes"])*60))
    values = {incumbent}
    predecessor = index.route_predecessor.get(key)
    if predecessor and predecessor not in neighborhood.operation_keys:
        values.add(seconds(index.by_key[predecessor]["end_minutes"]))
    elif not predecessor:
        values.add(max(0, problem.order_by_id[operation.order_id].release_seconds))
    for machine_id in row["machine_ids"]:
        for _start, end, order_id, _other in machine_res[int(machine_id)]:
            values.add(end+authoritative_changeover_seconds(
                order_id, operation.order_id))
    for employee in row.get("employee_ids", ()):
        for _start, end in worker_res.get(str(employee), ()):
            values.add(max(0, int(round(
                (end-problem.origin).total_seconds()))-setup))
    # Incumbent starts of coupled mutable operations permit exact local swaps.
    for other in neighborhood.operation_keys:
        values.add(seconds(index.by_key[other]["start_minutes"]))
    low = max(0, problem.order_by_id[operation.order_id].release_seconds)
    filtered = sorted(value for value in values
                      if low <= value <= problem.horizon_seconds)
    # Retain the incumbent and the closest earlier/later event boundaries.
    filtered.sort(key=lambda value: (abs(value-incumbent), value))
    selected = sorted(set(filtered[:maximum]) | {incumbent})
    return selected


def build_resource_anchored_model(problem, op_details, operator_index, index,
                                  neighborhood, *, diagnostic_disable=()):
    started, rss_before = time.perf_counter(), _rss_mb()
    model = cp_model.CpModel()
    mutable = set(neighborhood.operation_keys)
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = outside_worker_reservations(problem, index, mutable)
    options, literals = {}, {}
    starts, productive_starts, ends = {}, {}, {}
    machine_intervals = defaultdict(list)
    worker_intervals = defaultdict(list)
    machine_boundaries, worker_boundaries = {}, {}
    optional_machine, optional_worker, fixed_boundary = 0, 0, 0

    for key in sorted(mutable):
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        machines = tuple(sorted(int(value) for value in row["machine_ids"]))
        workers = tuple(sorted(str(value) for value in row.get("employee_ids", ())))
        if len(machines) != len(workers):
            raise ValueError(f"anchored machine/worker count mismatch for {key}")
        setup = authoritative_setup_seconds(op_details[key])
        _setup_minutes, productive_minutes = authoritative_operation_work_minutes(
            op_details[key], len(machines))
        route_pred = index.route_predecessor.get(key)
        route_succ = index.route_successor.get(key)
        earliest = (seconds(index.by_key[route_pred]["end_minutes"])
                    if route_pred and route_pred not in mutable else
                    max(0, problem.order_by_id[operation.order_id].release_seconds))
        latest_end = (seconds(index.by_key[route_succ]["start_minutes"])
                      if route_succ and route_succ not in mutable else
                      problem.horizon_seconds)
        candidate_options = []
        failures = []
        for start in _candidate_starts(
                problem, index, neighborhood, key, machine_res, worker_res):
            productive_start = start+setup
            incumbent_start = seconds(row["start_minutes"])
            if start == incumbent_start:
                # This option is an authoritative, jointly allocated fixed-
                # crew execution. Recomputing it independently after removing
                # all mutable reservations can pull it into another mutable
                # operation's segment and destroy the guaranteed incumbent.
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
            bad_machine = next((machine_id for machine_id in machines
                                if not machine_interval_feasible(
                                    machine_res[machine_id], start, end,
                                    operation.order_id)), None)
            if bad_machine is not None:
                failures.append({"start_seconds": start, "end_seconds": end,
                                 "machine_id": bad_machine,
                                 "reason": "outside machine/changeover boundary"})
                continue
            option = TimingOption(
                key, len(candidate_options), start, productive_start, end,
                machines, workers, segments,
                start == incumbent_start)
            candidate_options.append(option)
        if not candidate_options:
            raise ValueError(f"no anchored timing option for {key}: {failures[:3]}")
        options[key] = tuple(candidate_options)
        op_literals = []
        for option in candidate_options:
            literal = model.NewBoolVar(f"anchored_{key}_{option.option_id}")
            literals[(key, option.option_id)] = literal
            op_literals.append(literal)
            duration = option.end_seconds-option.start_seconds
            interval = model.NewOptionalIntervalVar(
                option.start_seconds, duration, option.end_seconds, literal,
                f"machine_hold_{key}_{option.option_id}")
            for machine_id in machines:
                machine_intervals[machine_id].append(interval)
            optional_machine += 1
            for segment_no, (left, right) in enumerate(option.worker_segments):
                seg_start = int(round((left-problem.origin).total_seconds()))
                seg_size = int(round((right-left).total_seconds()))
                segment_interval = model.NewOptionalFixedSizeIntervalVar(
                    seg_start, seg_size, literal,
                    f"worker_{key}_{option.option_id}_{segment_no}")
                for employee in workers:
                    worker_intervals[employee].append(segment_interval)
                optional_worker += 1
        model.AddExactlyOne(op_literals)
        start_var = model.NewIntVar(
            min(item.start_seconds for item in candidate_options),
            max(item.start_seconds for item in candidate_options), f"start_{key}")
        productive_var = model.NewIntVar(
            min(item.productive_start_seconds for item in candidate_options),
            max(item.productive_start_seconds for item in candidate_options),
            f"productive_start_{key}")
        end_var = model.NewIntVar(
            min(item.end_seconds for item in candidate_options),
            max(item.end_seconds for item in candidate_options), f"end_{key}")
        model.Add(start_var == sum(item.start_seconds*literals[(key, item.option_id)]
                                   for item in candidate_options))
        model.Add(productive_var == sum(
            item.productive_start_seconds*literals[(key, item.option_id)]
            for item in candidate_options))
        model.Add(end_var == sum(item.end_seconds*literals[(key, item.option_id)]
                                 for item in candidate_options))
        starts[key], productive_starts[key], ends[key] = (
            start_var, productive_var, end_var)
        machine_boundaries[key] = {
            "machine_ids": list(machines),
            "outside_intervals": {
                str(mid): [list(value) for value in machine_res[mid]]
                for mid in machines},
            "rejected_options": failures,
        }
        worker_boundaries[key] = {
            "employee_ids": list(workers),
            "outside_reservation_counts": {
                employee: len(worker_res.get(employee, ())) for employee in workers},
            "feasible_timing_options": len(candidate_options),
        }

    # Frozen outside intervals are explicit boundaries on anchored resources.
    disabled = set(diagnostic_disable)
    for machine_id in sorted(machine_intervals):
        intervals = machine_intervals[machine_id]
        for number, (start, end, _order, _key) in enumerate(machine_res[machine_id]):
            intervals.append(model.NewFixedSizeIntervalVar(
                start, end-start, f"outside_machine_{machine_id}_{number}"))
            fixed_boundary += 1
        if "machine_no_overlap" not in disabled:
            model.AddNoOverlap(intervals)
    for employee in sorted(worker_intervals):
        intervals = worker_intervals[employee]
        for number, (left, right) in enumerate(worker_res.get(employee, ())):
            start = int(round((left-problem.origin).total_seconds()))
            size = int(round((right-left).total_seconds()))
            intervals.append(model.NewFixedSizeIntervalVar(
                start, size, f"outside_worker_{employee}_{number}"))
            fixed_boundary += 1
        if "worker_no_overlap" not in disabled:
            model.AddNoOverlap(intervals)

    # Exact local order-switch gaps on shared anchored physical machines.
    keys = sorted(mutable)
    for left_no, left in enumerate(keys):
        left_order = problem.operation_by_key[left].order_id
        left_machines = set(options[left][0].machine_ids)
        for right in keys[left_no+1:]:
            if not left_machines.intersection(options[right][0].machine_ids):
                continue
            right_order = problem.operation_by_key[right].order_id
            for left_option in options[left]:
                for right_option in options[right]:
                    if left_option.end_seconds <= right_option.start_seconds:
                        compatible = (left_option.end_seconds+
                            authoritative_changeover_seconds(
                                left_order, right_order)
                            <= right_option.start_seconds)
                    elif right_option.end_seconds <= left_option.start_seconds:
                        compatible = (right_option.end_seconds+
                            authoritative_changeover_seconds(
                                right_order, left_order)
                            <= left_option.start_seconds)
                    else:
                        compatible = False
                    if not compatible and "local_changeover" not in disabled:
                        model.Add(
                            literals[(left, left_option.option_id)]+
                            literals[(right, right_option.option_id)] <= 1)

    for left, right in index.route_successor.items():
        if (left in mutable and right in mutable and
                "routing" not in disabled):
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
    size = AnchoredModelSize(
        operations=len(mutable),
        timing_options=sum(len(value) for value in options.values()),
        variables=len(proto.variables), constraints=len(proto.constraints),
        optional_machine_intervals=optional_machine,
        optional_worker_segment_intervals=optional_worker,
        fixed_boundary_intervals=fixed_boundary,
        build_time_seconds=time.perf_counter()-started,
        rss_increase_mb=max(0.0, _rss_mb()-rss_before),
    )
    return AnchoredArtifacts(
        model, options, literals, starts, productive_starts, ends, size,
        machine_boundaries, worker_boundaries)


class FirstSolution(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.first = None
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        if self.first is None:
            self.first = self.WallTime()


def solve_anchored(artifacts, *, seed=42, seconds_limit=5.0, log_path=None):
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
        for key, options in artifacts.options.items():
            chosen = next(item for item in options
                          if solver.Value(artifacts.literals[(
                              key, item.option_id)]))
            selected[key] = chosen
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
