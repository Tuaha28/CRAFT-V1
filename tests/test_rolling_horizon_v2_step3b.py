from datetime import datetime, timezone
from types import SimpleNamespace

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.objectives import ManagerPriorityOverrideV1
from crafter.repair.planning import PlanningContextV1
from crafter.repair.rolling_horizon_v2 import (
    FAST_FORWARD_TO_EVENT,
    HORIZON_EXTENSION_REQUIRED,
    NEIGHBORHOOD_EXPANSION_REQUIRED,
    SOLVE_READY_WORK,
    NO_EXECUTABLE_WORK,
    UNRESOLVED_ROLLING_STATE,
    decide_rolling_orchestration_v2,
    initialize_rolling_window_v2,
    no_progress_repeat_v2,
    rolling_state_signature_v2,
)


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fixture(keys=("1_a", "1_b"), *, states=None, release=0):
    orders = (OrderSpec("1", 1, release, 10_000, 1),)
    operations = tuple(
        OperationSpec(key, "1", key.split("_", 1)[1], index + 1, "sew", (1,), 60)
        for index, key in enumerate(keys)
    )
    problem = SchedulingProblem(
        "step3b", ORIGIN, 20_000,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
        orders, operations, (),
    )
    classified = {key: {"state": (states or {}).get(key, "FUTURE_RELEASED")}
                  for key in keys}
    return problem, classified


def _context(*, start=0, end=500, commit=200, selected=()):
    return PlanningContextV1(
        "step3b", "ROLLING_PLAN", 1, start, start, end, commit,
        tuple(selected), "STEP3B_FIXTURE", snapshot_as_of_seconds=0,
    )


def _route(predecessors):
    return SimpleNamespace(route_predecessor=dict(predecessors))


def _boundary(end):
    return SimpleNamespace(
        factory_id=1,
        committed_operation_keys=("1_a",),
        routing_completion_state={"1_a": {
            "operation_key": "1_a", "authoritative_end_seconds": end}},
        machine_commitments=(), worker_commitments=(),
    )


def test_ready_work_wins_over_deferred_work():
    problem, classified = _fixture(keys=("1_a", "1_b", "1_c"),
                                   states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a", "1_c": None})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(selected=("1_c",)), problem=problem,
        route_index=route, classified=classified, admitted_operation_keys=("1_c",),
        remaining_operation_keys=("1_b", "1_c"), committed_operation_keys=("1_a",),
        boundary=_boundary(700), maximum_operations=2)
    assert result.decision == SOLVE_READY_WORK
    assert result.admitted_operation_keys == ("1_c",)
    assert result.deferred_operation_keys == ("1_b",)


def test_fast_forward_is_forbidden_when_factory_local_ready_work_exists():
    problem, classified = _fixture(keys=("1_a", "1_b"))
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, admitted_operation_keys=("1_a",),
        remaining_operation_keys=("1_a", "1_b"), committed_operation_keys=(),
        boundary=None)
    assert result.decision == SOLVE_READY_WORK
    assert result.decision != FAST_FORWARD_TO_EVENT


def test_empty_factory_local_remaining_pool_is_no_executable_work():
    problem, classified = _fixture(keys=("1_a",))
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem,
        route_index=_route({"1_a": None}), classified=classified,
        remaining_operation_keys=())
    assert result.decision == NO_EXECUTABLE_WORK
    assert result.candidate_pool_count == 0


def test_zero_ready_authoritative_blocker_fast_forwards_and_preserves_snapshot_epoch():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=_boundary(700))
    assert result.decision == FAST_FORWARD_TO_EVENT
    assert result.next_window_start_seconds == 700
    assert result.provenance["snapshot_as_of_seconds"] == 0


def test_explicit_authoritative_reservation_event_is_eligible_for_fast_forward():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=_boundary(700),
        authoritative_future_events=(
            {"event_type": "HARD_RESERVATION_END", "event_time_seconds": 900,
             "operation_key": "reservation-900", "source": "reservation-ledger"},
            {"event_type": "DISRUPTION_RECOVERY", "event_time_seconds": 650,
             "operation_key": "recovery-650", "source": "recovery-ledger"},
            {"event_type": "ORDER_RELEASE", "event_time_seconds": 800,
             "operation_key": "release-800", "source": "release-ledger"},
        ))
    assert result.decision == FAST_FORWARD_TO_EVENT
    assert result.next_window_start_seconds == 650
    assert result.provenance["next_relevant_event"]["event_type"] == "DISRUPTION_RECOVERY"


def test_malformed_manager_priority_is_unresolved_not_silently_ignored():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    malformed = ({"override_id": "bad", "order_id": "1", "tier": "URGENT"},)
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=_boundary(700),
        manager_overrides=malformed)
    assert result.decision == UNRESOLVED_ROLLING_STATE
    assert result.decision_reason == "MANAGER_PRIORITY_EVALUATION_ERROR"
    assert result.trigger_blockers[0]["type"] == "MANAGER_PRIORITY_EVALUATION_ERROR"


def test_urgent_hard_blocker_requests_smallest_bounded_horizon_stage():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    override = ManagerPriorityOverrideV1(
        "urgent-1", "1", "URGENT", 0, reason_code="RUSH",
        entered_by="test", entered_at=0, revision=1)
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=_boundary(550),
        manager_overrides=(override,), temporal_extension_stages_seconds=(600, 900))
    assert result.decision == HORIZON_EXTENSION_REQUIRED
    assert result.extended_window_end_seconds == 600
    assert result.provenance["minimum_required_extension_seconds"] == 50


