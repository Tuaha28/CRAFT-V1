"""Strictly bounded, deterministic solver wrapper for Crafter."""
from __future__ import annotations

import os
from typing import Optional

import ortools
import psutil
from ortools.sat.python import cp_model

from .contracts import (
    CrafterSolution, CrafterStatus, OperationAssignment, SolverStatistics,
)
from .model import EXPANDED_PAIRWISE, ModelArtifacts, build_model
from .objective import ObjectiveWeights


def _memory_mb() -> float:
    info = psutil.Process(os.getpid()).memory_info()
    value = getattr(info, "peak_wset", None) or getattr(info, "rss", 0)
    return float(value) / (1024.0 * 1024.0)


def solve(problem, *, time_limit_seconds: float = 10.0, random_seed: int = 42,
          max_deterministic_time: float = 1.0,
          weights: ObjectiveWeights = ObjectiveWeights(),
          formulation: str = EXPANDED_PAIRWISE, hint=None):
    if time_limit_seconds <= 0:
        raise ValueError("time_limit_seconds must be positive")
    artifacts = build_model(problem, weights, formulation=formulation)
    if hint is not None:
        from .hints import apply_hint
        apply_hint(problem, artifacts, hint)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.max_deterministic_time = float(max_deterministic_time)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = int(random_seed)
    solver.parameters.log_search_progress = False
    cp_status = solver.Solve(artifacts.model)
    status_name = solver.StatusName(cp_status)
    feasible = cp_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    objective = float(solver.ObjectiveValue()) if feasible else None
    bound = float(solver.BestObjectiveBound()) if feasible else None
    gap = (
        abs(objective-bound) / max(1.0, abs(objective))
        if objective is not None and bound is not None else None
    )
    response = solver.ResponseProto()
    proto = artifacts.model.Proto()
    statistics = SolverStatistics(
        ortools_version=ortools.__version__, cp_status=status_name,
        wall_time_seconds=float(solver.WallTime()),
        deterministic_time=float(getattr(response, "deterministic_time", 0.0)),
        objective_value=objective, best_bound=bound, relative_gap=gap,
        variables=len(proto.variables), constraints=len(proto.constraints),
        optional_intervals=artifacts.optional_intervals,
        conflicts=int(solver.NumConflicts()), branches=int(solver.NumBranches()),
        peak_rss_mb=_memory_mb(),
    )
    if not feasible:
        mapped = (CrafterStatus.MASTER_INFEASIBLE
                  if cp_status == cp_model.INFEASIBLE
                  else CrafterStatus.MASTER_UNKNOWN)
        return CrafterSolution(mapped, (), statistics,
                               diagnostics=("No CP-SAT incumbent is operationally feasible.",))

    assignments = []
    for op in problem.operations:
        chosen_index = next(
            i for i, _mode in enumerate(artifacts.modes[op.operation_key])
            if solver.BooleanValue(artifacts.mode_literals[(op.operation_key, i)])
        )
        mode = artifacts.modes[op.operation_key][chosen_index]
        if formulation == EXPANDED_PAIRWISE:
            machine_ids = tuple(sorted(
                machine_id for machine_id in mode.candidate_machine_ids
                if solver.BooleanValue(
                    artifacts.machine_literals[(op.operation_key, chosen_index, machine_id)])
            ))
        else:
            machine_ids = tuple(sorted(
                machine_id for machine_id in {
                    candidate for candidate_mode in artifacts.modes[op.operation_key]
                    for candidate in candidate_mode.candidate_machine_ids
                }
                if solver.BooleanValue(
                    artifacts.machine_literals[(op.operation_key, machine_id)])
            ))
        start = int(solver.Value(artifacts.starts[op.operation_key]))
        end = int(solver.Value(artifacts.ends[op.operation_key]))
        assignments.append(OperationAssignment(
            operation_key=op.operation_key, order_id=op.order_id,
            operation_id=op.operation_id, line_id=mode.line_id,
            machine_ids=machine_ids, machine_count=mode.machine_count,
            start_seconds=start,
            productive_start_seconds=start + mode.setup_seconds,
            end_seconds=end, setup_seconds=mode.setup_seconds,
            productive_seconds=mode.productive_seconds,
        ))

    priority_tardiness = sum(
        problem.order_by_id[order_id].priority_weight * solver.Value(var)
        for order_id, var in artifacts.tardiness.items()
    )
    late_count = sum(solver.BooleanValue(x) for x in artifacts.late_literals.values())
    components = (
        ("priority_weighted_tardiness_seconds", int(priority_tardiness)),
        ("late_order_count", int(late_count)),
        ("makespan_seconds", int(solver.Value(artifacts.makespan))),
        ("load_imbalance_machine_seconds", int(solver.Value(artifacts.load_imbalance))),
    )
    return CrafterSolution(
        CrafterStatus.MASTER_FEASIBLE, tuple(assignments), statistics,
        objective_components=components,
        diagnostics=(
            "MASTER_FEASIBLE is provisional until exact worker allocation and independent validation pass.",
            "CP-SAT ran deterministically with one search worker.",
            f"Changeover formulation: {formulation}.",
        ),
    )
