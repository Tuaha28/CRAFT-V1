"""Genuinely local V2.1-style Tier-1 CP-SAT repair model.

Only mutable operations receive variables. The incumbent outside state enters
as fixed resource occupation and routing boundary constraints.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import os
import time

import psutil
from ortools.sat.python import cp_model

from crafter.authoritative_semantics import (
    authoritative_productive_seconds, authoritative_setup_seconds,
)
from crafter.model_v2_1 import _machine_classes
from .analysis import seconds


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss/1024**2


@dataclass(frozen=True)
class LocalModelSize:
    operations: int
    variables: int
    constraints: int
    optional_intervals: int
    mandatory_intervals: int
    cumulative_constraints: int
    class_allocation_variables: int
    outside_fixed_intervals: int
    build_time_seconds: float
    rss_increase_mb: float


@dataclass
class LocalArtifacts:
    model: cp_model.CpModel
    starts: dict
    productive_starts: dict
    ends: dict
    class_allocations: dict
    elapsed: dict
    size: LocalModelSize
    bounds: dict
    tier: int = 1


def _proto_counts(model):
    proto = model.Proto()
    optional = sum(1 for constraint in proto.constraints
                   if constraint.has_interval() and
                   constraint.enforcement_literal)
    mandatory = sum(1 for constraint in proto.constraints
                    if constraint.has_interval() and
                    not constraint.enforcement_literal)
    cumulative = sum(1 for constraint in proto.constraints
                     if constraint.has_cumulative())
    return len(proto.variables), len(proto.constraints), optional, mandatory, cumulative


def _resource_capacities(problem):
    line = Counter()
    capability = Counter()
    for machine in problem.machines:
        if not machine.is_usable:
            continue
        line[machine.line_id] += 1
        for item in set(x.strip().lower() for x in machine.capabilities):
            capability[(machine.line_id, item)] += 1
    return line, capability


def build_tier1_local_model(problem, op_details, index, neighborhood):
    started, rss_before = time.perf_counter(), _rss_mb()
    model = cp_model.CpModel()
    mutable = set(neighborhood.operation_keys)
    starts, productive_starts, ends, elapsed = {}, {}, {}, {}
    bounds = {}
    operation_intervals = {}
    line_intervals, line_demands = defaultdict(list), defaultdict(list)
    cap_intervals, cap_demands = defaultdict(list), defaultdict(list)
    class_intervals, class_demands = defaultdict(list), defaultdict(list)
    class_allocations = {}
    _members, machine_class, class_sizes, class_caps = _machine_classes(problem)
    line_capacity, capability_capacity = _resource_capacities(problem)

    # Bounds are established only from immutable route boundaries. Operations
    # inside the neighborhood remain free to resequence on shared resources.
    for key in sorted(mutable):
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        incumbent_start = seconds(row["start_minutes"])
        incumbent_end = seconds(row["end_minutes"])
        duration = incumbent_end-incumbent_start
        setup = authoritative_setup_seconds(op_details[key])
        productive = authoritative_productive_seconds(
            op_details[key], len(set(int(x) for x in row["machine_ids"])))
        lower = 0
        predecessor = index.route_predecessor.get(key)
        if predecessor and predecessor not in mutable:
            lower = max(lower, seconds(index.by_key[predecessor]["end_minutes"]))
        if not predecessor:
            lower = max(lower, problem.order_by_id[operation.order_id].release_seconds)
        upper_end = problem.horizon_seconds
        successor = index.route_successor.get(key)
        if successor and successor not in mutable:
            upper_end = min(upper_end,
                            seconds(index.by_key[successor]["start_minutes"]))
        upper_start = upper_end-duration
        if upper_start < lower:
            raise ValueError(f"empty local boundary domain for {key}")
        start = model.NewIntVar(int(lower), int(upper_start), f"local_start_{key}")
        product_start = model.NewIntVar(
            int(lower+setup), int(upper_start+setup), f"local_productive_{key}")
        end = model.NewIntVar(int(lower+duration), int(upper_end), f"local_end_{key}")
        model.Add(product_start == start+setup)
        model.Add(end == start+duration)
        model.Add(end >= product_start+productive)
        interval = model.NewIntervalVar(start, duration, end, f"local_hold_{key}")
        starts[key], productive_starts[key], ends[key] = start, product_start, end
        elapsed[key] = duration
        bounds[key] = {"start_lb": lower, "start_ub": upper_start,
                       "end_ub": upper_end, "elapsed": duration,
                       "setup": setup, "productive": productive}
        operation_intervals[key] = interval
        line_id = int(row["line_id"])
        k = len(set(int(x) for x in row["machine_ids"]))
        line_intervals[line_id].append(interval)
        line_demands[line_id].append(k)
        capability = operation.required_capability.strip().lower()
        cap_intervals[(line_id, capability)].append(interval)
        cap_demands[(line_id, capability)].append(k)
        candidate_classes = sorted({
            machine_class[machine.machine_id]
            for machine in problem.machines
            if machine.is_usable and machine.line_id == line_id and
            machine.supports(capability)
        })
        allocations = []
        for class_id in candidate_classes:
            alloc = model.NewIntVar(
                0, min(k, class_sizes[class_id]), f"local_class_{key}_{class_id}")
            class_allocations[(key, class_id)] = alloc
            allocations.append(alloc)
            class_intervals[class_id].append(interval)
            class_demands[class_id].append(alloc)
        if not allocations:
            raise ValueError(f"no machine class for {key}")
        model.Add(sum(allocations) == k)

    # Internal technological precedence.
    for left, right in index.route_successor.items():
        if left in mutable and right in mutable:
            model.Add(ends[left] <= starts[right])

    # Outside occupation only in the range where a mutable operation can run.
    range_start = min(item["start_lb"] for item in bounds.values())
    range_end = max(item["end_ub"] for item in bounds.values())
    outside_fixed_intervals = 0
    for row in index.rows:
        key = str(row["operation_key"])
        if key in mutable:
            continue
        start, end = seconds(row["start_minutes"]), seconds(row["end_minutes"])
        if start >= range_end or end <= range_start:
            continue
        duration = end-start
        line_id = int(row["line_id"])
        capability = problem.operation_by_key[key].required_capability.strip().lower()
        machine_ids = tuple(int(value) for value in row["machine_ids"])
        line_iv = model.NewFixedSizeIntervalVar(start, duration, f"outside_line_{key}")
        line_intervals[line_id].append(line_iv)
        line_demands[line_id].append(len(machine_ids))
        cap_iv = model.NewFixedSizeIntervalVar(start, duration, f"outside_cap_{key}")
        cap_intervals[(line_id, capability)].append(cap_iv)
        cap_demands[(line_id, capability)].append(len(machine_ids))
        class_count = Counter(machine_class[mid] for mid in machine_ids
                              if mid in machine_class)
        for class_id, demand in class_count.items():
            iv = model.NewFixedSizeIntervalVar(
                start, duration, f"outside_class_{key}_{class_id}")
            class_intervals[class_id].append(iv)
            class_demands[class_id].append(demand)
        outside_fixed_intervals += 2+len(class_count)

    for fixed_no, fixed in enumerate(problem.resources.fixed_machine_commitments):
        if fixed.start_seconds >= range_end or fixed.end_seconds <= range_start:
            continue
        machine = problem.machine_by_id[fixed.machine_id]
        duration = fixed.end_seconds-fixed.start_seconds
        line_iv = model.NewFixedSizeIntervalVar(
            fixed.start_seconds, duration, f"commit_line_{fixed_no}")
        line_intervals[machine.line_id].append(line_iv)
        line_demands[machine.line_id].append(1)
        for capability in set(x.strip().lower() for x in machine.capabilities):
            cap_iv = model.NewFixedSizeIntervalVar(
                fixed.start_seconds, duration, f"commit_cap_{fixed_no}_{capability}")
            cap_intervals[(machine.line_id, capability)].append(cap_iv)
            cap_demands[(machine.line_id, capability)].append(1)
        if fixed.machine_id in machine_class:
            class_id = machine_class[fixed.machine_id]
            class_iv = model.NewFixedSizeIntervalVar(
                fixed.start_seconds, duration, f"commit_class_{fixed_no}")
            class_intervals[class_id].append(class_iv)
            class_demands[class_id].append(1)
        outside_fixed_intervals += 2+len(machine.capabilities)

    for line_id, intervals in line_intervals.items():
        model.AddCumulative(intervals, line_demands[line_id], line_capacity[line_id])
    for pool, intervals in cap_intervals.items():
        model.AddCumulative(intervals, cap_demands[pool], capability_capacity[pool])
    for class_id, intervals in class_intervals.items():
        model.AddCumulative(intervals, class_demands[class_id], class_sizes[class_id])

    # Delivery-first local objective. A horizon+1 multiplier makes one second
    # of tardiness dominate the complete tie-break range.
    tardiness_terms = []
    for order_id in neighborhood.order_ids:
        final = max(index.order_operations[order_id],
                    key=lambda key: problem.operation_by_key[key].sequence)
        if final not in mutable:
            continue
        due = problem.order_by_id[order_id].deadline_seconds
        if due is None:
            continue
        tardy = model.NewIntVar(0, problem.horizon_seconds, f"local_tardy_{order_id}")
        model.Add(tardy >= ends[final]-max(0, due))
        tardiness_terms.append(tardy)
    tie = model.NewIntVar(0, problem.horizon_seconds, "local_max_end")
    model.AddMaxEquality(tie, list(ends.values()))
    model.Minimize((problem.horizon_seconds+1)*sum(tardiness_terms)+tie)

    variables, constraints, optional, mandatory, cumulative = _proto_counts(model)
    size = LocalModelSize(
        operations=len(mutable), variables=variables, constraints=constraints,
        optional_intervals=optional, mandatory_intervals=mandatory,
        cumulative_constraints=cumulative,
        class_allocation_variables=len(class_allocations),
        outside_fixed_intervals=outside_fixed_intervals,
        build_time_seconds=time.perf_counter()-started,
        rss_increase_mb=max(0.0, _rss_mb()-rss_before),
    )
    return LocalArtifacts(model, starts, productive_starts, ends,
                          class_allocations, elapsed, size, bounds)


class FirstSolution(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.first_wall_time = None
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        if self.first_wall_time is None:
            self.first_wall_time = self.WallTime()


def solve_local(artifacts, *, seed=42, time_limit=10.0, log_path=None):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = int(seed)
    solver.parameters.log_search_progress = True
    logs = []
    solver.log_callback = logs.append
    callback = FirstSolution()
    status = solver.Solve(artifacts.model, callback)
    if log_path is not None:
        log_path.write_text("".join(logs), encoding="utf-8")
    names = {value: name for name, value in vars(cp_model).items()
             if name.startswith("OPTIMAL") or name.startswith("FEASIBLE")}
    status_name = solver.StatusName(status)
    assignment = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignment = {
            key: {
                "start_seconds": solver.Value(artifacts.starts[key]),
                "productive_start_seconds": solver.Value(
                    artifacts.productive_starts[key]),
                "end_seconds": solver.Value(artifacts.ends[key]),
                "elapsed_seconds": artifacts.elapsed[key],
                "class_allocations": {
                    str(class_id): solver.Value(variable)
                    for (op_key, class_id), variable in artifacts.class_allocations.items()
                    if op_key == key and solver.Value(variable) > 0
                },
            }
            for key in artifacts.starts
        }
    response = solver.ResponseProto()
    result = {
        "status": status_name,
        "wall_time_seconds": solver.WallTime(),
        "deterministic_time": getattr(response, "deterministic_time", None),
        "first_incumbent_seconds": callback.first_wall_time,
        "solution_count": callback.count,
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "objective": solver.ObjectiveValue() if assignment else None,
        "best_bound": solver.BestObjectiveBound() if assignment else None,
        "entered_search": solver.NumBranches() > 0 or callback.count > 0,
        "model_size": asdict(artifacts.size),
    }
    return result, assignment
