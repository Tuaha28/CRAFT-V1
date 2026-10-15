"""Compressed hierarchical CP-SAT master for isolated Crafter V2.

The master keeps operation timing, line choice, machine count, aggregate
line/capability capacity, and aggregate labour.  It deliberately does not
enumerate physical-machine identities.  Exact identities are a separate,
mandatory materialization gate in :mod:`crafter.machine_materializer`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import os
import time
from typing import Mapping

from ortools.sat.python import cp_model
import psutil

from .contracts import SchedulingProblem
from .labour_capacity import capacity_map, fixed_labour_by_pool
from .objective import ObjectiveWeights


COMPRESSED_HIERARCHICAL = "compressed_hierarchical_v2"


@dataclass(frozen=True)
class V2ModelSizeDiagnostics:
    formulation: str
    operation_count: int
    eligible_line_choices: int
    implicit_line_count_modes: int
    implicit_physical_candidates: int
    machine_equivalence_classes: int
    class_allocation_variables: int
    optional_intervals: int
    mandatory_intervals: int
    line_choice_variables: int
    machine_count_variables: int
    variables_by_category: Mapping[str, int]
    constraints_by_category: Mapping[str, int]
    total_variables: int
    total_constraints: int
    build_time_seconds: float
    rss_before_mb: float
    rss_after_mb: float


@dataclass
class V2ModelArtifacts:
    model: cp_model.CpModel
    starts: dict
    ends: dict
    productive_starts: dict
    productive_durations: dict
    durations: dict
    reserved_durations: dict
    release_ends: dict
    line_choices: dict
    line_literals: dict
    machine_counts: dict
    legal_line_bounds: dict
    tardiness: dict
    late_literals: dict
    makespan: object
    line_loads: dict
    load_imbalance: object
    optional_intervals: int
    size: V2ModelSizeDiagnostics
    formulation: str = COMPRESSED_HIERARCHICAL


class _Tracked:
    def __init__(self):
        self.model = cp_model.CpModel()
        self.variables = Counter()
        self.constraints = Counter()
        self.optional_intervals = 0
        self.mandatory_intervals = 0

    def int_var(self, category, lb, ub, name):
        self.variables[category] += 1
        return self.model.NewIntVar(int(lb), int(ub), name)

    def domain_var(self, category, values, name):
        self.variables[category] += 1
        return self.model.NewIntVarFromDomain(
            cp_model.Domain.FromValues([int(x) for x in values]), name)

    def bool_var(self, category, name):
        self.variables[category] += 1
        return self.model.NewBoolVar(name)

    def add(self, category, expression):
        self.constraints[category] += 1
        return self.model.Add(expression)

    def exactly_one(self, category, literals):
        self.constraints[category] += 1
        return self.model.AddExactlyOne(literals)

    def allowed(self, category, variables, tuples):
        self.constraints[category] += 1
        return self.model.AddAllowedAssignments(variables, tuples)

    def optional_interval(self, category, start, size, end, present, name):
        self.constraints[category] += 1
        self.optional_intervals += 1
        return self.model.NewOptionalIntervalVar(start, size, end, present, name)

    def interval(self, category, start, size, end, name):
        self.constraints[category] += 1
        self.mandatory_intervals += 1
        return self.model.NewIntervalVar(start, size, end, name)

    def fixed_interval(self, category, start, size, name):
        self.constraints[category] += 1
        self.mandatory_intervals += 1
        return self.model.NewFixedSizeIntervalVar(start, size, name)

    def cumulative(self, category, intervals, demands, capacity):
        self.constraints[category] += 1
        return self.model.AddCumulative(intervals, demands, int(capacity))

    def max_equality(self, category, target, expressions):
        self.constraints[category] += 1
        return self.model.AddMaxEquality(target, expressions)

    def min_equality(self, category, target, expressions):
        self.constraints[category] += 1
        return self.model.AddMinEquality(target, expressions)


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)


def _physical_candidates(problem, op, line_id):
    order = problem.order_by_id[op.order_id]
    return tuple(sorted(
        machine.machine_id for machine in problem.machines
        if machine.is_usable
        and machine.factory_id == order.factory_id
        and machine.line_id == line_id
        and machine.supports(op.required_capability)
    ))


def legal_line_bounds(problem, op, labour_caps):
    """Exact per-line count bounds without expanding ``(line, k)`` modes."""
    order = problem.order_by_id[op.order_id]
    pool = (order.factory_id, op.required_capability.strip().lower())
    if pool not in labour_caps:
        raise ValueError(f"missing aggregate labour capacity for {pool}")
    bounds = {}
    for line_id in op.eligible_line_ids:
        line = problem.line_by_id.get(line_id)
        if line is None or line.factory_id != order.factory_id:
            continue
        candidates = _physical_candidates(problem, op, line_id)
        upper = min(op.max_machine_count, len(candidates), labour_caps[pool])
        if upper >= op.min_machine_count:
            bounds[int(line_id)] = (int(op.min_machine_count), int(upper), candidates)
    return bounds


def build_model_v2(problem: SchedulingProblem,
                   weights: ObjectiveWeights = ObjectiveWeights()):
    started = time.perf_counter()
    rss_before = _rss_mb()
    tm = _Tracked()
    model = tm.model
    horizon = int(problem.horizon_seconds)
    buffer_seconds = int(problem.changeover_seconds)
    labour_caps = capacity_map(problem)

    starts, ends, productive_starts = {}, {}, {}
    productive_durations, durations = {}, {}
    reserved_durations, release_ends = {}, {}
    line_choices, line_literals, machine_counts = {}, {}, {}
    bounds_by_op = {}
    line_pool_intervals, line_pool_demands = defaultdict(list), defaultdict(list)
    capability_intervals, capability_demands = defaultdict(list), defaultdict(list)
    class_intervals, class_demands = defaultdict(list), defaultdict(list)
    labour_intervals, labour_demands = defaultdict(list), defaultdict(list)
    line_load_terms = defaultdict(list)
    eligible_line_choices = implicit_modes = implicit_candidates = 0
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            fixed.production_order_id,
            getattr(fixed, "required_capability", None)))
    class_members = defaultdict(list)
    for machine in problem.machines:
        if not machine.is_usable:
            continue
        class_key = (
            machine.factory_id, machine.line_id,
            tuple(sorted(x.strip().lower() for x in machine.capabilities)),
            tuple(sorted(fixed_by_machine.get(machine.machine_id, ()))),
        )
        class_members[class_key].append(machine.machine_id)
    class_keys = {
        key: index for index, key in enumerate(sorted(class_members, key=repr))
    }
    machine_class = {
        machine_id: class_keys[key]
        for key, ids in class_members.items() for machine_id in ids
    }
    class_capabilities = {
        class_keys[key]: set(key[2]) for key in class_members
    }
    class_sizes = {
        class_keys[key]: len(ids) for key, ids in class_members.items()
    }

    # Fixed machine occupation is represented once in the line-wide pool and
    # once in its exact capability pool.  The latter is inferred only when the
    # capability is known; otherwise all supported capabilities are reserved
    # conservatively.  Exact identity remains enforced by materialization.
    for fixed in problem.resources.fixed_machine_commitments:
        machine = problem.machine_by_id.get(fixed.machine_id)
        if machine is None:
            raise ValueError(f"unknown fixed machine {fixed.machine_id}")
        size = int(fixed.end_seconds - fixed.start_seconds + buffer_seconds)
        if size <= 0:
            raise ValueError("fixed machine commitment end must exceed start")
        total_iv = tm.fixed_interval(
            "fixed_machine_capacity", int(fixed.start_seconds), size,
            f"fixed_line_m{fixed.machine_id}_{fixed.start_seconds}")
        line_pool_intervals[machine.line_id].append(total_iv)
        line_pool_demands[machine.line_id].append(1)
        fixed_capability = getattr(fixed, "required_capability", None)
        capabilities = ((fixed_capability.strip().lower(),)
                        if fixed_capability else
                        tuple(x.strip().lower() for x in machine.capabilities))
        for capability in capabilities:
            iv = tm.fixed_interval(
                "fixed_machine_capacity", int(fixed.start_seconds), size,
                f"fixed_cap_m{fixed.machine_id}_{capability}_{fixed.start_seconds}")
            key = (machine.line_id, capability)
            capability_intervals[key].append(iv)
            capability_demands[key].append(1)
        class_id = machine_class.get(machine.machine_id)
        if class_id is not None:
            class_iv = tm.fixed_interval(
                "fixed_machine_capacity", int(fixed.start_seconds), size,
                f"fixed_class_{class_id}_m{fixed.machine_id}_{fixed.start_seconds}")
            class_intervals[class_id].append(class_iv)
            class_demands[class_id].append(1)

    for pool, rows in fixed_labour_by_pool(problem).items():
        if pool not in labour_caps:
            continue
        for fixed in rows:
            iv = tm.fixed_interval(
                "fixed_labour_capacity", int(fixed.start_seconds),
                int(fixed.end_seconds-fixed.start_seconds),
                f"fixed_w_{fixed.employee_id}_{fixed.start_seconds}")
            labour_intervals[pool].append(iv)
            labour_demands[pool].append(1)

    operations_by_order = defaultdict(list)
    max_load_by_op = {}
    for op in problem.operations:
        operations_by_order[op.order_id].append(op)
        bounds = legal_line_bounds(problem, op, labour_caps)
        if not bounds:
            raise ValueError(f"operation {op.operation_key} has no legal line/count domain")
        bounds_by_op[op.operation_key] = bounds
        eligible_line_choices += len(bounds)
        implicit_modes += sum(hi-lo+1 for lo, hi, _ in bounds.values())
        implicit_candidates += sum(len(ids) for _, _, ids in bounds.values())

        min_k = int(op.min_machine_count)
        max_k = max(hi for _lo, hi, _ids in bounds.values())
        min_productive = (op.serial_productive_seconds + max_k - 1) // max_k
        max_productive = (op.serial_productive_seconds + min_k - 1) // min_k
        start = tm.int_var("operation_time", 0, horizon, f"start_{op.operation_key}")
        productive_start = tm.int_var(
            "operation_time", 0, horizon, f"productive_start_{op.operation_key}")
        end = tm.int_var("operation_time", 0, horizon, f"end_{op.operation_key}")
        k = tm.int_var("machine_count", min_k, max_k, f"k_{op.operation_key}")
        productive = tm.int_var(
            "operation_time", min_productive, max_productive,
            f"productive_duration_{op.operation_key}")
        duration = tm.int_var(
            "operation_time", op.setup_seconds+min_productive,
            op.setup_seconds+max_productive, f"duration_{op.operation_key}")
        reserved = tm.int_var(
            "operation_time", op.setup_seconds+min_productive+buffer_seconds,
            op.setup_seconds+max_productive+buffer_seconds,
            f"reserved_duration_{op.operation_key}")
        release_end = tm.int_var(
            "operation_time", 0, horizon+buffer_seconds,
            f"machine_release_{op.operation_key}")
        load = tm.int_var(
            "load_balance", 0,
            max_k*(op.setup_seconds+max_productive),
            f"machine_load_{op.operation_key}")

        tuples = []
        for count in range(min_k, max_k+1):
            productive_seconds = (
                op.serial_productive_seconds + count - 1) // count
            duration_seconds = int(op.setup_seconds + productive_seconds)
            tuples.append((count, productive_seconds, duration_seconds,
                           count*duration_seconds))
        tm.allowed("duration_lookup", (k, productive, duration, load), tuples)
        tm.add("operation_timing", productive_start == start + int(op.setup_seconds))
        tm.add("operation_timing", end == start + duration)
        tm.add("operation_timing", reserved == duration + buffer_seconds)
        tm.add("operation_timing", release_end == end + buffer_seconds)

        line_ids = tuple(sorted(bounds))
        line_choice = tm.domain_var(
            "line_choice", line_ids, f"line_{op.operation_key}")
        literals = []
        for line_id in line_ids:
            lo, hi, _candidate_ids = bounds[line_id]
            lit = tm.bool_var("line_choice", f"on_line_{op.operation_key}_{line_id}")
            line_literals[(op.operation_key, line_id)] = lit
            literals.append(lit)
            tm.add("line_choice", line_choice == line_id).OnlyEnforceIf(lit)
            tm.add("line_count_domain", k >= lo).OnlyEnforceIf(lit)
            tm.add("line_count_domain", k <= hi).OnlyEnforceIf(lit)
            iv = tm.optional_interval(
                "line_pool_intervals", start, reserved, release_end, lit,
                f"pool_{op.operation_key}_{line_id}")
            line_pool_intervals[line_id].append(iv)
            line_pool_demands[line_id].append(k)
            cap_key = (line_id, op.required_capability.strip().lower())
            capability_intervals[cap_key].append(iv)
            capability_demands[cap_key].append(k)
            supporting_classes = tuple(sorted({
                machine_class[machine_id] for machine_id in _candidate_ids
                if machine_id in machine_class
                and op.required_capability.strip().lower()
                in class_capabilities[machine_class[machine_id]]
            }))
            allocations = []
            for class_id in supporting_classes:
                demand = tm.int_var(
                    "machine_class_allocation", 0,
                    min(hi, class_sizes[class_id]),
                    f"class_demand_{op.operation_key}_{line_id}_{class_id}")
                tm.add("machine_class_allocation", demand == 0).OnlyEnforceIf(lit.Not())
                class_intervals[class_id].append(iv)
                class_demands[class_id].append(demand)
                allocations.append(demand)
            if not allocations:
                raise ValueError(
                    f"operation {op.operation_key} line {line_id} has no machine class")
            tm.add("machine_class_allocation", sum(allocations) == k).OnlyEnforceIf(lit)
            load_here = tm.int_var(
                "load_balance", 0,
                max_k*(op.setup_seconds+max_productive),
                f"load_{op.operation_key}_{line_id}")
            tm.add("load_balance", load_here == load).OnlyEnforceIf(lit)
            tm.add("load_balance", load_here == 0).OnlyEnforceIf(lit.Not())
            line_load_terms[line_id].append(load_here)
        tm.exactly_one("line_choice", literals)

        order = problem.order_by_id[op.order_id]
        labour_pool = (order.factory_id, op.required_capability.strip().lower())
        labour_iv = tm.interval(
            "labour_intervals", productive_start, productive, end,
            f"labour_{op.operation_key}")
        labour_intervals[labour_pool].append(labour_iv)
        labour_demands[labour_pool].append(k)

        starts[op.operation_key], productive_starts[op.operation_key] = start, productive_start
        ends[op.operation_key], productive_durations[op.operation_key] = end, productive
        durations[op.operation_key], reserved_durations[op.operation_key] = duration, reserved
        release_ends[op.operation_key] = release_end
        line_choices[op.operation_key], machine_counts[op.operation_key] = line_choice, k
        max_load_by_op[op.operation_key] = max_k*(op.setup_seconds+max_productive)

    for order_id, chain in operations_by_order.items():
        chain.sort(key=lambda item: (item.sequence, item.operation_key))
        if len({item.sequence for item in chain}) != len(chain):
            raise ValueError(f"duplicate routing sequence in order {order_id}")
        order = problem.order_by_id[order_id]
        tm.add("release", starts[chain[0].operation_key] >= max(0, int(order.release_seconds)))
        for left, right in zip(chain, chain[1:]):
            tm.add("precedence", ends[left.operation_key] <= starts[right.operation_key])

    usable_by_line = Counter(
        machine.line_id for machine in problem.machines if machine.is_usable)
    usable_by_capability = Counter()
    for machine in problem.machines:
        if machine.is_usable:
            for capability in set(x.strip().lower() for x in machine.capabilities):
                usable_by_capability[(machine.line_id, capability)] += 1
    for line_id, intervals in line_pool_intervals.items():
        tm.cumulative("line_machine_capacity", intervals,
                      line_pool_demands[line_id], usable_by_line[line_id])
    for key, intervals in capability_intervals.items():
        tm.cumulative("capability_machine_capacity", intervals,
                      capability_demands[key], usable_by_capability[key])
    for class_id, intervals in class_intervals.items():
        tm.cumulative("machine_class_capacity", intervals,
                      class_demands[class_id], class_sizes[class_id])
    for pool, intervals in labour_intervals.items():
        tm.cumulative("labour_capacity", intervals,
                      labour_demands[pool], labour_caps[pool])

    tardiness, late_literals = {}, {}
    weighted_tardiness_terms, late_terms, completions = [], [], []
    for order_id, chain in operations_by_order.items():
        final = max(chain, key=lambda item: (item.sequence, item.operation_key))
        completion = ends[final.operation_key]
        completions.append(completion)
        order = problem.order_by_id[order_id]
        if order.deadline_seconds is None:
            continue
        due = max(0, int(order.deadline_seconds))
        tardy = tm.int_var("tardiness_late", 0, horizon, f"tardy_{order_id}")
        tm.max_equality("objective_auxiliaries", tardy, (0, completion-due))
        late = tm.bool_var("tardiness_late", f"late_{order_id}")
        tm.add("objective_auxiliaries", tardy >= 1).OnlyEnforceIf(late)
        tm.add("objective_auxiliaries", tardy == 0).OnlyEnforceIf(late.Not())
        tardiness[order_id], late_literals[order_id] = tardy, late
        weighted_tardiness_terms.append(int(order.priority_weight)*tardy)
        late_terms.append(late)

    makespan = tm.int_var("load_balance", 0, horizon, "makespan")
    tm.max_equality("objective_auxiliaries", makespan, completions)
    total_load_upper = max(1, sum(max_load_by_op.values()))
    line_loads = {}
    for line in problem.lines:
        load = tm.int_var("load_balance", 0, total_load_upper,
                          f"line_load_{line.line_id}")
        tm.add("objective_auxiliaries",
               load == sum(line_load_terms.get(line.line_id, ())))
        line_loads[line.line_id] = load
    max_load = tm.int_var("load_balance", 0, total_load_upper, "max_line_load")
    min_load = tm.int_var("load_balance", 0, total_load_upper, "min_line_load")
    tm.max_equality("objective_auxiliaries", max_load, tuple(line_loads.values()))
    tm.min_equality("objective_auxiliaries", min_load, tuple(line_loads.values()))
    load_imbalance = tm.int_var("load_balance", 0, total_load_upper, "load_imbalance")
    tm.add("objective_auxiliaries", load_imbalance == max_load-min_load)

    model.Minimize(
        weights.tardiness*sum(weighted_tardiness_terms)
        + weights.late_order*sum(late_terms)
        + weights.makespan*makespan
        + weights.load_imbalance*load_imbalance
    )

    proto = model.Proto()
    size = V2ModelSizeDiagnostics(
        formulation=COMPRESSED_HIERARCHICAL,
        operation_count=len(problem.operations),
        eligible_line_choices=eligible_line_choices,
        implicit_line_count_modes=implicit_modes,
        implicit_physical_candidates=implicit_candidates,
        machine_equivalence_classes=len(class_members),
        class_allocation_variables=int(tm.variables.get(
            "machine_class_allocation", 0)),
        optional_intervals=tm.optional_intervals,
        mandatory_intervals=tm.mandatory_intervals,
        line_choice_variables=len(problem.operations)+eligible_line_choices,
        machine_count_variables=len(problem.operations),
        variables_by_category=dict(sorted(tm.variables.items())),
        constraints_by_category=dict(sorted(tm.constraints.items())),
        total_variables=len(proto.variables), total_constraints=len(proto.constraints),
        build_time_seconds=time.perf_counter()-started,
        rss_before_mb=rss_before, rss_after_mb=_rss_mb(),
    )
    if sum(size.variables_by_category.values()) != size.total_variables:
        raise AssertionError("V2 variable attribution does not match proto")
    if sum(size.constraints_by_category.values()) != size.total_constraints:
        raise AssertionError("V2 constraint attribution does not match proto")
    return V2ModelArtifacts(
        model=model, starts=starts, ends=ends,
        productive_starts=productive_starts,
        productive_durations=productive_durations, durations=durations,
        reserved_durations=reserved_durations, release_ends=release_ends,
        line_choices=line_choices, line_literals=line_literals,
        machine_counts=machine_counts, legal_line_bounds=bounds_by_op,
        tardiness=tardiness, late_literals=late_literals,
        makespan=makespan, line_loads=line_loads,
        load_imbalance=load_imbalance,
        optional_intervals=tm.optional_intervals, size=size,
    )
