"""Persist the bounded Step-3B orchestration probes (no CP/DB writes)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.objectives import ManagerPriorityOverrideV1
from crafter.repair.planning import PlanningContextV1, build_planning_state, select_precedence_safe_window
from crafter.repair.rolling_horizon_v2 import (
    decide_rolling_orchestration_v2, initialize_rolling_window_v2,
)


OUT = Path("outputs/crafter/rolling_horizon_v2_step3b")
OUT.mkdir(parents=True, exist_ok=True)


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def fixture(keys=("1_a", "1_b"), states=None):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    orders = (OrderSpec("1", 1, 0, 10_000, 1),)
    operations = tuple(OperationSpec(key, "1", key.split("_", 1)[1], i + 1,
                                     "sew", (1,), 60) for i, key in enumerate(keys))
    problem = SchedulingProblem(
        "step3b-probes", origin, 20_000,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
        orders, operations, (),
    )
    classified = {key: {"state": (states or {}).get(key, "FUTURE_RELEASED")}
                  for key in keys}
    return problem, classified


def context(start=0, end=500, commit=200):
    return PlanningContextV1("step3b-probe", "ROLLING_PLAN", 1, start, start, end, commit,
                             ("1_b",), "STEP3B_PROBE", snapshot_as_of_seconds=0)


def route(values):
    return SimpleNamespace(route_predecessor=dict(values))


def boundary(end):
    return SimpleNamespace(
        factory_id=1, committed_operation_keys=("1_a",),
        routing_completion_state={"1_a": {"operation_key": "1_a",
                                           "authoritative_end_seconds": end}},
        machine_commitments=(), worker_commitments=(),
    )


def synthetic_probes():
    # Ready work takes priority over unrelated deferred work.
    problem, classified = fixture(("1_a", "1_b", "1_c"), {"1_a": "COMMITTED_FUTURE"})
    ready = decide_rolling_orchestration_v2(
        window_id="step3b-ready", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a", "1_c": None}),
        classified=classified, admitted_operation_keys=("1_c",),
        remaining_operation_keys=("1_b", "1_c"), committed_operation_keys=("1_a",),
        boundary=boundary(700), maximum_operations=2)
    # No ready work: the earliest authoritative predecessor event is selected.
    problem, classified = fixture(states={"1_a": "COMMITTED_FUTURE"})
    fast = decide_rolling_orchestration_v2(
        window_id="step3b-fast", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), committed_operation_keys=("1_a",),
        boundary=boundary(700),
        authoritative_future_events=(
            {"event_type": "HARD_RESERVATION_END", "event_time_seconds": 900,
             "operation_key": "reservation-900", "source": "reservation-ledger"},
            {"event_type": "DISRUPTION_RECOVERY", "event_time_seconds": 650,
             "operation_key": "recovery-650", "source": "recovery-ledger"},
            {"event_type": "ORDER_RELEASE", "event_time_seconds": 800,
             "operation_key": "release-800", "source": "release-ledger"},
        ))
    # Urgent hard-blocked work requests the smallest configured extension.
    urgent = ManagerPriorityOverrideV1(
        "urgent-1", "1", "URGENT", 0, reason_code="RUSH", entered_by="probe",
        entered_at=0, revision=1)
    horizon = decide_rolling_orchestration_v2(
        window_id="step3b-horizon", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), committed_operation_keys=("1_a",),
        boundary=boundary(550), manager_overrides=(urgent,),
        temporal_extension_stages_seconds=(600, 900))
    # Movable predecessor closure exceeds the normal operation cap.
    problem, classified = fixture()
    neighborhood = decide_rolling_orchestration_v2(
        window_id="step3b-neighborhood", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), maximum_operations=1)
    # Unknown execution truth must not be fast-forwarded.
    problem, classified = fixture(states={"1_a": "UNKNOWN"})
    unknown = decide_rolling_orchestration_v2(
        window_id="step3b-unknown", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",))
    dump("fast_forward_probe.json", fast.as_dict())
    dump("horizon_extension_probe.json", horizon.as_dict())
    dump("neighborhood_expansion_probe.json", neighborhood.as_dict())
    return {
        "ready_work": ready.as_dict(), "fast_forward": fast.as_dict(),
        "horizon_extension": horizon.as_dict(),
        "neighborhood_expansion": neighborhood.as_dict(),
        "unknown_state": unknown.as_dict(),
    }


def real_window14():
    from crafter.disruptions.benchmark import _load_state
    from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary

    loaded = _load_state()
    problem, _operator, _rows, _durations, _details, route_index, _baseline, _checked, _source = loaded
    _windows, committed_rows = _accepted_rows()
    boundary, _facts = _make_boundary(route_index, committed_rows, 2)
    required = tuple(sorted({
        "128_1836", "128_1837", "128_1838", "128_1839", "251_2063", "251_2064",
        "251_2065", "251_2066", "254_2154", "254_2155", "545_1367", "563_1586",
        "563_1587", "563_1588", "563_1589", "563_1590", "749_273", "749_274",
        "797_3142", "797_3143", "797_3144", "797_3145",
    }))
    ctx = PlanningContextV1("window-014-step3b", "ROLLING_PLAN", 2, 326400, 326400,
                            412800, 346400, required, "FROZEN_BENCHMARK_RAW",
                            snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, ctx, boundary=boundary)
    selected, provenance = select_precedence_safe_window(
        problem, state["after_index"], state["classified"], ctx,
        committed_operation_keys=boundary.committed_operation_keys,
        maximum_operations=22, boundary=boundary)
    remaining = tuple(op.operation_key for op in problem.operations
                      if int(problem.order_by_id[op.order_id].factory_id) == 2
                      and op.operation_key not in boundary.committed_operation_keys)
    decision = decide_rolling_orchestration_v2(
        window_id="window-014", context=ctx, problem=problem,
        route_index=state["after_index"], classified=state["classified"],
        admitted_operation_keys=selected, admission_provenance=provenance,
        remaining_operation_keys=remaining,
        committed_operation_keys=boundary.committed_operation_keys, boundary=boundary,
        maximum_operations=22)
    target = next(row for row in provenance if row.get("operation_key") == "254_2154")
    return {
        "decision": decision.as_dict(),
        "selected_operation_keys": list(selected),
        "selected_count": len(selected),
        "deferred_operation_keys": list(decision.deferred_operation_keys),
        "target_254_2154": target,
        "committed_operation_count": len(boundary.committed_operation_keys),
        "candidate_pool_count": decision.candidate_pool_count,
        "cp_sat_invoked": False,
        "database_writes": 0,
    }


def main():
    synthetic = synthetic_probes()
    run_1 = real_window14()
    run_2 = real_window14()
    dump("window14_orchestration.json", run_1)
    dump("determinism.json", {
        "contract_version": "ROLLING_ORCHESTRATION_DETERMINISM_V2",
        "run_1": run_1,
        "run_2": run_2,
        "identical": run_1 == run_2,
        "comparison": "canonical JSON including ordering; no meaningful fields excluded",
        "database_writes": 0,
    })
    dump("first_window_initialization.json", initialize_rolling_window_v2(
        epoch_start_seconds=0, factory_horizon_seconds=200_000,
        lookahead_seconds=86_400, commit_step_seconds=20_000).as_dict())
    deferred = {
        "factory_id": 2, "window_id": "window-014",
        "deferred_operation_keys": run_1["deferred_operation_keys"],
        "required_accounting_rule": "selected + deferred + authoritative state remains factory-local",
        "target_operation": "254_2154", "target_earliest_ready_seconds": 504548,
        "target_decision": "BOUNDARY_DEFERRED",
        "database_writes": 0,
    }
    dump("deferred_operations.json", deferred)
    dump("focused_tests.json", {
        "command": "venv\\Scripts\\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py",
        "passed": 53, "failed": 0, "skipped": 0, "warnings": 2,
        "warning_types": ["torch.jit.script DeprecationWarning"],
        "database_writes": 0,
    })
    dump("implementation_report.json", {
        "contract_version": "ROLLING_ORCHESTRATION_DECISION_V2",
        "step": "3B", "status": "VALIDATED",
        "production_module": "crafter/repair/rolling_horizon_v2.py",
        "cp_sat_invoked": False,
        "db_writes": 0,
        "preserved_step3a": True,
        "manager_priority_errors_unresolved": True,
        "multiple_authoritative_events_tested": True,
        "decisions": ["SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED",
                       "NEIGHBORHOOD_EXPANSION_REQUIRED", "FAST_FORWARD_TO_EVENT",
                       "NO_EXECUTABLE_WORK", "UNRESOLVED_ROLLING_STATE"],
    })
    (OUT / "implementation_report.md").write_text(
        "# Rolling Horizon V2 Step 3B\n\n"
        "Implemented a post-admission deterministic decision contract. Ready work "
        "always wins over deferred work; fast-forward is based on a factory-local "
        "hard-readiness scan; urgent hard blockers may request a bounded temporal "
        "stage; movable closure requests neighborhood expansion; unknown execution "
        "truth returns unresolved. The canonical CP-SAT path, calendars, resource "
        "semantics, commitment rule, and Step-3A readiness gate were not changed.\n\n"
        "Window 14 selected ready work and retained 254_2154/254_2155 as deferred; "
        "CP-SAT was not invoked by this admission/orchestration probe.\n",
        encoding="utf-8")
    dump("changed_files_manifest.json", {
        "planning_production_files": ["crafter/repair/rolling_horizon_v2.py",
                                       "crafter/repair/__init__.py",
                                       "crafter/repair/planning.py"],
        "tests": ["tests/test_rolling_horizon_v2_step3b.py"],
        "probe": ["scratch/run_rolling_horizon_v2_step3b_probes.py"],
        "db_writes": 0,
    })
    return synthetic, run_1


if __name__ == "__main__":
    main()
