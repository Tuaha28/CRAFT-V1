from crafter.repair.temporal_escalation import (
    TemporalEscalationPolicyV3,
    _retryable_temporal_failure,
    run_temporal_escalated_planning_schedule,
)


def _failed(status="CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT"):
    return {
        "acceptance": {
            "status": "REJECTED",
            "stages": [{"status": "INFEASIBLE"}],
            "winning_stage": None,
            "selected_schedule": [],
            "materialization": {"mismatch_count": 0},
        },
        "resource_domain_v2": {
            "rounds": [{
                "cp_stage_statuses": ["INFEASIBLE"],
                "resource_domain_status": status,
                "diagnosis": {
                    "status": status,
                    "full_domain_probe": {"feasible": True},
                },
            }]
        },
    }


def test_temporal_policy_is_finite_and_first_plan_is_historical_default():
    policy = TemporalEscalationPolicyV3()
    assert len(policy.plans) == 3
    assert policy.plans[0][1] == (2, 7, 180)
    assert policy.plans[-1][-1][1] is None


def test_temporal_controller_retries_only_the_bounded_stage_case(monkeypatch):
    calls = []
    outcomes = iter((_failed(), {
        "acceptance": {
            "status": "REJECTED",
            "stages": [{"status": "OPTIMAL"}],
            "winning_stage": {"status": "OPTIMAL", "stage": 4},
            "selected_schedule": [{"operation_key": "A"}],
            "materialization": {"mismatch_count": 0},
        },
        "resource_domain_v2": {"rounds": [{"resource_domain_status": "CANDIDATE_READY"}]},
    }))

    def fake(state, **kwargs):
        calls.append(kwargs["canonical_kwargs"]["plan"])
        return next(outcomes)

    monkeypatch.setattr(
        "crafter.repair.temporal_escalation.run_adaptive_planning_schedule", fake)
    result = run_temporal_escalated_planning_schedule({}, resource_domain_policy=None)
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert result["temporal_escalation"]["winning_attempt"] == 2


def test_temporal_controller_does_not_retry_non_temporal_failure(monkeypatch):
    calls = []
    result = _failed("NON_RESOURCE_INFEASIBILITY")

    def fake(state, **kwargs):
        calls.append(kwargs["canonical_kwargs"]["plan"])
        return result

    monkeypatch.setattr(
        "crafter.repair.temporal_escalation.run_adaptive_planning_schedule", fake)
    output = run_temporal_escalated_planning_schedule({})
    assert len(calls) == 1
    assert output["temporal_escalation"]["status"] == "TEMPORAL_STAGE_ESCALATION_LIMIT"


def test_temporal_failure_requires_exact_exposed_domain_probe():
    failed = _failed()
    assert _retryable_temporal_failure(failed)
    failed["resource_domain_v2"]["rounds"][0]["diagnosis"]["full_domain_probe"]["feasible"] = False
    assert not _retryable_temporal_failure(failed)

