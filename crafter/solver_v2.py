"""Bounded deterministic solver for the compressed Crafter V2 master."""
from __future__ import annotations

import os

import ortools
import psutil
from ortools.sat.python import cp_model

from .contracts import CrafterSolution, CrafterStatus, OperationAssignment, SolverStatistics
from .model_v2 import build_model_v2
from .objective import ObjectiveWeights


def _memory_mb():
    info = psutil.Process(os.getpid()).memory_info()
    return float(getattr(info, "peak_wset", None) or info.rss) / (1024.0*1024.0)


def _freeze_solution(problem, artifacts, solver, cp_status):
    feasible = cp_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    objective = float(solver.ObjectiveValue()) if feasible else None
    bound = float(solver.BestObjectiveBound()) if feasible else None
    response = solver.ResponseProto()
    stats = SolverStatistics(
        ortools_version=ortools.__version__, cp_status=solver.StatusName(cp_status),
        wall_time_seconds=float(solver.WallTime()),
        deterministic_time=float(getattr(response, "deterministic_time", 0.0)),
        objective_value=objective, best_bound=bound,
        relative_gap=(abs(objective-bound)/max(1.0, abs(objective))
                      if objective is not None and bound is not None else None),
        variables=artifacts.size.total_variables,
        constraints=artifacts.size.total_constraints,
        optional_intervals=artifacts.size.optional_intervals,
        conflicts=int(solver.NumConflicts()), branches=int(solver.NumBranches()),
        peak_rss_mb=_memory_mb(),
    )
    if not feasible:
        status = (CrafterStatus.MASTER_INFEASIBLE if cp_status == cp_model.INFEASIBLE
                  else CrafterStatus.MASTER_UNKNOWN)
        return CrafterSolution(status, (), stats,
            diagnostics=("Compressed master produced no incumbent.",))

    assignments = []
    for op in problem.operations:
        key = op.operation_key
        start = int(solver.Value(artifacts.starts[key]))
        productive = int(solver.Value(artifacts.productive_durations[key]))
        assignments.append(OperationAssignment(
            operation_key=key, order_id=op.order_id, operation_id=op.operation_id,
            line_id=int(solver.Value(artifacts.line_choices[key])), machine_ids=(),
            machine_count=int(solver.Value(artifacts.machine_counts[key])),
            start_seconds=start,
            productive_start_seconds=int(solver.Value(artifacts.productive_starts[key])),
            end_seconds=int(solver.Value(artifacts.ends[key])),
            setup_seconds=int(op.setup_seconds), productive_seconds=productive,
        ))
    priority_tardiness = sum(
        problem.order_by_id[order_id].priority_weight*int(solver.Value(var))
        for order_id, var in artifacts.tardiness.items())
    components = (
        ("priority_weighted_tardiness_seconds", int(priority_tardiness)),
        ("late_order_count", sum(int(solver.BooleanValue(x))
                                 for x in artifacts.late_literals.values())),
        ("makespan_seconds", int(solver.Value(artifacts.makespan))),
        ("load_imbalance_machine_seconds",
         int(solver.Value(artifacts.load_imbalance))),
    )
    return CrafterSolution(
        CrafterStatus.MASTER_FEASIBLE, tuple(assignments), stats,
        objective_components=components,
        diagnostics=(
            "MASTER_FEASIBLE is provisional: physical machines are not yet materialized.",
            "V2 uses exact k-to-duration tables and conservative post-use buffers.",
        ))


def solve_built_v2(problem, artifacts, *, time_limit_seconds=10.0,
                   max_deterministic_time=1.0, random_seed=42,
                   num_search_workers=1, solution_callback=None):
    """Solve and freeze an already-built model exactly once."""
    if time_limit_seconds <= 0:
        raise ValueError("time_limit_seconds must be positive")
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.max_deterministic_time = float(max_deterministic_time)
    solver.parameters.num_search_workers = int(num_search_workers)
    solver.parameters.random_seed = int(random_seed)
    cp_status = solver.Solve(artifacts.model, solution_callback)
    return _freeze_solution(problem, artifacts, solver, cp_status), solver


def solve_v2(problem, *, time_limit_seconds=10.0, max_deterministic_time=1.0,
             random_seed=42, weights=ObjectiveWeights()):
    artifacts = build_model_v2(problem, weights)
    result, _solver = solve_built_v2(
        problem, artifacts, time_limit_seconds=time_limit_seconds,
        max_deterministic_time=max_deterministic_time,
        random_seed=random_seed, num_search_workers=1)
    return result, artifacts
