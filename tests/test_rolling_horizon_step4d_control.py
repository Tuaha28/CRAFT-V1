from crafter.repair.rolling_horizon_v2 import (
    FAST_FORWARD_TO_EVENT,
    HORIZON_EXTENSION_REQUIRED,
    NEIGHBORHOOD_EXPANSION_REQUIRED,
    NO_EXECUTABLE_WORK,
    SOLVE_READY_WORK,
    UNRESOLVED_ROLLING_STATE,
    RollingOrchestrationDecisionV2,
    advance_zero_commit_rolling_epoch,
    apply_rolling_orchestration_decision_v2,
)


def _decision(kind, **kwargs):
    return RollingOrchestrationDecisionV2(
        window_id="window-test", window_start_seconds=100,
        window_end_seconds=200, commit_end_seconds=140,
        decision=kind, **kwargs)


def test_step4d_solve_ready_work_routes_admitted_keys_to_solver():
    outcome = apply_rolling_orchestration_decision_v2(
        _decision(SOLVE_READY_WORK, admitted_operation_keys=("b", "a")),
        normal_operation_cap=22, maximum_operation_cap=70, commit_step_seconds=20)
    assert outcome["action"] == SOLVE_READY_WORK
    assert outcome["selected_operation_keys"] == ("b", "a")


def test_step4d_horizon_extension_keeps_commit_step_and_does_not_move_boundary():
    outcome = apply_rolling_orchestration_decision_v2(
        _decision(HORIZON_EXTENSION_REQUIRED, extended_window_end_seconds=360),
        normal_operation_cap=22, maximum_operation_cap=70, commit_step_seconds=20)
    assert outcome["window_end_seconds"] == 360
    assert outcome["commit_end_seconds"] == 120


def test_step4d_neighborhood_request_is_structurally_bounded():
    outcome = apply_rolling_orchestration_decision_v2(
        _decision(NEIGHBORHOOD_EXPANSION_REQUIRED, trigger_operation_keys=("a", "b", "c"),
                  trigger_blockers=({"required_minimum_closure_size": 35},)),
        normal_operation_cap=22, maximum_operation_cap=50, commit_step_seconds=20)
    assert outcome["requested_operation_cap"] == 35


def test_step4d_fast_forward_requires_strictly_future_event():
    outcome = apply_rolling_orchestration_decision_v2(
        _decision(FAST_FORWARD_TO_EVENT, next_window_start_seconds=300),
        normal_operation_cap=22, maximum_operation_cap=70, commit_step_seconds=20)
    assert outcome == {"action": FAST_FORWARD_TO_EVENT, "next_window_start_seconds": 300}
    invalid = apply_rolling_orchestration_decision_v2(
        _decision(FAST_FORWARD_TO_EVENT, next_window_start_seconds=100),
        normal_operation_cap=22, maximum_operation_cap=70, commit_step_seconds=20)
    assert invalid["action"] == UNRESOLVED_ROLLING_STATE


def test_step4d_terminal_actions_remain_terminal():
    for action in (NO_EXECUTABLE_WORK, UNRESOLVED_ROLLING_STATE):
        outcome = apply_rolling_orchestration_decision_v2(
            _decision(action, decision_reason="truthful-stop"),
            normal_operation_cap=22, maximum_operation_cap=70, commit_step_seconds=20)
        assert outcome["action"] == action


def test_zero_commit_valid_provisional_plan_advances_epoch_instead_of_failing():
    outcome = advance_zero_commit_rolling_epoch(
        window_start_seconds=100, commit_end_seconds=140,
        provisional_operation_keys=("b", "a"))
    assert outcome["action"] == FAST_FORWARD_TO_EVENT
    assert outcome["next_window_start_seconds"] == 140
    assert outcome["provisional_operation_keys"] == ["a", "b"]


def test_zero_commit_guard_stops_when_epoch_cannot_advance_or_plan_is_empty():
    assert advance_zero_commit_rolling_epoch(
        window_start_seconds=100, commit_end_seconds=100,
        provisional_operation_keys=("a",))["action"] == UNRESOLVED_ROLLING_STATE
    assert advance_zero_commit_rolling_epoch(
        window_start_seconds=100, commit_end_seconds=140,
        provisional_operation_keys=())["action"] == UNRESOLVED_ROLLING_STATE