def test_nonurgent_deferred_work_does_not_extend_horizon():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=_boundary(550),
        temporal_extension_stages_seconds=(600, 900))
    assert result.decision == FAST_FORWARD_TO_EVENT
    assert result.extended_window_end_seconds is None


def test_movable_closure_over_cap_requests_neighborhood_expansion():
    problem, classified = _fixture()
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",), maximum_operations=1)
    assert result.decision == NEIGHBORHOOD_EXPANSION_REQUIRED
    blocker = result.trigger_blockers[0]
    assert blocker["type"] == "MOVABLE_PREDECESSOR_CLOSURE"
    assert blocker["required_minimum_closure_size"] == 2


def test_movable_closure_within_cap_is_normal_admission():
    problem, classified = _fixture()
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, admitted_operation_keys=(),
        remaining_operation_keys=("1_a", "1_b"), maximum_operations=2)
    assert result.decision == SOLVE_READY_WORK
    assert result.admitted_operation_keys == ("1_a", "1_b")
    assert result.deferred_operation_keys == ()


def test_unknown_execution_state_prevents_fast_forward():
    problem, classified = _fixture(states={"1_a": "UNKNOWN"})
    classified["1_a"]["end_seconds"] = 100  # stale planned data is not authoritative
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",))
    assert result.decision == UNRESOLVED_ROLLING_STATE
    assert result.next_window_start_seconds is None


def test_missing_authoritative_completion_is_unresolved_not_an_exception():
    problem, classified = _fixture(states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a"})
    result = decide_rolling_orchestration_v2(
        window_id="w", context=_context(), problem=problem, route_index=route,
        classified=classified, remaining_operation_keys=("1_b",),
        committed_operation_keys=("1_a",), boundary=None)
    assert result.decision == UNRESOLVED_ROLLING_STATE
    assert result.trigger_blockers[0]["type"] == "UNKNOWN_EXECUTION_STATE"


def test_first_window_initialization_has_no_hidden_special_case():
    first = initialize_rolling_window_v2(
        epoch_start_seconds=0, factory_horizon_seconds=200_000,
        lookahead_seconds=86_400, commit_step_seconds=20_000)
    assert first.window_start_seconds == 0
    assert first.window_end_seconds == 86_400
    assert first.commit_end_seconds == 20_000
    assert initialize_rolling_window_v2(
        epoch_start_seconds=20_000, factory_horizon_seconds=200_000,
        lookahead_seconds=86_400, commit_step_seconds=20_000).commit_end_seconds == 40_000


def test_repeated_no_progress_signature_is_detected():
    problem, classified = _fixture(states={"1_a": "UNKNOWN"})
    route = _route({"1_a": None, "1_b": "1_a"})
    context = _context()
    from crafter.repair.rolling_horizon_v2 import scan_factory_local_readiness_v2
    scan = scan_factory_local_readiness_v2(problem, route, classified, context, ("1_b",))
    signature = rolling_state_signature_v2(
        factory_id=1, remaining_operation_keys=("1_b",), committed_operation_keys=(),
        context=context, scan=scan, decision=UNRESOLVED_ROLLING_STATE)
    assert no_progress_repeat_v2((signature,), signature) is True
    assert no_progress_repeat_v2((signature,), signature, state_changed=True) is False


def test_repeated_orchestration_from_same_frozen_state_is_deterministic():
    problem, classified = _fixture(keys=("1_a", "1_b", "1_c"),
                                   states={"1_a": "COMMITTED_FUTURE"})
    route = _route({"1_a": None, "1_b": "1_a", "1_c": None})
    kwargs = dict(
        window_id="w", context=_context(selected=("1_c",)), problem=problem,
        route_index=route, classified=classified, admitted_operation_keys=("1_c",),
        remaining_operation_keys=("1_b", "1_c"), committed_operation_keys=("1_a",),
        boundary=_boundary(700), maximum_operations=2)
    first = decide_rolling_orchestration_v2(**kwargs).as_dict()
    second = decide_rolling_orchestration_v2(**kwargs).as_dict()
    assert first == second


def test_window14_real_reconstruction_solves_ready_work_and_retains_deferred_keys():
    from crafter.disruptions.benchmark import _load_state
    from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary
    from crafter.repair.planning import build_planning_state, select_precedence_safe_window

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
    context = PlanningContextV1(
        "window-014-step3b", "ROLLING_PLAN", 2, 326400, 326400, 412800, 346400,
        required, "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, context, boundary=boundary)
    selected, provenance = select_precedence_safe_window(
        problem, state["after_index"], state["classified"], context,
        committed_operation_keys=boundary.committed_operation_keys,
        maximum_operations=22, boundary=boundary)
    remaining = tuple(
        op.operation_key for op in problem.operations
        if int(problem.order_by_id[op.order_id].factory_id) == 2
        and op.operation_key not in boundary.committed_operation_keys
    )
    result = decide_rolling_orchestration_v2(
        window_id="window-014", context=context, problem=problem,
        route_index=state["after_index"], classified=state["classified"],
        admitted_operation_keys=selected, admission_provenance=provenance,
        remaining_operation_keys=remaining,
        committed_operation_keys=boundary.committed_operation_keys, boundary=boundary,
        maximum_operations=22)
    assert result.decision == SOLVE_READY_WORK
    assert "254_2154" in result.deferred_operation_keys
    assert "254_2155" in result.deferred_operation_keys
    assert result.ready_candidate_count > 0
    assert result.provenance["snapshot_as_of_seconds"] == 0
