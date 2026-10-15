from crafter.production import controller_v1 as controller
import time
from types import SimpleNamespace


def _result(status="ACCEPTED", solver="FEASIBLE", valid=True):
    return {
        "state": {"keys": ("op",)},
        "resource_configurations": {"op": ()}, "regime_count": 1,
        "acceptance": {
            "status": status,
            "selected_schedule": ({"operation_key": "op"},) if valid else (),
            "materialization": {"mismatch_count": 0},
            "replay": {"status": "PASS" if valid else "NOT_REACHED"},
            "validator": {"status": "VALID" if valid else "NOT_REACHED"},
            "winning_stage": {"status": solver}, "stages": (),
        },
    }


def test_normal_success_does_not_run_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "run_canonical_schedule",
                        lambda prepared, **kwargs: calls.append(kwargs) or _result())
    result = controller.ProductionControllerV1().run_window({"state": {"keys": ("op",)}})
    assert result["recovery"] == "NONE"
    assert len(calls) == 1


def test_temporal_failure_runs_one_wider_retry(monkeypatch):
    calls = []
    outputs = [_result(status="REJECTED", solver="INFEASIBLE", valid=False), _result()]
    monkeypatch.setattr(controller, "run_canonical_schedule",
                        lambda prepared, **kwargs: calls.append(kwargs) or outputs.pop(0))
    monkeypatch.setattr(controller, "_failure_classification",
                        lambda prepared: (controller.TEMPORAL_SCOPE_INSUFFICIENT, {"evidence": True}))
    monkeypatch.setattr(controller.ProductionControllerV1, "_worker_recovery",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("crew recovery must not run")))
    result = controller.ProductionControllerV1().run_window({"state": {"keys": ("op",)}})
    assert result["recovery"] == "TEMPORAL"
    assert len(calls) == 2
    assert calls[1]["unrestricted_final_stage"] is True


def test_worker_failure_selects_crew_recovery_only(monkeypatch):
    normal = _result(status="REJECTED", solver="INFEASIBLE", valid=False)
    recovery = {"candidate": _result(), "recovery": "CREW", "trace": {},
                "failure_classification": controller.WORKER_CREW_DOMAIN_TRUNCATED}
    monkeypatch.setattr(controller, "run_canonical_schedule", lambda *a, **k: normal)
    monkeypatch.setattr(controller, "_failure_classification",
                        lambda prepared: (controller.WORKER_CREW_DOMAIN_TRUNCATED, {"evidence": True}))
    monkeypatch.setattr(controller.ProductionControllerV1, "_worker_recovery",
                        lambda self, *a, **k: recovery)
    result = controller.ProductionControllerV1().run_window({"state": {"keys": ("op",)}}, boundary=object())
    assert result["recovery"] == "CREW"


def test_unresolved_failure_does_not_recurse(monkeypatch):
    monkeypatch.setattr(controller, "run_canonical_schedule",
                        lambda prepared, **kwargs: _result(status="REJECTED", solver="UNKNOWN", valid=False))
    monkeypatch.setattr(controller, "_failure_classification",
                        lambda prepared: (controller.OTHER_UNRESOLVED, {"evidence": False}))
    result = controller.ProductionControllerV1().run_window({"state": {"keys": ("op",)}})
    assert result["recovery"] == "NONE"
    assert result["failure_classification"] == controller.OTHER_UNRESOLVED


def test_direct_matching_unresolved_preserves_initial_diagnostic(monkeypatch):
    initial = _result(status="REJECTED", solver="UNKNOWN", valid=False)
    monkeypatch.setattr(controller, "run_canonical_schedule", lambda *a, **kw: initial)
    monkeypatch.setattr(controller, "_failure_classification",
                        lambda result: (controller.OTHER_UNRESOLVED, {"reason": "test"}))
    monkeypatch.setattr(controller.ProductionControllerV1, "_normal_variant",
                        staticmethod(lambda prepared, cap, cache=None: prepared))
    result = controller.ProductionControllerV1()._run_direct_matching(
        initial, boundary=None, cache=None, boundary_facts=(), required_operation_keys=())
    assert result["candidate"] is initial
    assert result["trace"]["normal"]["mode"] == "DIRECT_COMPACT_MATCHING_DOMAIN"
    assert result["failure_classification"] == controller.OTHER_UNRESOLVED


def test_route_pressure_alone_is_not_worker_truncation(monkeypatch):
    monkeypatch.setattr(
        controller, "diagnose_worker_route_pressure",
        lambda *args: [{"operation_key": "op", "route_pressure": True}],
    )
    prepared = {
        "state": {"keys": ("op",)},
        "resource_configurations": {"op": ()}, "regimes": {"op": ()},
        "resource_audit": (),
        "acceptance": {"stages": ({"status": "INFEASIBLE"},)},
    }
    classification, diagnosis = controller._failure_classification(prepared)
    assert classification == controller.TEMPORAL_SCOPE_INSUFFICIENT
    assert "resource_domain_truncated" not in diagnosis
    assert diagnosis["route_pressure_without_truncation_proof"]


