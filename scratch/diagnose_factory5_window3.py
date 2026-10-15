"""Read-only diagnosis of the persisted Factory-5 Window-3 infeasibility."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import (
    CommittedScheduleState,
    GlobalCRAFTPlanner,
)
from crafter.repair.planning import PlanningContextV1, build_planning_state, prepare_planning_schedule
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.cp_repair import build_repair_model
from ortools.sat.python import cp_model


ROOT = Path("outputs/crafter/global_craft_factory5_v1")


def rows(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))["selected_assignments"]


def main():
    loaded = _load_state()
    problem = loaded[0]
    w1 = json.loads((ROOT / "window_001.json").read_text(encoding="utf-8"))
    w2 = json.loads((ROOT / "window_002.json").read_text(encoding="utf-8"))
    r1, r2 = rows("window_001.json"), rows("window_002.json")
    context1 = PlanningContextV1("diagnostic-1", "INITIAL_PLAN", 5, 0, 0, 86400, 20000,
                                 tuple(w1["pre_solve_required_keys"]), "FROZEN_BENCHMARK_RAW",
                                 snapshot_as_of_seconds=0)
    state1 = build_planning_state(loaded, context1, boundary=None)
    b1 = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
        {"acceptance": {"status": "ACCEPTED", "selected_schedule": r1}, "state": state1},
        factory_id=5, commit_end_seconds=20000, source_repair_id="diagnostic-1")
    context2 = PlanningContextV1("diagnostic-2", "ROLLING_PLAN", 5, 20000, 20000, 106400, 40000,
                                 tuple(w2["pre_solve_required_keys"]), "FROZEN_BENCHMARK_RAW",
                                 previous_provisional_plan_reference="diagnostic-1",
                                 snapshot_as_of_seconds=0)
    state2 = build_planning_state(loaded, context2, boundary=b1)
    b2 = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
        {"acceptance": {"status": "ACCEPTED", "selected_schedule": r2}, "state": state2},
        factory_id=5, commit_end_seconds=40000, source_repair_id="diagnostic-2")
    committed = CommittedScheduleState(5).append(b1).append(b2)
    from scratch.run_one_factory_craft import merge_boundaries
    boundary = merge_boundaries(committed)
    failure = json.loads((ROOT / "window_003.json").read_text(encoding="utf-8"))
    required = tuple(failure["window_acceptance"]["scope"]["missing_required_operations"])
    context3 = PlanningContextV1("diagnostic-3", "ROLLING_PLAN", 5, 40000, 40000, 126400, 60000,
                                 required, "FROZEN_BENCHMARK_RAW",
                                 previous_provisional_plan_reference="diagnostic-2",
                                 snapshot_as_of_seconds=0)
    state3 = build_planning_state(loaded, context3, boundary=boundary)
    max_machine_sets = int(os.environ.get("CRAFT_DIAG_MAX_MACHINE_SETS", "1"))
    max_worker_crews = int(os.environ.get("CRAFT_DIAG_MAX_WORKER_CREWS", "1"))
    prepared = prepare_planning_schedule(state3, max_machine_sets=max_machine_sets,
                                         max_worker_crews=max_worker_crews)
    result = run_canonical_schedule(prepared, objective_mode=False)
    variants = {
        "canonical": {},
        "without_routing": {"include_internal_routing": False, "include_outside_routing": False},
        "without_internal_routing": {"include_internal_routing": False},
        "without_outside_routing": {"include_outside_routing": False},
        "without_changeover": {"include_changeover": False},
        "without_machine_overlap": {"include_internal_machine_no_overlap": False, "include_outside_machine_reservations": False},
        "without_worker_overlap": {"include_internal_worker_no_overlap": False, "include_outside_worker_reservations": False},
        "without_machine_or_worker_overlap": {
            "include_internal_machine_no_overlap": False,
            "include_outside_machine_reservations": False,
            "include_internal_worker_no_overlap": False,
            "include_outside_worker_reservations": False,
        },
    }
    relaxation_results = {}
    routing_artifact = build_repair_model(state3, prepared["regimes"])
    for name, kwargs in variants.items():
        artifact = build_repair_model(state3, prepared["regimes"], **kwargs)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5.0
        code = solver.Solve(artifact["model"])
        relaxation_results[name] = {
            "status": solver.StatusName(code),
            "variables": artifact["size"]["variables"],
            "constraints": artifact["size"]["constraints"],
            "optional_intervals": artifact["size"]["optional_intervals"],
        }
    boundary_only = build_repair_model(state3, prepared["regimes"], include_outside_routing=False)
    boundary_tests = {
        "boundary_431_only": (("431_294", 40524),),
        "boundary_549_only": (("549_328", 39611),),
        "both_boundary_constraints": (("431_294", 40524), ("549_328", 39611)),
    }
    for name, constraints in boundary_tests.items():
        artifact = build_repair_model(state3, prepared["regimes"], include_outside_routing=False)
        for key, lower in constraints:
            artifact["model"].Add(artifact["starts"][key] >= int(lower))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 5.0
        code = solver.Solve(artifact["model"])
        relaxation_results[name] = {"status": solver.StatusName(code)}
    payload = {
        "required": required,
        "boundary_committed": boundary.committed_operation_keys,
        "regimes_per_operation": {k: len(v) for k, v in prepared["regimes"].items()},
        "boundary_successor_regime_bounds": {
            key: {
                "min_lo": min((int(row.get("lo", 0)) for row in prepared["regimes"].get(key, ())), default=None),
                "max_hi": max((int(row.get("hi", 0)) for row in prepared["regimes"].get(key, ())), default=None),
            }
            for key in ("431_294", "549_328")
        },
        "canonical_status": result.get("acceptance", {}).get("status"),
        "stages": result.get("acceptance", {}).get("stages", []),
        "relaxation_results": relaxation_results,
        "routing_context": {
            "predecessors": {key: state3["after_index"].route_predecessor.get(key) for key in required},
            "successors": {key: state3["after_index"].route_successor.get(key) for key in required},
            "committed_routing_completion": state3.get("committed_routing_completion", {}),
            "routing_constraint_records": routing_artifact.get("routing_constraint_records", []),
        },
    }
    output_name = "window3_diagnostic.json" if (max_machine_sets, max_worker_crews) == (1, 1) else "window3_diagnostic_config3.json"
    (ROOT / output_name).write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"relaxation_results": relaxation_results,
                      "boundary_successor_regime_bounds": payload["boundary_successor_regime_bounds"]},
                     default=str), flush=True)


if __name__ == "__main__":
    main()
