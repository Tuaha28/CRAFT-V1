"""Instrumented CP-SAT master formulations for isolated Crafter experiments.

``expanded_pairwise`` is the smoke-test compatibility formulation.
``compact_pairwise`` preserves its exact placeholder changeover semantics while
deduplicating physical-machine assignment literals across line/count modes.
``conservative_buffer`` keeps physical machine identity exact but reserves a
full changeover buffer after every selected operation. It is deliberately
conservative and must not be described as exact sequence-dependent setup.
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
from .machine_model import legal_modes
from .objective import ObjectiveWeights


EXPANDED_PAIRWISE = "expanded_pairwise"
COMPACT_PAIRWISE = "compact_pairwise"
CONSERVATIVE_BUFFER = "conservative_buffer"
SUPPORTED_FORMULATIONS = (
    EXPANDED_PAIRWISE,
    COMPACT_PAIRWISE,
    CONSERVATIVE_BUFFER,
)


@dataclass(frozen=True)
class ModelSizeDiagnostics:
    formulation: str
    operation_count: int
    candidate_modes: int
    candidate_machine_assignments: int
    optional_intervals: int
    changeover_pairs: int
    changeover_variables: int
    changeover_constraints: int
    variables_by_category: Mapping[str, int]
    constraints_by_category: Mapping[str, int]
    total_variables: int
    total_constraints: int
    build_time_seconds: float
    rss_before_mb: float
    rss_after_mb: float


@dataclass
class ModelArtifacts:
    model: cp_model.CpModel
    starts: dict
    ends: dict
    mode_literals: dict
    modes: dict
    machine_literals: dict
    tardiness: dict
    late_literals: dict
    makespan: object
    line_loads: dict
    load_imbalance: object
    optional_intervals: int
    size: ModelSizeDiagnostics
    formulation: str


class _TrackedModel:
    """Small accounting wrapper; categories sum to final proto totals."""

    def __init__(self):
        self.model = cp_model.CpModel()
        self.variables = Counter()
        self.constraints = Counter()
        self.optional_intervals = 0

    def int_var(self, category, lb, ub, name):
        self.variables[category] += 1
        return self.model.NewIntVar(lb, ub, name)

    def bool_var(self, category, name):
        self.variables[category] += 1
        return self.model.NewBoolVar(name)

    def add(self, category, expression, name=None):
        self.constraints[category] += 1
        row = self.model.Add(expression)
        return row.WithName(name) if name else row

    def implication(self, category, left, right):
        self.constraints[category] += 1
        return self.model.AddImplication(left, right)

    def bool_or(self, category, literals):
        self.constraints[category] += 1
        return self.model.AddBoolOr(literals)

    def at_most_one(self, category, literals):
        self.constraints[category] += 1
        return self.model.AddAtMostOne(literals)

    def exactly_one(self, category, literals):
        self.constraints[category] += 1
        return self.model.AddExactlyOne(literals)

    def optional_interval(self, category, start, size, end, present, name):
        self.constraints[category] += 1
        self.optional_intervals += 1
        return self.model.NewOptionalIntervalVar(start, size, end, present, name)

    def interval(self, category, start, size, end, name):
        self.constraints[category] += 1
        return self.model.NewIntervalVar(start, size, end, name)

    def fixed_interval(self, category, start, size, name):
        self.constraints[category] += 1
        return self.model.NewFixedSizeIntervalVar(start, size, name)

    def no_overlap(self, category, intervals):
        self.constraints[category] += 1
        return self.model.AddNoOverlap(intervals)

    def cumulative(self, category, intervals, demands, capacity):
        self.constraints[category] += 1
        return self.model.AddCumulative(intervals, demands, capacity)

    def max_equality(self, category, target, expressions):
        self.constraints[category] += 1
        return self.model.AddMaxEquality(target, expressions)

    def min_equality(self, category, target, expressions):
        self.constraints[category] += 1
        return self.model.AddMinEquality(target, expressions)


def _rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)


def _filtered_modes(problem, operation, cap_by_pool):
    """Remove only modes that violate a proven aggregate capacity bound."""
    order = problem.order_by_id[operation.order_id]
    pool = (order.factory_id, operation.required_capability.strip().lower())
    if pool not in cap_by_pool:
        raise ValueError(f"missing aggregate labour capacity for {pool}")
    capacity = cap_by_pool[pool]
    return tuple(mode for mode in legal_modes(problem, operation)
                 if mode.machine_count <= capacity)


def estimate_candidate_structure(problem: SchedulingProblem,
                                 formulation: str = EXPANDED_PAIRWISE) -> dict:
    """Cheap preflight count that allocates no CP-SAT variables."""
    if formulation not in SUPPORTED_FORMULATIONS:
        raise ValueError(f"unsupported changeover formulation: {formulation}")
    cap_by_pool = capacity_map(problem)
    assignments = defaultdict(list)
    mode_count = 0
    assignment_count = 0
    for op in problem.operations:
        modes = _filtered_modes(problem, op, cap_by_pool)
        mode_count += len(modes)
        if formulation == EXPANDED_PAIRWISE:
            for mode_index, mode in enumerate(modes):
                for machine_id in mode.candidate_machine_ids:
                    assignments[machine_id].append((op.operation_key, mode_index))
                    assignment_count += 1
        else:
            machine_ids = sorted({machine_id for mode in modes
                                  for machine_id in mode.candidate_machine_ids})
            for machine_id in machine_ids:
                assignments[machine_id].append((op.operation_key, None))
                assignment_count += 1
    pairs = 0
    if formulation != CONSERVATIVE_BUFFER:
        for rows in assignments.values():
            for index, left in enumerate(rows):
                pairs += sum(1 for right in rows[index + 1:] if left[0] != right[0])
    return {
        "candidate_modes": mode_count,
        "candidate_machine_assignments": assignment_count,
        "changeover_pairs": pairs,
        "estimated_changeover_variables": pairs * 2,
        "estimated_changeover_constraints": pairs * 8,
    }


def _add_pairwise_changeovers(tm, problem, assignment_literals_by_machine):
    pair_count = 0
    for machine_id, assignments in assignment_literals_by_machine.items():
        for index, left in enumerate(assignments):
            for right in assignments[index + 1:]:
                if left[0] == right[0]:
                    continue
                pair_count += 1
                before_lr = tm.bool_var(
                    "changeover_sequence", f"m{machine_id}_{left[0]}_before_{right[0]}_{pair_count}")
                before_rl = tm.bool_var(
                    "changeover_sequence", f"m{machine_id}_{right[0]}_before_{left[0]}_{pair_count}")
                tm.implication("changeover", before_lr, left[2])
                tm.implication("changeover", before_lr, right[2])
                tm.implication("changeover", before_rl, left[2])
                tm.implication("changeover", before_rl, right[2])
                tm.at_most_one("changeover", (before_lr, before_rl))
                tm.bool_or("changeover", (before_lr, before_rl, left[2].Not(), right[2].Not()))
                gap = problem.changeover_seconds if left[1] != right[1] else 0
                tm.add("changeover", right[3] >= left[4] + gap).OnlyEnforceIf(before_lr)
                tm.add("changeover", left[3] >= right[4] + gap).OnlyEnforceIf(before_rl)
    return pair_count


def build_model(problem: SchedulingProblem,
                weights: ObjectiveWeights = ObjectiveWeights(), *,
                formulation: str = EXPANDED_PAIRWISE) -> ModelArtifacts:
    if formulation not in SUPPORTED_FORMULATIONS:
        raise ValueError(f"unsupported changeover formulation: {formulation}")
    started = time.perf_counter()
    rss_before = _rss_mb()
    tm = _TrackedModel()
    model = tm.model
    horizon = int(problem.horizon_seconds)
    starts, ends = {}, {}
    mode_literals, modes = {}, {}
    machine_literals = {}
    machine_intervals = defaultdict(list)
    labour_intervals = defaultdict(list)
    labour_demands = defaultdict(list)
    fixed_by_machine = defaultdict(list)
    cap_by_pool = capacity_map(problem)

    for fixed in problem.resources.fixed_machine_commitments:
        if fixed.end_seconds <= fixed.start_seconds:
            raise ValueError("fixed machine commitment end must exceed start")
        buffer_seconds = problem.changeover_seconds if formulation == CONSERVATIVE_BUFFER else 0
        interval = tm.fixed_interval(
            "fixed_commitments", int(fixed.start_seconds),
            int(fixed.end_seconds - fixed.start_seconds + buffer_seconds),
            f"fixed_m{fixed.machine_id}_{fixed.start_seconds}")
        machine_intervals[fixed.machine_id].append(interval)
        fixed_by_machine[fixed.machine_id].append(fixed)

    for pool, rows in fixed_labour_by_pool(problem).items():
        # A fixed identity reservation is always retained for the exact
        # worker bridge.  The aggregate master only needs a Cumulative pool
        # when this bounded problem actually has that (factory, skill) pool.
        if pool not in cap_by_pool:
            continue
        for fixed in rows:
            interval = tm.fixed_interval(
                "labour_capacity", int(fixed.start_seconds),
                int(fixed.end_seconds - fixed.start_seconds),
                f"fixed_w_{fixed.employee_id}_{fixed.start_seconds}")
            labour_intervals[pool].append(interval)
            labour_demands[pool].append(1)

    assignment_literals_by_machine = defaultdict(list)
    line_load_terms = defaultdict(list)

    for op in problem.operations:
        op_modes = _filtered_modes(problem, op, cap_by_pool)
        if not op_modes:
            raise ValueError(f"operation {op.operation_key} has no legal mode after capacity screening")
        modes[op.operation_key] = op_modes
        start = tm.int_var("operation_time", 0, horizon, f"start_{op.operation_key}")
        end = tm.int_var("operation_time", 0, horizon, f"end_{op.operation_key}")
        productive_start = tm.int_var(
            "operation_time", 0, horizon, f"productive_start_{op.operation_key}")
        tm.add("operation_timing", productive_start == start + int(op.setup_seconds))
        starts[op.operation_key], ends[op.operation_key] = start, end

        literals = []
        for mode_index, mode in enumerate(op_modes):
            lit = tm.bool_var(
                "line_count_modes", f"mode_{op.operation_key}_{mode.line_id}_{mode.machine_count}")
            mode_literals[(op.operation_key, mode_index)] = lit
            literals.append(lit)
            if formulation == EXPANDED_PAIRWISE:
                tm.optional_interval(
                    "mode_intervals", start, mode.duration_seconds, end, lit,
                    f"opiv_{op.operation_key}_{mode_index}")

            order = problem.order_by_id[op.order_id]
            pool = (order.factory_id, op.required_capability.strip().lower())
            productive_interval = tm.optional_interval(
                "labour_capacity", productive_start, mode.productive_seconds, end, lit,
                f"labiv_{op.operation_key}_{mode_index}")
            labour_intervals[pool].append(productive_interval)
            labour_demands[pool].append(mode.machine_count)
            line_load_terms[mode.line_id].append(
                mode.machine_count * mode.duration_seconds * lit)

        tm.exactly_one("mode_selection", literals)

        if formulation == EXPANDED_PAIRWISE:
            for mode_index, mode in enumerate(op_modes):
                lit = mode_literals[(op.operation_key, mode_index)]
                selected = []
                for machine_id in mode.candidate_machine_ids:
                    y = tm.bool_var(
                        "physical_machine_selection",
                        f"use_{op.operation_key}_{mode_index}_m{machine_id}")
                    machine_literals[(op.operation_key, mode_index, machine_id)] = y
                    tm.implication("machine_selection", y, lit)
                    interval = tm.optional_interval(
                        "machine_intervals", start, mode.duration_seconds, end, y,
                        f"miv_{op.operation_key}_{mode_index}_m{machine_id}")
                    machine_intervals[machine_id].append(interval)
                    assignment_literals_by_machine[machine_id].append(
                        (op.operation_key, op.order_id, y, start, end))
                    selected.append(y)
                    for fixed in fixed_by_machine.get(machine_id, ()):
                        if fixed.production_order_id != op.order_id:
                            tm.add(
                                "changeover",
                                start >= int(fixed.end_seconds + problem.changeover_seconds),
                            ).OnlyEnforceIf(y)
                tm.add("machine_selection", sum(selected) == mode.machine_count * lit)
        else:
            min_duration = min(mode.duration_seconds for mode in op_modes)
            max_duration = max(mode.duration_seconds for mode in op_modes)
            duration = tm.int_var(
                "operation_time", min_duration, max_duration, f"duration_{op.operation_key}")
            tm.add(
                "operation_timing",
                duration == sum(mode.duration_seconds * mode_literals[(op.operation_key, i)]
                                for i, mode in enumerate(op_modes)))
            tm.interval("mode_intervals", start, duration, end, f"opiv_{op.operation_key}")
            reserved_duration = None
            release_end = None
            if formulation == CONSERVATIVE_BUFFER:
                reserved_duration = tm.int_var(
                    "operation_time",
                    min_duration + problem.changeover_seconds,
                    max_duration + problem.changeover_seconds,
                    f"reserved_duration_{op.operation_key}")
                release_end = tm.int_var(
                    "operation_time", 0, horizon + problem.changeover_seconds,
                    f"machine_release_{op.operation_key}")
                tm.add("operation_timing", reserved_duration == duration + problem.changeover_seconds)
                tm.add("operation_timing", release_end == end + problem.changeover_seconds)
            candidate_ids = sorted({machine_id for mode in op_modes
                                    for machine_id in mode.candidate_machine_ids})
            selected = []
            for machine_id in candidate_ids:
                y = tm.bool_var(
                    "physical_machine_selection", f"use_{op.operation_key}_m{machine_id}")
                machine_literals[(op.operation_key, machine_id)] = y
                line_id = problem.machine_by_id[machine_id].line_id
                compatible_modes = [
                    mode_literals[(op.operation_key, i)]
                    for i, mode in enumerate(op_modes) if mode.line_id == line_id
                ]
                tm.add("machine_selection", y <= sum(compatible_modes))
                if formulation == CONSERVATIVE_BUFFER:
                    interval = tm.optional_interval(
                        "machine_intervals", start, reserved_duration, release_end, y,
                        f"miv_{op.operation_key}_m{machine_id}")
                else:
                    interval = tm.optional_interval(
                        "machine_intervals", start, duration, end, y,
                        f"miv_{op.operation_key}_m{machine_id}")
                    assignment_literals_by_machine[machine_id].append(
                        (op.operation_key, op.order_id, y, start, end))
                    for fixed in fixed_by_machine.get(machine_id, ()):
                        if fixed.production_order_id != op.order_id:
                            tm.add(
                                "changeover",
                                start >= int(fixed.end_seconds + problem.changeover_seconds),
                            ).OnlyEnforceIf(y)
                machine_intervals[machine_id].append(interval)
                selected.append(y)
            tm.add(
                "machine_selection",
                sum(selected) == sum(
                    mode.machine_count * mode_literals[(op.operation_key, i)]
                    for i, mode in enumerate(op_modes)))

    operations_by_order = defaultdict(list)
    for op in problem.operations:
        operations_by_order[op.order_id].append(op)
    for order_id, chain in operations_by_order.items():
        chain.sort(key=lambda x: (x.sequence, x.operation_key))
        if len({x.sequence for x in chain}) != len(chain):
            raise ValueError(f"duplicate routing sequence in order {order_id}")
        order = problem.order_by_id[order_id]
        tm.add("release", starts[chain[0].operation_key] >= max(0, int(order.release_seconds)))
        for left, right in zip(chain, chain[1:]):
            tm.add("precedence", ends[left.operation_key] <= starts[right.operation_key])

    for machine in problem.machines:
        intervals = machine_intervals.get(machine.machine_id, ())
        if intervals:
            tm.no_overlap("machine_no_overlap", intervals)

    changeover_pairs = 0
    if formulation != CONSERVATIVE_BUFFER:
        changeover_pairs = _add_pairwise_changeovers(
            tm, problem, assignment_literals_by_machine)

    for pool, intervals in labour_intervals.items():
        tm.cumulative("labour_capacity", intervals, labour_demands[pool], cap_by_pool[pool])

    tardiness, late_literals = {}, {}
    weighted_tardiness_terms = []
    late_terms = []
    completions = []
    for order_id, chain in operations_by_order.items():
        final = max(chain, key=lambda x: (x.sequence, x.operation_key))
        completion = ends[final.operation_key]
        completions.append(completion)
        order = problem.order_by_id[order_id]
        if order.deadline_seconds is None:
            continue
        due = max(0, int(order.deadline_seconds))
        tardy = tm.int_var("tardiness_late", 0, horizon, f"tardy_{order_id}")
        tm.max_equality("objective_auxiliaries", tardy, [0, completion - due])
        late = tm.bool_var("tardiness_late", f"late_{order_id}")
        tm.add("objective_auxiliaries", tardy >= 1).OnlyEnforceIf(late)
        tm.add("objective_auxiliaries", tardy == 0).OnlyEnforceIf(late.Not())
        tardiness[order_id] = tardy
        late_literals[order_id] = late
        weighted_tardiness_terms.append(int(order.priority_weight) * tardy)
        late_terms.append(late)

    makespan = tm.int_var("load_balance_makespan", 0, horizon, "makespan")
    tm.max_equality("objective_auxiliaries", makespan, completions)
    line_loads = {}
    load_upper = horizon * max(1, len(problem.machines))
    for line in problem.lines:
        load = tm.int_var(
            "load_balance_makespan", 0, load_upper, f"line_load_{line.line_id}")
        tm.add("objective_auxiliaries", load == sum(line_load_terms.get(line.line_id, ())))
        line_loads[line.line_id] = load
    max_load = tm.int_var("load_balance_makespan", 0, load_upper, "max_line_load")
    min_load = tm.int_var("load_balance_makespan", 0, load_upper, "min_line_load")
    tm.max_equality("objective_auxiliaries", max_load, list(line_loads.values()))
    tm.min_equality("objective_auxiliaries", min_load, list(line_loads.values()))
    load_imbalance = tm.int_var(
        "load_balance_makespan", 0, load_upper, "load_imbalance")
    tm.add("objective_auxiliaries", load_imbalance == max_load - min_load)

    # All coefficients/components are non-negative. Increasing any component
    # while holding the rest fixed cannot improve this minimization objective.
    model.Minimize(
        weights.tardiness * sum(weighted_tardiness_terms)
        + weights.late_order * sum(late_terms)
        + weights.makespan * makespan
        + weights.load_imbalance * load_imbalance
    )

    proto = model.Proto()
    candidate = estimate_candidate_structure(problem, formulation)
    size = ModelSizeDiagnostics(
        formulation=formulation,
        operation_count=len(problem.operations),
        candidate_modes=candidate["candidate_modes"],
        candidate_machine_assignments=candidate["candidate_machine_assignments"],
        optional_intervals=tm.optional_intervals,
        changeover_pairs=changeover_pairs,
        changeover_variables=int(tm.variables.get("changeover_sequence", 0)),
        changeover_constraints=int(tm.constraints.get("changeover", 0)),
        variables_by_category=dict(sorted(tm.variables.items())),
        constraints_by_category=dict(sorted(tm.constraints.items())),
        total_variables=len(proto.variables),
        total_constraints=len(proto.constraints),
        build_time_seconds=time.perf_counter() - started,
        rss_before_mb=rss_before,
        rss_after_mb=_rss_mb(),
    )
    if sum(size.variables_by_category.values()) != size.total_variables:
        raise AssertionError("Crafter variable attribution does not match model proto")
    if sum(size.constraints_by_category.values()) != size.total_constraints:
        raise AssertionError("Crafter constraint attribution does not match model proto")

    return ModelArtifacts(
        model=model, starts=starts, ends=ends,
        mode_literals=mode_literals, modes=modes,
        machine_literals=machine_literals, tardiness=tardiness,
        late_literals=late_literals, makespan=makespan,
        line_loads=line_loads, load_imbalance=load_imbalance,
        optional_intervals=tm.optional_intervals, size=size,
        formulation=formulation,
    )