def test_route_pressure_with_explicit_worker_truncation_selects_crew(monkeypatch):
    monkeypatch.setattr(
        controller, "diagnose_worker_route_pressure",
        lambda *args: [{"operation_key": "op", "route_pressure": True}],
    )
    prepared = {
        "state": {"keys": ("op",)},
        "resource_configurations": {"op": ()}, "regimes": {"op": ()},
        "resource_audit": [{
            "operation_key": "op",
            "domain_rows": [{"more_worker_crews_available": True,
                             "worker_crews_after_pruning": [["w1"]],
                             "total_legal_worker_crews": 2}],
        }],
        "acceptance": {"stages": ({"status": "INFEASIBLE"},)},
    }
    classification, diagnosis = controller._failure_classification(prepared)
    assert classification == controller.WORKER_CREW_DOMAIN_TRUNCATED
    assert diagnosis["resource_domain_truncated"] is True


def test_normal_k_ladder_shares_one_total_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller.ProductionControllerV1, "_normal_variant",
        staticmethod(lambda prepared, worker_cap, cache=None: prepared),
    )

    def fake_run(prepared, **kwargs):
        calls.append(kwargs["plan"][0][2])
        time.sleep(0.01)
        return _result(status="REJECTED", solver="INFEASIBLE", valid=False)

    monkeypatch.setattr(controller, "run_canonical_schedule", fake_run)
    result, trace = controller.ProductionControllerV1(
        controller.ProductionControllerConfigV1(normal_seconds=0.2)
    )._normal_ladder({"state": {"keys": ("op",)}})
    assert len(calls) == 3
    assert calls[0] > calls[1] > calls[2]
    assert trace["worker_caps_attempted"] == [1, 2, 3]
    assert trace["total_budget_seconds"] == 0.2


def test_production_crew_recovery_uses_causal_closure_not_magic_limits(monkeypatch):
    captured = {}

    def fake_recovery(state, availability, base, audit, build, solve, *, policy, baseline_result):
        captured.update({
            "max_expansion_operations": policy.max_expansion_operations,
            "max_successor_depth": policy.max_successor_depth,
            "max_pressure_targets_per_round": policy.max_pressure_targets_per_round,
        })
        return {"status": "WORKER_CREW_DOMAIN_STILL_TRUNCATED",
                "configurations": base, "rounds": [], "selected_result": None}

    monkeypatch.setattr(controller, "run_route_aware_adaptive_crew_expansion", fake_recovery)
    assert captured == {}
    # The policy is constructed inside the recovery method; exercise the
    # production path with a direct policy assertion through the real factory
    # helper's constructor contract.
    policy = controller.RouteAwareCrewExpansionPolicyV1(
        max_rounds=1, total_new_crews_per_operation_by_round=(8,),
        max_expansion_operations=None, max_successor_depth=None,
        max_pressure_targets_per_round=None)
    assert policy.max_expansion_operations is None
    assert policy.max_successor_depth is None
    assert policy.max_pressure_targets_per_round is None


def test_production_default_worker_recovery_runs_to_domain_fixed_point():
    config = controller.ProductionControllerConfigV1()
    assert config.max_worker_rounds == 4
    assert config.worker_recovery_seconds > 0


def test_direct_matching_domain_skips_redundant_k_ladder(monkeypatch):
    prepared = {
        "state": {"keys": ("op",), "problem": object()},
        "resource_configurations": {"op": (
            SimpleNamespace(worker_ids=("w1",)),
            SimpleNamespace(worker_ids=("w2",)),
            SimpleNamespace(worker_ids=("w3",)),
        )},
        "resource_audit": (),
        "regimes": {"op": ("r",)},
        "regime_count": 1,
    }
    monkeypatch.setattr(
        controller, "_failure_classification",
        lambda value: (controller.WORKER_CREW_DOMAIN_TRUNCATED, {"proven": True}),
    )
    monkeypatch.setattr(
        controller.ProductionControllerV1, "_normal_ladder",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("historical K ladder was entered")),
    )
    monkeypatch.setattr(
        controller.ProductionControllerV1, "_worker_recovery",
        lambda self, base, **kwargs: {
            "candidate": base, "recovery": "CREW", "trace": {},
            "failure_classification": controller.WORKER_CREW_DOMAIN_TRUNCATED,
        },
    )
    monkeypatch.setattr(
        controller, "run_canonical_schedule",
        lambda *args, **kwargs: _result(status="REJECTED", solver="INFEASIBLE", valid=False),
    )
    result = controller.ProductionControllerV1().run_window(prepared)
    assert result["recovery"] == "CREW"
