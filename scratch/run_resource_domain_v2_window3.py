"""Decisive Factory-5 Window-3 adaptive resource-domain probe.

This is a read-only reproduction of the known compact-domain failure.  The
controller itself is generic; the fixture selection is intentionally explicit
only for this falsification test.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import CommittedScheduleState, GlobalCRAFTPlanner
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.resource_domain_v2 import (
    ResourceDomainPolicyV2, run_adaptive_planning_schedule,
)
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.global_planner import factory_future_operation_keys
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype
from crafter.repair.scoped_validation import validate_planning_window

ROOT = Path("outputs/crafter/global_craft_factory5_v1")
OUT = Path("outputs/crafter/resource_domain_v2")
OUT.mkdir(parents=True, exist_ok=True)


def rows(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))["selected_assignments"]


def main():
    loaded = _load_state()
    w1 = json.loads((ROOT / "window_001.json").read_text(encoding="utf-8"))
    w2 = json.loads((ROOT / "window_002.json").read_text(encoding="utf-8"))
    state1 = build_planning_state(
        loaded,
        PlanningContextV1("resource-v2-1", "INITIAL_PLAN", 5, 0, 0, 86400, 20000,
                          tuple(w1["pre_solve_required_keys"]), "FROZEN_BENCHMARK_RAW",
                          snapshot_as_of_seconds=0))
    b1 = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
        {"acceptance": {"status": "ACCEPTED", "selected_schedule": rows("window_001.json")},
         "state": state1}, factory_id=5, commit_end_seconds=20000,
        source_repair_id="resource-v2-1")
    state2 = build_planning_state(
        loaded,
        PlanningContextV1("resource-v2-2", "ROLLING_PLAN", 5, 20000, 20000, 106400, 40000,
                          tuple(w2["pre_solve_required_keys"]), "FROZEN_BENCHMARK_RAW",
                          previous_provisional_plan_reference="resource-v2-1",
                          snapshot_as_of_seconds=0), boundary=b1)
    b2 = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
        {"acceptance": {"status": "ACCEPTED", "selected_schedule": rows("window_002.json")},
         "state": state2}, factory_id=5, commit_end_seconds=40000,
        source_repair_id="resource-v2-2")
    committed = CommittedScheduleState(5).append(b1).append(b2)
    from scratch.run_one_factory_craft import merge_boundaries
    boundary = merge_boundaries(committed)
    failure = json.loads((ROOT / "window_003.json").read_text(encoding="utf-8"))
    context = PlanningContextV1(
        "resource-v2-3", "ROLLING_PLAN", 5, 40000, 40000, 126400, 60000,
        tuple(failure["pre_solve_required_keys"]), "FROZEN_BENCHMARK_RAW",
        previous_provisional_plan_reference="resource-v2-2", snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, context, boundary=boundary)
    result = run_adaptive_planning_schedule(
        state, cache=CalendarRegimeCache(), objective_mode=False,
        policy=ResourceDomainPolicyV2(initial_machine_sets=1,
                                      initial_worker_crews=1,
                                      diagnostic_time_limit_seconds=15.0))
    selected = list(result.get("acceptance", {}).get("selected_schedule", ()))
    for row in selected:
        op = state["problem"].operation_by_key[str(row["operation_key"])]
        row["order_id"] = str(op.order_id)
        cfg = row["resource_configuration"]
        row["setup_seconds"] = int(cfg["setup_seconds"])
        row["productive_seconds"] = int(cfg["productive_seconds"])
        row["productive_minutes"] = float(cfg.get("productive_minutes", 0.0))
    future = factory_future_operation_keys(state, 5)
    scope = ValidationScopeV1("WINDOW", tuple(context.selected_operation_keys), future)
    boundary_facts = [dict(row) for row in boundary.machine_commitments]
    boundary_facts.extend(dict(row) for row in boundary.worker_commitments)
    for key, route in boundary.routing_completion_state.items():
        boundary_facts.append({"operation_key": key, **dict(route),
                               "authoritative_end_seconds": route["authoritative_end_seconds"]})
    phenotype, _ = build_window_phenotype(
        selected, boundary_facts=tuple(boundary_facts), future_outside_operation_keys=future,
        validation_scope=scope)
    scoped = validate_planning_window(state, phenotype)
    payload = {
        "fixture": "Factory 5 Window 3 from accepted rolling boundaries",
        "initial_policy": {"machine_sets": 1, "worker_crews": 1},
        "status": result.get("acceptance", {}).get("status"),
        "resource_domain_v2": result.get("resource_domain_v2"),
        "cp_stages": result.get("acceptance", {}).get("stages", []),
        "materialization": result.get("acceptance", {}).get("materialization", {}),
        "replay": result.get("acceptance", {}).get("replay", {}),
        "validator": result.get("acceptance", {}).get("validator", {}),
        "scoped_planning_validation": scoped,
        "database_writes": result.get("acceptance", {}).get("database_writes", 0),
    }
    (OUT / "window3_expansion_trace.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"],
                      "rounds": [(r["round"], r["machine_cap"], r["worker_cap"],
                                  r.get("canonical_status"), r.get("resource_domain_status"))
                                 for r in payload["resource_domain_v2"]["rounds"]]},
                     default=str), flush=True)


if __name__ == "__main__":
    main()
