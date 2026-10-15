"""Semantically reconciled, isolated Crafter V2.1 CP-SAT master.

V2.1 is a necessary-condition master, not an exact worker scheduler:

* productive work and setup come from the authoritative GA semantics;
* operation end is a machine-hold envelope and may exceed productive work;
* machines are held continuously through that envelope;
* exact pause/resume segments and worker identities stay downstream;
* order-dependent physical-machine changeover stays in exact materialization.

The historical V2 builder is intentionally untouched.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import os
import time
from typing import Mapping

from ortools.sat.python import cp_model
import psutil

from .authoritative_semantics import (
    authoritative_productive_seconds,
    authoritative_setup_seconds,
)
from .contracts import SchedulingProblem
from .labour_capacity import capacity_map, fixed_labour_by_pool
from .model_v2 import legal_line_bounds
from .objective import ObjectiveWeights


SEMANTIC_RECONCILED_V2_1 = "semantic_reconciled_v2_1"


@dataclass(frozen=True)
class V21ModelSize:
    formulation: str
    operation_count: int
    eligible_line_choices: int
    machine_equivalence_classes: int
    class_allocation_variables: int
    optional_intervals: int
    mandatory_intervals: int
    total_variables: int
    total_constraints: int
    variables_by_category: Mapping[str, int]
    constraints_by_category: Mapping[str, int]
    build_time_seconds: float
    rss_before_mb: float
    rss_after_mb: float


@dataclass
class V21ModelArtifacts:
    model: cp_model.CpModel
    starts: dict
    productive_starts: dict
    ends: dict
    productive_durations: dict
    elapsed_durations: dict
    release_ends: dict
    machine_counts: dict
    line_choices: dict
    line_literals: dict
    legal_line_bounds: dict
    machine_loads: dict
    worker_energy: dict
    line_load_terms: dict
    class_allocations: dict
    machine_class: dict
    class_sizes: dict
    tardiness: dict
    late_literals: dict
    makespan: object
    line_loads: dict
    max_line_load: object
    min_line_load: object
    load_imbalance: object
    setup_seconds: dict
    size: V21ModelSize
    formulation: str = SEMANTIC_RECONCILED_V2_1


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
            cp_model.Domain.FromValues([int(value) for value in values]), name)

    def bool_var(self, category, name):
        self.variables[category] += 1
        return self.model.NewBoolVar(name)

    def add(self, category, expression):
        self.constraints[category] += 1
        return self.model.Add(expression)

    def allowed(self, category, variables, tuples):
        self.constraints[category] += 1
        return self.model.AddAllowedAssignments(variables, tuples)

    def exactly_one(self, category, literals):
        self.constraints[category] += 1
        return self.model.AddExactlyOne(literals)

    def fixed_interval(self, category, start, size, name):
        self.constraints[category] += 1
        self.mandatory_intervals += 1
        return self.model.NewFixedSizeIntervalVar(int(start), int(size), name)

    def optional_interval(self, category, start, size, end, present, name):
        self.constraints[category] += 1
        self.optional_intervals += 1
        return self.model.NewOptionalIntervalVar(start, size, end, present, name)

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
    return psutil.Process(os.getpid()).memory_info().rss / 1024**2


def _machine_classes(problem):
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            str(fixed.production_order_id),
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
        for key, ids in members.items() for machine_id in ids
    }
    class_sizes = {
        class_keys[key]: len(ids) for key, ids in members.items()
    }
    class_capabilities = {
        class_keys[key]: set(key[2]) for key in members
    }
    return members, machine_class, class_sizes, class_capabilities


def _fallback_detail(operation):
    return {
        "setup_mins": operation.setup_seconds / 60.0,
        "serial_processing_mins": operation.serial_productive_seconds / 60.0,
    }


def build_model_v2_1(problem: SchedulingProblem, op_details: Mapping[str, dict],
                     weights: ObjectiveWeights = ObjectiveWeights()):
    started = time.perf_counter()
    rss_before = _rss_mb()
    tm = _Tracked()
    model = tm.model
    horizon = int(problem.horizon_seconds)
    labour_caps = capacity_map(problem)
    members, machine_class, class_sizes, class_capabilities = _machine_classes(problem)

    starts, productive_starts, ends = {}, {}, {}
    productive_durations, elapsed_durations, release_ends = {}, {}, {}
    machine_counts, line_choices, line_literals = {}, {}, {}
    machine_loads, worker_energy = {}, {}
    line_load_terms_by_line = defaultdict(list)
    line_load_terms = {}
    class_allocations = {}
    bounds_by_op, setup_by_op = {}, {}
    line_intervals, line_demands = defaultdict(list), defaultdict(list)
    capability_intervals, capability_demands = defaultdict(list), defaultdict(list)
    class_intervals, class_demands = defaultdict(list), defaultdict(list)
    labour_energy_terms = defaultdict(list)
    fixed_labour_energy = Counter()
    eligible_line_choices = 0

    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append(fixed)

    # Fixed physical occupation is exact [start, end). Changeover is a
    # transition before a later different-order use, not an unconditional
    # suffix on this commitment.
    for fixed in problem.resources.fixed_machine_commitments:
        machine = problem.machine_by_id[fixed.machine_id]
        size = int(fixed.end_seconds-fixed.start_seconds)
        if size <= 0:
            raise ValueError("fixed machine commitment must have positive size")
        line_iv = tm.fixed_interval(
            "fixed_machine_capacity", fixed.start_seconds, size,
            f"fixed_line_m{fixed.machine_id}_{fixed.start_seconds}")
        line_intervals[machine.line_id].append(line_iv)
        line_demands[machine.line_id].append(1)
        fixed_capability = getattr(fixed, "required_capability", None)
        capabilities = ((str(fixed_capability).strip().lower(),)
                        if fixed_capability else tuple(
                            item.strip().lower() for item in machine.capabilities))
        for capability in capabilities:
            iv = tm.fixed_interval(
                "fixed_machine_capacity", fixed.start_seconds, size,
                f"fixed_cap_m{fixed.machine_id}_{capability}_{fixed.start_seconds}")
            capability_intervals[(machine.line_id, capability)].append(iv)
            capability_demands[(machine.line_id, capability)].append(1)
        # A fixed/in-flight commitment can belong to a machine currently
        # marked unusable for new assignment. It must still consume its line
        # and capability capacity, but it has no usable-machine class from
        # which a new operation could draw capacity.
        class_id = machine_class.get(fixed.machine_id)
        if class_id is not None:
            class_iv = tm.fixed_interval(
                "fixed_machine_capacity", fixed.start_seconds, size,
                f"fixed_class_{class_id}_m{fixed.machine_id}_{fixed.start_seconds}")
            class_intervals[class_id].append(class_iv)
            class_demands[class_id].append(1)

    for pool, rows in fixed_labour_by_pool(problem).items():
        fixed_labour_energy[pool] += sum(
            int(row.end_seconds-row.start_seconds) for row in rows)

    operations_by_order = defaultdict(list)
    max_load_by_op = {}
    for operation in problem.operations:
        key = operation.operation_key
        detail = dict(op_details.get(key) or _fallback_detail(operation))
        setup_seconds = authoritative_setup_seconds(detail)
        setup_by_op[key] = setup_seconds
        bounds = legal_line_bounds(problem, operation, labour_caps)
        if not bounds:
            raise ValueError(f"operation {key} has no legal line/count domain")
        bounds_by_op[key] = bounds
        eligible_line_choices += len(bounds)
        min_k = min(value[0] for value in bounds.values())
        max_k = max(value[1] for value in bounds.values())
        productive_by_k = {
            count: authoritative_productive_seconds(detail, count)
            for count in range(min_k, max_k+1)
        }
        min_productive = min(productive_by_k.values())
        max_productive = max(productive_by_k.values())
        max_nonpaused = setup_seconds + max_productive
        max_load = max(
            count * (setup_seconds+productive)
            for count, productive in productive_by_k.items())

        start = tm.int_var("operation_time", 0, horizon, f"start_{key}")
        productive_start = tm.int_var(
            "operation_time", 0, horizon, f"productive_start_{key}")
        end = tm.int_var("operation_time", 0, horizon, f"end_{key}")
        elapsed = tm.int_var(
            "operation_time", setup_seconds+min_productive, horizon,
            f"elapsed_duration_{key}")
        release_end = tm.int_var(
            "operation_time", 0, horizon, f"machine_release_{key}")
        machine_count = tm.int_var(
            "machine_count", min_k, max_k, f"k_{key}")
        productive = tm.int_var(
            "operation_time", min_productive, max_productive,
            f"productive_duration_{key}")
        machine_load = tm.int_var(
            "load_balance", 0, max_load, f"machine_load_{key}")
        energy = tm.int_var(
            "labour_energy", 0, max_k*max_productive,
            f"worker_energy_{key}")
        tuples = tuple(
            (count, productive, count*(setup_seconds+productive),
             count*productive)
            for count, productive in sorted(productive_by_k.items())
        )
        tm.allowed("duration_lookup", (
            machine_count, productive, machine_load, energy), tuples)
        tm.add("operation_timing", productive_start == start+setup_seconds)
        tm.add("operation_timing", end == start+elapsed)
        tm.add("operation_timing", end >= productive_start+productive)
        tm.add("operation_timing", release_end == end)

        line_ids = tuple(sorted(bounds))
        line_choice = tm.domain_var("line_choice", line_ids, f"line_{key}")
        literals = []
        for line_id in line_ids:
            low, high, candidate_ids = bounds[line_id]
            literal = tm.bool_var("line_choice", f"on_line_{key}_{line_id}")
            line_literals[(key, line_id)] = literal
            literals.append(literal)
            tm.add("line_choice", line_choice == line_id).OnlyEnforceIf(literal)
            tm.add("line_count_domain", machine_count >= low).OnlyEnforceIf(literal)
            tm.add("line_count_domain", machine_count <= high).OnlyEnforceIf(literal)
            interval = tm.optional_interval(
                "line_pool_intervals", start, elapsed, end, literal,
                f"pool_{key}_{line_id}")
            line_intervals[line_id].append(interval)
            line_demands[line_id].append(machine_count)
            capability = operation.required_capability.strip().lower()
            capability_intervals[(line_id, capability)].append(interval)
            capability_demands[(line_id, capability)].append(machine_count)
            supporting = tuple(sorted({
                machine_class[machine_id] for machine_id in candidate_ids
                if machine_id in machine_class
                and capability in class_capabilities[machine_class[machine_id]]
            }))
            allocations = []
            for class_id in supporting:
                allocation = tm.int_var(
                    "machine_class_allocation", 0,
                    min(high, class_sizes[class_id]),
                    f"class_demand_{key}_{line_id}_{class_id}")
                tm.add("machine_class_allocation",
                       allocation == 0).OnlyEnforceIf(literal.Not())
                class_intervals[class_id].append(interval)
                class_demands[class_id].append(allocation)
                class_allocations[(key, line_id, class_id)] = allocation
                allocations.append(allocation)
            if not allocations:
                raise ValueError(f"operation {key} line {line_id} has no class")
            tm.add("machine_class_allocation",
                   sum(allocations) == machine_count).OnlyEnforceIf(literal)
            load_here = tm.int_var(
                "load_balance", 0, max_load, f"load_{key}_{line_id}")
            tm.add("load_balance",
                   load_here == machine_load).OnlyEnforceIf(literal)
            tm.add("load_balance", load_here == 0).OnlyEnforceIf(literal.Not())
            line_load_terms[(key, line_id)] = load_here
            line_load_terms_by_line[line_id].append(load_here)
        tm.exactly_one("line_choice", literals)

        order = problem.order_by_id[operation.order_id]
        pool = (order.factory_id,
                operation.required_capability.strip().lower())
        labour_energy_terms[pool].append(energy)
        operations_by_order[operation.order_id].append(operation)
        starts[key], productive_starts[key], ends[key] = start, productive_start, end
        productive_durations[key] = productive
        elapsed_durations[key], release_ends[key] = elapsed, release_end
        machine_counts[key], line_choices[key] = machine_count, line_choice
        machine_loads[key], worker_energy[key] = machine_load, energy
        max_load_by_op[key] = max_load

    for order_id, chain in operations_by_order.items():
        chain.sort(key=lambda item: (item.sequence, item.operation_key))
        if len({item.sequence for item in chain}) != len(chain):
            raise ValueError(f"duplicate routing sequence in order {order_id}")
        release = max(0, int(problem.order_by_id[order_id].release_seconds))
        tm.add("release", starts[chain[0].operation_key] >= release)
        for left, right in zip(chain, chain[1:]):
            tm.add("precedence",
                   ends[left.operation_key] <= starts[right.operation_key])

    usable_by_line = Counter(
        machine.line_id for machine in problem.machines if machine.is_usable)
    usable_by_capability = Counter()
    for machine in problem.machines:
        if machine.is_usable:
            for capability in set(
                    item.strip().lower() for item in machine.capabilities):
                usable_by_capability[(machine.line_id, capability)] += 1
    for line_id, intervals in line_intervals.items():
        tm.cumulative("line_machine_capacity", intervals,
                      line_demands[line_id], usable_by_line[line_id])
    for pool, intervals in capability_intervals.items():
        tm.cumulative("capability_machine_capacity", intervals,
                      capability_demands[pool], usable_by_capability[pool])
    for class_id, intervals in class_intervals.items():
        tm.cumulative("machine_class_capacity", intervals,
                      class_demands[class_id], class_sizes[class_id])

    # Safe necessary condition only.  It cannot certify worker feasibility:
    # exact identities, calendars, reservations and common crew segments stay
    # in allocate_crew().  This global energy bound cannot reject a genuinely
    # feasible schedule, although it may accept an infeasible one.
    for pool, terms in labour_energy_terms.items():
        tm.add("labour_energy_capacity",
               sum(terms)+fixed_labour_energy[pool]
               <= int(labour_caps[pool])*horizon)

    tardiness, late_literals = {}, {}
    weighted_tardiness, late_terms, completions = [], [], []
    for order_id, chain in operations_by_order.items():
        final = max(chain, key=lambda item: (item.sequence, item.operation_key))
        completion = ends[final.operation_key]
        completions.append(completion)
        order = problem.order_by_id[order_id]
        if order.deadline_seconds is None:
            continue
        due = max(0, int(order.deadline_seconds))
        tardy = tm.int_var("tardiness_late", 0, horizon, f"tardy_{order_id}")
        tm.max_equality("objective_auxiliaries", tardy,
                        (0, completion-due))
        late = tm.bool_var("tardiness_late", f"late_{order_id}")
        tm.add("objective_auxiliaries", tardy >= 1).OnlyEnforceIf(late)
        tm.add("objective_auxiliaries", tardy == 0).OnlyEnforceIf(late.Not())
        tardiness[order_id], late_literals[order_id] = tardy, late
        weighted_tardiness.append(int(order.priority_weight)*tardy)
        late_terms.append(late)

    makespan = tm.int_var("load_balance", 0, horizon, "makespan")
    tm.max_equality("objective_auxiliaries", makespan, completions)
    total_load_upper = max(1, sum(max_load_by_op.values()))
    line_loads = {}
    for line in problem.lines:
        load = tm.int_var("load_balance", 0, total_load_upper,
                          f"line_load_{line.line_id}")
        tm.add("objective_auxiliaries",
               load == sum(line_load_terms_by_line.get(line.line_id, ())))
        line_loads[line.line_id] = load
    max_line_load = tm.int_var(
        "load_balance", 0, total_load_upper, "max_line_load")
    min_line_load = tm.int_var(
        "load_balance", 0, total_load_upper, "min_line_load")
    tm.max_equality("objective_auxiliaries", max_line_load,
                    tuple(line_loads.values()))
    tm.min_equality("objective_auxiliaries", min_line_load,
                    tuple(line_loads.values()))
    load_imbalance = tm.int_var(
        "load_balance", 0, total_load_upper, "load_imbalance")
    tm.add("objective_auxiliaries",
           load_imbalance == max_line_load-min_line_load)
    model.Minimize(
        weights.tardiness*sum(weighted_tardiness)
        + weights.late_order*sum(late_terms)
        + weights.makespan*makespan
        + weights.load_imbalance*load_imbalance
    )

    proto = model.Proto()
    size = V21ModelSize(
        formulation=SEMANTIC_RECONCILED_V2_1,
        operation_count=len(problem.operations),
        eligible_line_choices=eligible_line_choices,
        machine_equivalence_classes=len(members),
        class_allocation_variables=int(
            tm.variables.get("machine_class_allocation", 0)),
        optional_intervals=tm.optional_intervals,
        mandatory_intervals=tm.mandatory_intervals,
        total_variables=len(proto.variables),
        total_constraints=len(proto.constraints),
        variables_by_category=dict(sorted(tm.variables.items())),
        constraints_by_category=dict(sorted(tm.constraints.items())),
        build_time_seconds=time.perf_counter()-started,
        rss_before_mb=rss_before,
        rss_after_mb=_rss_mb(),
    )
    if sum(size.variables_by_category.values()) != size.total_variables:
        raise AssertionError("V2.1 variable attribution mismatch")
    if sum(size.constraints_by_category.values()) != size.total_constraints:
        raise AssertionError("V2.1 constraint attribution mismatch")
    return V21ModelArtifacts(
        model=model,
        starts=starts, productive_starts=productive_starts, ends=ends,
        productive_durations=productive_durations,
        elapsed_durations=elapsed_durations, release_ends=release_ends,
        machine_counts=machine_counts, line_choices=line_choices,
        line_literals=line_literals, legal_line_bounds=bounds_by_op,
        machine_loads=machine_loads, worker_energy=worker_energy,
        line_load_terms=line_load_terms,
        class_allocations=class_allocations,
        machine_class=machine_class, class_sizes=class_sizes,
        tardiness=tardiness, late_literals=late_literals,
        makespan=makespan, line_loads=line_loads,
        max_line_load=max_line_load, min_line_load=min_line_load,
        load_imbalance=load_imbalance,
        setup_seconds=setup_by_op, size=size,
    )
