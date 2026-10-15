from crafter.repair.resource_domain_v3 import (
    ResourceDomainPolicyV3,
    _candidate_quality_targets,
    run_v3_planning_schedule,
)


def test_quality_targets_are_deterministic_and_truncated_only():
    result = {
        "resource_audit": [
            {"operation_key": "B", "truncated": True,
             "total_resource_configurations": 2, "domain_rows": []},
            {"operation_key": "A", "truncated": False,
             "total_resource_configurations": 9, "domain_rows": []},
        ],
        "acceptance": {"selected_schedule": [{"operation_key": "B", "start_seconds": 10}]},
    }
    assert _candidate_quality_targets(result, 8) == ("B",)


def test_v3_retains_valid_incumbent_when_no_validator_is_supplied():
    incumbent = {
        "state": {},
        "acceptance": {"selected_schedule": [{"operation_key": "A"}]},
        "resource_audit": [],
    }
    # The compact incumbent must never be treated as a production-accepted
    # V3 result without the planning scoped acceptance callback.
    import crafter.repair.resource_domain_v3 as v3
    v3.run_temporal_escalated_planning_schedule = lambda *a, **k: incumbent
    result = run_v3_planning_schedule({}, policy=ResourceDomainPolicyV3())
    assert result["resource_domain_v3"]["status"] == "INCUMBENT_REQUIRES_SCOPED_ACCEPTANCE"


def test_v3_does_not_call_truncated_failure_physical_infeasibility(monkeypatch):
    failed = {
        "acceptance": {"selected_schedule": [], "stages": [{"status": "INFEASIBLE"}]},
        "resource_domain_v2": {"rounds": [{
            "candidate_domain": {"unused_worker_alternatives": ["A"],
                                  "unused_machine_alternatives": []},
            "resource_domain_status": "NON_RESOURCE_INFEASIBILITY",
        }]},
    }
    recovered = {
        "acceptance": {"selected_schedule": [{"operation_key": "A"}],
                        "winning_stage": {"status": "OPTIMAL"}},
        "resource_domain_v2": {"rounds": []},
    }
    calls=[]
    def fake(*args, **kwargs):
        calls.append(kwargs["resource_domain_policy"].initial_worker_crews)
        return failed if len(calls) == 1 else recovered
    monkeypatch.setattr("crafter.repair.resource_domain_v3.run_temporal_escalated_planning_schedule", fake)
    result = run_v3_planning_schedule({}, policy=ResourceDomainPolicyV3(max_expansion_rounds=1))
    assert calls == [1, 2]
    assert result["acceptance"]["selected_schedule"]


def test_global_planner_exposes_v3_controller(monkeypatch):
    from crafter.repair.global_planner import GlobalCRAFTPlanner, RollingWindow
    calls=[]
    def fake(state, **kwargs):
        calls.append(kwargs["policy"])
        return {"state": state, "acceptance": {"status": "REJECTED", "selected_schedule": []}}
    monkeypatch.setattr("crafter.repair.resource_domain_v3.run_v3_planning_schedule", fake)
    planner=GlobalCRAFTPlanner(resource_domain_v3_policy=ResourceDomainPolicyV3())
    result=planner.plan_factory_window(
        window=RollingWindow(1, 0, 100, 50, "w1"), prepared={"state": {"problem": None}})
    assert calls and isinstance(calls[0], ResourceDomainPolicyV3)
    assert result["commit_status"] == "REJECTED_NOT_COMMITTED"


def test_global_planner_v3_wires_scoped_incumbent_validator(monkeypatch):
    from crafter.repair.global_planner import GlobalCRAFTPlanner, RollingWindow
    captured = {}

    def fake_validate(result, state, **kwargs):
        captured["required"] = kwargs["required_operation_keys"]
        captured["future"] = kwargs["future_outside_operation_keys"]
        return {"accepted": True, "scope_status": "SCOPE_VALID",
                "replay_status": "PASS", "physical_status": "VALID"}

    monkeypatch.setattr(
        "crafter.repair.scoped_validation.validate_canonical_planning_candidate",
        fake_validate)

    def fake_v3(state, **kwargs):
        captured["validator"] = kwargs.get("incumbent_validator")
        return {"state": state, "acceptance": {"selected_schedule": []}}

    monkeypatch.setattr("crafter.repair.resource_domain_v3.run_v3_planning_schedule", fake_v3)
    state = {"problem": None, "keys": ("A",), "classified": {}}
    planner = GlobalCRAFTPlanner(resource_domain_v3_policy=ResourceDomainPolicyV3())
    planner.plan_factory_window(
        window=RollingWindow(1, 0, 100, 50, "w1"),
        prepared={"state": state, "required_window_keys": ("A",)})
    assert callable(captured["validator"])
    assert captured["validator"]({"state": state, "acceptance": {}})
    assert captured["required"] == ("A",)
    assert captured["future"] == ()


def test_quality_diagnostics_are_schedule_aware_and_deterministic():
    from types import SimpleNamespace
    from crafter.repair.resource_domain_v3 import _incumbent_quality_diagnostics
    order = SimpleNamespace(release_seconds=10, deadline_seconds=100)
    op = SimpleNamespace(order_id="O", operation_key="A")
    problem = SimpleNamespace(operation_by_key={"A": op}, order_by_id={"O": order})
    after = SimpleNamespace(route_predecessor={"A": None}, route_successor={"A": None})
    result = {"state": {"problem": problem, "after_index": after},
              "acceptance": {"selected_schedule": [{
                  "operation_key": "A", "start_seconds": 40, "end_seconds": 120,
                  "resource_configuration": {"machine_ids": (1,), "worker_ids": ("w",)}}]}}
    rows = _incumbent_quality_diagnostics(result)
    assert rows[0]["waiting_seconds"] == 30
    assert rows[0]["tardiness_seconds"] == 20
    assert rows[0]["due_slack_seconds"] == -20


def test_scoped_quality_validator_uses_pre_solve_required_keys(monkeypatch):
    from types import SimpleNamespace
    import crafter.repair.scoped_validation as scoped
    op_a = SimpleNamespace(order_id="O", operation_key="A")
    op_b = SimpleNamespace(order_id="O", operation_key="B")
    problem = SimpleNamespace(operation_by_key={"A": op_a, "B": op_b},
                              order_by_id={"O": SimpleNamespace(release_seconds=0,
                                                                 deadline_seconds=None)})
    state = {"problem": problem}
    seen = {}

    def fake_window_validation(_state, phenotype):
        seen["assignment_keys"] = phenotype.assignment_keys
        complete = set(phenotype.assignment_keys) == {"A", "B"}
        return {"accepted": complete, "scope_status": "SCOPE_VALID" if complete else "SCOPE_INVALID",
                "replay_status": "PASS" if complete else "NOT_RUN",
                "physical_status": "VALID" if complete else "NOT_RUN"}

    monkeypatch.setattr(scoped, "validate_planning_window", fake_window_validation)
    result = {"acceptance": {"selected_schedule": [{
        "operation_key": "A", "start_seconds": 0, "end_seconds": 10,
        "resource_configuration": {"factory_id": 1, "line_id": 1, "k": 1,
                                    "machine_ids": (1,), "worker_ids": ("w",),
                                    "setup_seconds": 0, "productive_seconds": 10}}]}}
    checked = scoped.validate_canonical_planning_candidate(
        result, state, required_operation_keys=("A", "B"),
        future_outside_operation_keys=("C",))
    assert seen["assignment_keys"] == ("A",)
    assert checked["accepted"] is False
    assert checked["required_operation_keys"] == ("A", "B")
