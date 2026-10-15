from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.planning import (
    PlanningContextV1,
    boundary_readiness_for_operation, build_planning_state,
    select_precedence_safe_window,
)


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixture(keys=("1_a", "1_b"), *, states=None, authoritative=None,
             releases=None, deadline=10_000):
    releases = releases or {}
    orders = (OrderSpec("1", 1, 0, deadline, 1),)
    operations = tuple(
        OperationSpec(key, "1", key.split("_", 1)[1], i + 1, "sew", (1,), 60)
        for i, key in enumerate(keys)
    )
    problem = SchedulingProblem(
        "step3a", ORIGIN, 20_000,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
        orders, operations, (),
    )
    classified = {
        key: {"state": (states or {}).get(key, "FUTURE_RELEASED")}
        for key in keys
    }
    for key, value in (authoritative or {}).items():
        classified.setdefault(key, {}).update(value)
    for key, value in releases.items():
        classified.setdefault(key, {})["release_seconds"] = value
    return problem, classified


def _context(selected=("1_b",), end=500):
    return PlanningContextV1(
        "step3a", "ROLLING_PLAN", 1, 0, 0, end, 300,
        tuple(selected), "STEP3A_FIXTURE", snapshot_as_of_seconds=0,
    )


def _route(predecessor="1_a"):
    return SimpleNamespace(route_predecessor={"1_a": None, "1_b": predecessor})


def _boundary(end):
    return SimpleNamespace(
        factory_id=1,
        committed_operation_keys=("1_a",),
        routing_completion_state={"1_a": {
            "operation_key": "1_a",
            "authoritative_end_seconds": end,
        }},
        machine_commitments=(), worker_commitments=(),
    )


def test_committed_predecessor_before_window_end_allows_successor():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    keys, provenance = select_precedence_safe_window(
        problem, _route(), classified, _context(),
        committed_operation_keys=("1_a",), maximum_operations=1,
        boundary=_boundary(499),
    )
    assert keys == ("1_b",)
    assert any(row["selected"] for row in provenance)


def test_committed_predecessor_at_window_end_is_deferred_half_open():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    result = boundary_readiness_for_operation(
        problem, _route(), classified, _context(), "1_b",
        committed_operation_keys=("1_a",), boundary=_boundary(500),
    )
    assert result.decision == "BOUNDARY_DEFERRED"
    assert result.admissible_now is False
    assert result.future_schedulable is True
    assert result.is_hard_boundary is False


def test_committed_predecessor_after_window_end_is_deferred():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    keys, provenance = select_precedence_safe_window(
        problem, _route(), classified, _context(),
        committed_operation_keys=("1_a",), maximum_operations=1,
        boundary=_boundary(600),
    )
    assert keys == ()
    decision = next(row for row in provenance if row["operation_key"] == "1_b")
    assert decision["reason"] == "BOUNDARY_DEFERRED"
    assert decision["readiness"]["earliest_ready_seconds"] == 600


def test_provisional_predecessor_remains_movable_closure():
    problem, classified = _fixture(states={"1_a": "FUTURE_RELEASED"})
    keys, _ = select_precedence_safe_window(
        problem, _route(), classified, _context(),
        committed_operation_keys=(), maximum_operations=2,
    )
    assert keys == ("1_a", "1_b")


def test_completed_predecessor_uses_explicit_authoritative_completion():
    problem, classified = _fixture(
        states={"1_a": "COMPLETED"},
        authoritative={"1_a": {"authoritative_end_seconds": 400, "end_seconds": 9_999}},
    )
    result = boundary_readiness_for_operation(
        problem, _route(), classified, _context(), "1_b",
        committed_operation_keys=(), boundary=None,
    )
    assert result.decision == "ADMIT"
    assert result.earliest_ready_seconds == 400


def test_in_progress_predecessor_uses_authoritative_completion():
    problem, classified = _fixture(
        states={"1_a": "IN_PROGRESS"},
        authoritative={"1_a": {"authoritative_end_seconds": 600}},
    )
    result = boundary_readiness_for_operation(
        problem, _route(), classified, _context(), "1_b",
        committed_operation_keys=(), boundary=None,
    )
    assert result.decision == "BOUNDARY_DEFERRED"
    assert result.blockers[0]["type"] == "IN_PROGRESS_PREDECESSOR_COMPLETION"


def test_unknown_predecessor_does_not_use_stale_planned_end():
    problem, classified = _fixture(
        states={"1_a": "UNKNOWN"},
        authoritative={"1_a": {"end_seconds": 100}},
    )
    result = boundary_readiness_for_operation(
        problem, _route(), classified, _context(), "1_b",
        committed_operation_keys=(), boundary=None,
    )
    assert result.decision == "UNRESOLVED_EXECUTION_STATE"
    assert result.admissible_now is False
    assert result.earliest_ready_seconds == 0


def test_deferred_operation_remains_future_accounting_and_not_hard_boundary():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    result = boundary_readiness_for_operation(
        problem, _route(), classified, _context(), "1_b",
        committed_operation_keys=("1_a",), boundary=_boundary(600),
    )
    assert result.operation_key == "1_b"
    assert result.future_schedulable is True
    assert result.is_hard_boundary is False
    assert "1_b" in {op.operation_key for op in problem.operations}


def test_unrelated_ready_operation_is_still_admitted():
    problem, classified = _fixture(
        keys=("1_a", "1_b", "1_c"),
        states={"1_a": "COMMITTED_FUTURE", "1_b": "FUTURE_RELEASED", "1_c": "FUTURE_RELEASED"},
    )
    route = SimpleNamespace(route_predecessor={"1_a": None, "1_b": "1_a", "1_c": None})
    keys, provenance = select_precedence_safe_window(
        problem, route, classified, _context(("1_b", "1_c")),
        committed_operation_keys=("1_a",), maximum_operations=1,
        boundary=_boundary(600),
    )
    assert keys == ("1_c",)
    assert any(row["operation_key"] == "1_b" and row["reason"] == "BOUNDARY_DEFERRED" for row in provenance)


def test_window14_real_boundary_defers_254_2154():
    from crafter.disruptions.benchmark import _load_state
    from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary

    loaded = _load_state()
    problem, _operator, _rows, _durations, _details, route_index, _baseline, _checked, _source = loaded
    windows, committed_rows = _accepted_rows()
    boundary, _facts = _make_boundary(route_index, committed_rows, 2)
    required = tuple(json_key for json_key in [
        "128_1836", "128_1837", "128_1838", "128_1839", "251_2063", "251_2064",
        "251_2065", "251_2066", "254_2154", "254_2155", "545_1367", "563_1586",
        "563_1587", "563_1588", "563_1589", "563_1590", "749_273", "749_274",
        "797_3142", "797_3143", "797_3144", "797_3145",
    ])
    context = PlanningContextV1(
        "window-014-step3a", "ROLLING_PLAN", 2, 326400, 326400, 412800, 346400,
        required, "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0,
    )
    state = build_planning_state(loaded, context, boundary=boundary)
    selected, provenance = select_precedence_safe_window(
        problem, state["after_index"], state["classified"], context,
        committed_operation_keys=boundary.committed_operation_keys,
        maximum_operations=22, boundary=boundary,
    )
    assert "254_2154" not in selected
    row = next(row for row in provenance if row["operation_key"] == "254_2154")
    assert row["reason"] == "BOUNDARY_DEFERRED"
    assert row["readiness"]["earliest_ready_seconds"] == 504548
    assert row["readiness"]["is_hard_boundary"] is False
    assert selected


def _independent_window14_admission_output():
    """Rebuild every mutable input independently for deterministic comparison."""
    from crafter.disruptions.benchmark import _load_state
    from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary

    loaded = _load_state()
    problem, _operator, _rows, _durations, _details, route_index, _baseline, _checked, _source = loaded
    _windows, committed_rows = _accepted_rows()
    boundary, _facts = _make_boundary(route_index, committed_rows, 2)
    diagnosis = json.loads(Path(
        "outputs/crafter/resource_domain_v3/quality_validation_v1/window14_diagnosis.json"
    ).read_text(encoding="utf-8"))
    required = tuple(sorted(str(key) for key in diagnosis["selected_required_keys"]))
    context = PlanningContextV1(
        "window-014-determinism", "ROLLING_PLAN", 2, 326400, 326400, 412800, 346400,
        required, "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0,
    )
    state = build_planning_state(loaded, context, boundary=boundary)
    selected, provenance = select_precedence_safe_window(
        problem, state["after_index"], state["classified"], context,
        committed_operation_keys=boundary.committed_operation_keys,
        maximum_operations=22, boundary=boundary,
    )
    deferred = [row for row in provenance if row.get("reason") == "BOUNDARY_DEFERRED"]
    readiness = [{
        "operation_key": row["operation_key"],
        "decision": row["readiness"]["decision"],
        "earliest_ready_seconds": row["readiness"]["earliest_ready_seconds"],
        "blockers": row["readiness"]["blockers"],
    } for row in provenance if row.get("readiness")]
    return {
        "selected_keys": list(selected),
        "deferred_keys_in_provenance_order": [row["operation_key"] for row in deferred],
        "deferred_keys_sorted": sorted(row["operation_key"] for row in deferred),
        "readiness_decisions_in_provenance_order": readiness,
        "admission_provenance": provenance,
        "required_predecessor_lists": [
            {"operation_key": row["operation_key"],
             "required_predecessors": row.get("required_predecessors", [])}
            for row in provenance
        ],
        "window": {
            "window_start_seconds": 326400,
            "window_end_seconds": 412800,
            "commit_end_seconds": 346400,
            "committed_operation_count": len(boundary.committed_operation_keys),
        },
        "cp_sat_invoked": False,
        "database_writes": 0,
    }


def test_window14_admission_is_deterministic_across_independent_reconstruction():
    run_1 = _independent_window14_admission_output()
    run_2 = _independent_window14_admission_output()
    assert json.dumps(run_1, sort_keys=True) == json.dumps(run_2, sort_keys=True)
    assert run_1 == run_2
    assert "254_2154" not in run_1["selected_keys"]
    target = next(row for row in run_1["readiness_decisions_in_provenance_order"]
                  if row["operation_key"] == "254_2154")
    assert target["decision"] == "BOUNDARY_DEFERRED"
    assert target["earliest_ready_seconds"] == 504548
    assert target["blockers"] == [{
        "type": "COMMITTED_PREDECESSOR_COMPLETION",
        "operation_key": "254_2153",
        "authoritative_end_seconds": 504548,
        "source": "boundary.routing_completion_state",
    }]
    assert run_1["selected_keys"]
