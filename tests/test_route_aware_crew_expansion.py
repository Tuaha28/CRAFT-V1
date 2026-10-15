from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
import time

import pytest

import crafter.repair.route_aware_crew_expansion as expansion
from crafter.repair.boundary_aware_modes import BoundaryAvailabilityIndex, _matching
from crafter.repair.calendar_regimes import _breakpoints
from crafter.repair.resource_configs import ResourceConfiguration


def config(key="op-a", workers=("w1", "w2"), machines=(11, 12)):
    return ResourceConfiguration(
        key, 9, 3, 2, tuple(machines), tuple(workers), 10, 100,
        ("BASE",), 100 / 60,
    )


def test_strict_superset_retains_existing_configurations():
    base = {"op-a": (config(),)}
    added = replace(config(), worker_ids=("w3", "w4"))
    merged = expansion.merge_additional_configurations(base, {"op-a": (added,)})
    audit = expansion.audit_strict_superset(base, merged)
    assert audit["strict_superset_or_equal"] is True
    assert audit["strict_superset"] is True
    assert base["op-a"][0] in merged["op-a"]


def test_duplicate_configuration_is_not_added():
    base = {"op-a": (config(),)}
    merged = expansion.merge_additional_configurations(base, {"op-a": (config(), config())})
    assert merged == base


def test_matching_prevents_duplicate_worker_and_respects_contention_order():
    result = _matching(
        ("slot-1", "slot-2"),
        (("busy", "free-a", "free-b"),) * 2,
        max_solutions=1,
        worker_pressure={"busy": 9, "free-a": 1, "free-b": 1},
    )
    assert result == (("free-a", "free-b"),)
    assert len(set(result[0])) == 2


def test_worker_contention_ranking_is_deterministic():
    audit = (
        {"operation_key": "b", "domain_rows": ({"worker_pool": ("w2", "w1")},)},
        {"operation_key": "a", "domain_rows": ({"worker_pool": ("w1", "w3")},)},
    )
    expected = {"w1": 2, "w2": 1, "w3": 1}
    assert expansion.build_worker_contention(audit, ("a", "b")) == expected
    assert expansion.build_worker_contention(tuple(reversed(audit)), ("b", "a")) == expected


def test_adaptive_resource_ranking_uses_bottleneck_evidence_deterministically():
    configurations = {
        "no-start": (config("no-start", workers=("shared", "w2")),),
        "delayed": (config("delayed", workers=("shared", "w3")),),
        "plain": (config("plain", workers=("w4", "w5")),),
    }
    diagnosis = (
        {"operation_key": "plain", "earliest_start_in_exposed_domain": 0,
         "earliest_ready_source": "ORDER_RELEASE", "crew_delay_seconds": 0},
        {"operation_key": "delayed", "earliest_start_in_exposed_domain": 100,
         "earliest_ready_source": "ORDER_RELEASE", "crew_delay_seconds": 90},
        {"operation_key": "no-start", "earliest_start_in_exposed_domain": None,
         "earliest_ready_source": "UNRESOLVED_INTERNAL_PREDECESSOR",
         "crew_delay_seconds": None},
    )
    worker = {key: {"worker_domain_truncated": True} for key in configurations}
    machine = {
        "no-start": {"machine_domain_truncated": True},
        "delayed": {"machine_domain_truncated": True},
        "plain": {"machine_domain_truncated": False},
    }
    first = expansion.rank_adaptive_resource_bottlenecks(
        diagnosis, worker, machine, configurations)
    second = expansion.rank_adaptive_resource_bottlenecks(
        tuple(reversed(diagnosis)), worker, machine, configurations)
    assert [row["operation_key"] for row in first] == [
        "delayed", "no-start", "plain"]
    assert [row["operation_key"] for row in second] == [
        "delayed", "no-start", "plain"]
    assert "PREDECESSOR_BLOCKED" in first[1]["bottleneck_reasons"]
    assert "MACHINE_DOMAIN_TRUNCATED" in first[0]["bottleneck_reasons"]


def test_machine_expansion_adds_only_unseen_legal_shapes(monkeypatch):
    base = {"op-a": (config("op-a", machines=(11, 12)),)}
    candidates = {
        "op-a": (
            config("op-a", machines=(11, 12)),
            config("op-a", machines=(13, 14)),
            config("op-a", machines=(15, 16)),
        )
    }
    monkeypatch.setattr(
        expansion, "generate_resource_configurations",
        lambda *args, **kwargs: (candidates, (), {}),
    )
    rows, trace = expansion.generate_additional_machine_configurations(
        {"unused": True}, base, "op-a", 1)
    assert len(rows) == 1
    assert rows[0].machine_ids == (13, 14)
    assert trace["new_machine_alternatives_accepted"] == 1
    assert rows[0].worker_ids == base["op-a"][0].worker_ids


def test_production_adaptive_policy_is_moderate_and_operationally_bounded():
    policy = expansion.RouteAwareCrewExpansionPolicyV1(
        max_rounds=4,
        total_new_crews_per_operation_by_round=(8, 16, 32, 64),
        max_pressure_targets_per_round=4,
    )
    assert policy.pressure_targets_by_round == (3, 4, 4, 4)
    assert policy.total_new_machine_alternatives_per_operation_by_round == (1, 2, 3, 4)
    assert policy.max_resource_configurations == 600
    assert policy.max_calendar_regimes == 5500
    assert policy.max_peak_rss_mb == 1536.0
    assert policy.max_recovery_runtime_seconds == 300.0


def fake_route_state(successor="op-b"):
    operation = SimpleNamespace(order_id="order")
    problem = SimpleNamespace(
        operation_by_key={"op-a": operation, "op-b": operation, "op-c": operation},
        order_by_id={"order": SimpleNamespace(release_seconds=0)},
    )
    index = SimpleNamespace(
        route_predecessor={"op-a": None, "op-b": "op-a", "op-c": "op-b"},
        route_successor={"op-a": successor, "op-b": "op-c", "op-c": None},
    )
    return {"keys": ("op-a", "op-b", "op-c"), "problem": problem,
            "after_index": index, "committed_routing_completion": {}}


def truncated_audit(*keys):
    return tuple({
        "operation_key": key,
        "domain_rows": ({"more_worker_crews_available": True,
                         "more_machine_sets_available": False,
                         "worker_pool": ("w1", "w2")},),
    } for key in keys)


def test_route_successor_closure_includes_changed_truncated_successor():
    state = fake_route_state()
    old = {"earliest_end": {"op-a": 1000}}
    new = {"earliest_end": {"op-a": 100, "op-b": 200}}
    selected, reasons = expansion.select_route_successor_closure(
        state, ("op-a",), old, new, truncated_audit("op-a", "op-b", "op-c"),
        max_depth=1, max_operations=4)
    assert selected == ("op-a", "op-b")
    assert reasons[0]["trigger_predecessor"] == "op-a"


def test_route_successor_closure_stops_when_successor_domain_is_not_truncated():
    state = fake_route_state()
    audit = truncated_audit("op-a") + ({"operation_key": "op-b", "domain_rows": ({
        "more_worker_crews_available": False, "more_machine_sets_available": False,
    },)},)
    selected, reasons = expansion.select_route_successor_closure(
        state, ("op-a",), {"earliest_end": {"op-a": 1000}},
        {"earliest_end": {"op-a": 100}}, audit)
    assert selected == ("op-a",)
    assert reasons == ()


def test_route_closure_respects_operation_budget():
    state = fake_route_state()
    selected, _ = expansion.select_route_successor_closure(
        state, ("op-a",), {"earliest_end": {"op-a": 1000, "op-b": 1100}},
        {"earliest_end": {"op-a": 100, "op-b": 200}},
        truncated_audit("op-a", "op-b", "op-c"), max_depth=4, max_operations=2)
    assert selected == ("op-a", "op-b")


def test_complete_causal_closure_exceeds_historical_limits():
    keys = tuple(f"op-{i}" for i in range(9))
    operation = SimpleNamespace(order_id="order")
    problem = SimpleNamespace(
        operation_by_key={key: operation for key in keys},
        order_by_id={"order": SimpleNamespace(release_seconds=0)},
    )
    predecessor = {keys[0]: None}
    successor = {}
    for left, right in zip(keys, keys[1:]):
        predecessor[right] = left
        successor[left] = right
    successor[keys[-1]] = None
    state = {"keys": keys, "problem": problem,
             "after_index": SimpleNamespace(route_predecessor=predecessor,
                                              route_successor=successor),
             "committed_routing_completion": {}}
    old = {"earliest_end": {key: 1000 + i * 100 for i, key in enumerate(keys)}}
    new = {"earliest_end": {key: 100 + i * 100 for i, key in enumerate(keys)}}
    selected, reasons = expansion.select_route_successor_closure(
        state, (keys[0],), old, new, truncated_audit(*keys),
        max_depth=None, max_operations=None)
    assert selected == keys
    assert max(row["depth"] for row in reasons) >= 8


def test_complete_closure_excludes_unrelated_operation_and_is_deterministic():
    state = fake_route_state()
    state["keys"] = ("op-a", "op-b", "op-c", "unrelated")
    state["after_index"].route_predecessor["unrelated"] = None
    state["after_index"].route_successor["unrelated"] = None
    audit = truncated_audit("op-a", "op-b", "op-c")
    old = {"earliest_end": {"op-a": 1000, "op-b": 1100, "op-c": 1200}}
    new = {"earliest_end": {"op-a": 100, "op-b": 200, "op-c": 300}}
    first = expansion.select_route_successor_closure(
        state, ("op-a",), old, new, audit, max_depth=None, max_operations=None)
    second = expansion.select_route_successor_closure(
        state, ("op-a",), old, new, audit, max_depth=None, max_operations=None)
    assert first == second
    assert "unrelated" not in first[0]


def test_generated_configurations_respect_boundary_and_are_deterministic(monkeypatch):
    base = config()
    availability = BoundaryAvailabilityIndex(
        0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    monkeypatch.setattr(expansion, "_crew_candidates", lambda *args, **kwargs: (
        ("w3", "w4"), ("w5", "w6")))
    monkeypatch.setattr(expansion, "_candidate_end_seconds", lambda *args, **kwargs: (
        200, ((100, 200),)))
    state = {"unused": True}
    first, trace1 = expansion.generate_additional_crew_configurations(
        state, availability, {"op-a": (base,)}, "op-a", 100, 8,
        worker_pressure={"w3": 2, "w4": 2, "w5": 1, "w6": 1})
    second, trace2 = expansion.generate_additional_crew_configurations(
        state, availability, {"op-a": (base,)}, "op-a", 100, 8,
        worker_pressure={"w3": 2, "w4": 2, "w5": 1, "w6": 1})
    assert expansion.domain_signatures({"op-a": first}) == expansion.domain_signatures({"op-a": second})
    assert first[0].worker_ids == ("w5", "w6")
    assert {k: v for k, v in trace1.items() if k != "matching_generation_seconds"} == {
        k: v for k, v in trace2.items() if k != "matching_generation_seconds"
    }


def test_machine_boundary_rejects_illegal_generated_mode(monkeypatch):
    availability = BoundaryAvailabilityIndex(
        0, 1000, {}, {11: ((0, 150),), 12: ((0, 1000),)}, {}, {})
    monkeypatch.setattr(expansion, "_crew_candidates", lambda *args, **kwargs: (("w3", "w4"),))
    monkeypatch.setattr(expansion, "_candidate_end_seconds", lambda *args, **kwargs: (200, ((100, 200),)))
    rows, trace = expansion.generate_additional_crew_configurations(
        {}, availability, {"op-a": (config(),)}, "op-a", 100, 8)
    assert rows == ()
    assert trace["machine_boundary_rejections"] == 1


def test_exact_route_ready_breakpoint_is_bracketed_without_changing_calendars():
    fake_state = {
        "problem": SimpleNamespace(origin=datetime(2026, 1, 1), horizon_seconds=1000),
        "after_index": SimpleNamespace(by_key={"op-a": {"start_minutes": 0}}),
        "_machine_res": {},
        "_worker_res": {},
        "operator_index": {},
    }
    points = _breakpoints(
        fake_state, "op-a", config(), 0, cache=None,
        calendar_signature=("test",), additional_breakpoints=(528,))
    assert 527 in points and 528 in points and 529 in points


def test_authoritative_probe_generation_uses_exact_future_free_interval_starts(monkeypatch):
    state = {
        "problem": SimpleNamespace(operation_by_key={
            "op-a": SimpleNamespace(required_capability="skill")}),
        "planning_context": {"factory_id": 9},
    }
    availability = BoundaryAvailabilityIndex(
        0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {},
        {"w1": ((0, 200), (400, 1000)), "w2": ((500, 1000),)})
    monkeypatch.setattr(expansion, "_eligible_workers", lambda *args: ("w1", "w2"))
    probes = expansion.authoritative_start_probes(
        state, availability, {"op-a": (config(),)}, "op-a", 300)
    assert probes == (300, 400, 500)


def test_cp_failure_is_not_relabelled_physical_infeasibility(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),), "op-b": (config("op-b"),), "op-c": (config("op-c"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    regimes = {key: ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},) for key in base}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10, "op-b": 20, "op-c": 30},
        "earliest_end": {"op-a": 20, "op-b": 30, "op-c": 40}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ())
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, (), lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": False, "cp_status": "INFEASIBLE"})
    assert result["status"] == "WORKER_CREW_DOMAIN_STILL_TRUNCATED"
    assert "PHYSICAL" not in result["status"]


def test_candidate_without_replay_validator_acceptance_is_not_success(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),), "op-b": (config("op-b"),), "op-c": (config("op-c"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    regimes = {key: ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},) for key in base}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10, "op-b": 20, "op-c": 30},
        "earliest_end": {"op-a": 20, "op-b": 30, "op-c": 40}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ({
        "operation_key": "op-a", "route_pressure": True,
    },))
    monkeypatch.setattr(expansion, "authoritative_start_probes", lambda *args, **kwargs: (0,))
    monkeypatch.setattr(
        expansion, "generate_additional_crew_configurations",
        lambda *args, **kwargs: ((), {"operation_key": "op-a",
                                      "new_crews_accepted": 0,
                                      "candidate_crews_generated": 0,
                                      "start_probe_seconds": 0}),
    )
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, truncated_audit("op-a"),
        lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": False, "candidate_exists": True,
                       "replay": "FAIL", "validator": "NOT_RUN"})
    assert result["status"] == "RECOVERY_CANDIDATE_REJECTED"
    assert len(result["rounds"]) == 2  # base + one CP recovery attempt


def test_infeasible_truncated_domain_expands_only_affected_operation(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),), "op-b": (config("op-b"),)}
    availability = BoundaryAvailabilityIndex(
        0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    regimes = {key: ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},)
               for key in base}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10, "op-b": 20},
        "earliest_end": {"op-a": 20, "op-b": 30}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ({
        "operation_key": "op-a", "route_pressure": True,
    },))
    monkeypatch.setattr(expansion, "authoritative_start_probes", lambda *args, **kwargs: (0,))
    calls = []

    def fake_generate(state, availability, configurations, key, start, quota, **kwargs):
        calls.append((key, quota))
        if key != "op-a":
            raise AssertionError("unaffected operation was expanded")
        value = replace(config("op-a"), worker_ids=("w3", "w4"))
        return (value,), {"operation_key": key, "new_crews_accepted": 1,
                           "candidate_crews_generated": 1,
                           "start_probe_seconds": start,
                           "selected_start_probes": [start]}

    monkeypatch.setattr(expansion, "generate_additional_crew_configurations", fake_generate)
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base,
        truncated_audit("op-a"),
        lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": False, "candidate_exists": False,
                       "cp_status": "INFEASIBLE"},
        policy=expansion.RouteAwareCrewExpansionPolicyV1(
            max_rounds=1, total_new_crews_per_operation_by_round=(1,),
            max_expansion_operations=None, max_successor_depth=None,
            max_pressure_targets_per_round=None),
        baseline_result={"accepted": False},
    )
    assert calls == [("op-a", 1)]
    assert result["configurations"]["op-a"]
    assert result["configurations"]["op-b"] == base["op-b"]


def test_valid_candidate_stops_before_another_expansion_round(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),)}, {}, {})
    regimes = {"op-a": ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},)}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10}, "earliest_end": {"op-a": 20}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ({
        "operation_key": "op-a", "route_pressure": True,
    },))
    monkeypatch.setattr(expansion, "authoritative_start_probes", lambda *args, **kwargs: (0,))
    calls = {"generate": 0}

    def fake_generate(*args, **kwargs):
        calls["generate"] += 1
        value = replace(config("op-a"), worker_ids=("w3", "w4"))
        return (value,), {"operation_key": "op-a", "new_crews_accepted": 1,
                           "candidate_crews_generated": 1,
                           "start_probe_seconds": 10, "selected_start_probes": [10]}

    monkeypatch.setattr(expansion, "generate_additional_crew_configurations", fake_generate)
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, truncated_audit("op-a"),
        lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": True, "candidate_exists": True},
        policy=expansion.RouteAwareCrewExpansionPolicyV1(
            max_rounds=None, total_new_crews_per_operation_by_round=(1,),
            max_expansion_operations=None, max_successor_depth=None,
            max_pressure_targets_per_round=None),
        baseline_result={"accepted": False},
    )
    assert result["status"] == "ADAPTIVE_CREW_EXPANSION_ACCEPTED"
    assert calls["generate"] == 1


def test_production_fixed_point_recovery_can_run_multiple_cumulative_rounds(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    regimes = {"op-a": ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},)}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10}, "earliest_end": {"op-a": 20}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ({
        "operation_key": "op-a", "route_pressure": True,
    },))
    monkeypatch.setattr(expansion, "authoritative_start_probes", lambda *args, **kwargs: (0,))
    calls = {"count": 0}

    def fake_generate(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 2:
            return (), {"operation_key": "op-a", "new_crews_accepted": 0,
                        "candidate_crews_generated": 0, "start_probe_seconds": 10}
        value = replace(config("op-a"), worker_ids=(f"w{calls['count'] + 2}", f"w{calls['count'] + 3}"))
        return (value,), {"operation_key": "op-a", "new_crews_accepted": 1,
                          "candidate_crews_generated": 1, "start_probe_seconds": 10,
                          "selected_start_probes": [10]}

    monkeypatch.setattr(expansion, "generate_additional_crew_configurations", fake_generate)
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, truncated_audit("op-a"),
        lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": False, "cp_status": "INFEASIBLE"},
        policy=expansion.RouteAwareCrewExpansionPolicyV1(
            max_rounds=None, total_new_crews_per_operation_by_round=(1,),
            max_expansion_operations=None, max_successor_depth=None,
            max_pressure_targets_per_round=None),
        baseline_result={"accepted": False})
    assert result["status"] == "WORKER_DOMAIN_NO_PROGRESS_INCONSISTENCY"
    assert len(result["rounds"]) >= 3
    assert len(result["configurations"]["op-a"]) == 3
    assert result["rounds"][2]["added_crews_by_operation"]["op-a"] == 2


def test_domain_exhaustion_is_distinct_from_truncation(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    regimes = {"op-a": ({"lo": 0, "hi": 1000, "formula": ((0, 100),)},)}
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10}, "earliest_end": {"op-a": 20}, "unresolved": []})
    monkeypatch.setattr(expansion, "diagnose_worker_route_pressure", lambda *args: ({
        "operation_key": "op-a", "route_pressure": True,
    },))
    monkeypatch.setattr(expansion, "authoritative_start_probes", lambda *args, **kwargs: (0,))
    monkeypatch.setattr(
        expansion, "generate_additional_crew_configurations",
        lambda *args, **kwargs: (
            (replace(config("op-a"), worker_ids=("w3", "w4")),),
            {"operation_key": "op-a", "new_crews_accepted": 1,
             "candidate_crews_generated": 1, "start_probe_seconds": 10,
             "selected_start_probes": [10]},
        ),
    )
    audit = ({"operation_key": "op-a", "domain_rows": ({
        "more_worker_crews_available": True,
        "total_legal_worker_crews": 2,
    },)},)
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, audit,
        lambda configs, probes=None: (regimes, {}),
        lambda *args: {"accepted": False, "cp_status": "INFEASIBLE"},
        policy=expansion.RouteAwareCrewExpansionPolicyV1(
            max_rounds=None, total_new_crews_per_operation_by_round=(8,),
            max_expansion_operations=None, max_successor_depth=None,
            max_pressure_targets_per_round=None),
        baseline_result={"accepted": False})
    assert result["status"] == "WORKER_CREW_DOMAIN_EXHAUSTED_NO_CANDIDATE"
    assert result["worker_domain_accounting"]["op-a"]["worker_domain_fully_exposed"] is True


def test_worker_recovery_runtime_limit_is_distinct_from_domain_exhaustion(monkeypatch):
    state = fake_route_state(successor=None)
    base = {"op-a": (config("op-a"),)}
    availability = BoundaryAvailabilityIndex(0, 1000, {}, {11: ((0, 1000),), 12: ((0, 1000),)}, {}, {})
    monkeypatch.setattr(expansion, "_routing_aware_earliest", lambda *args: {
        "earliest_start": {"op-a": 10}, "earliest_end": {"op-a": 20}, "unresolved": []})
    result = expansion.run_route_aware_adaptive_crew_expansion(
        state, availability, base, truncated_audit("op-a"),
        lambda configs, probes=None: ({}, {}),
        lambda *args: {"accepted": False, "cp_status": "INFEASIBLE"},
        policy=expansion.RouteAwareCrewExpansionPolicyV1(
            max_rounds=None, total_new_crews_per_operation_by_round=(8,),
            max_expansion_operations=None, max_successor_depth=None,
            max_pressure_targets_per_round=None),
        baseline_result={"accepted": False},
        recovery_deadline=time.perf_counter() - 1,
    )
    assert result["status"] == "WORKER_RECOVERY_RUNTIME_LIMIT"
    assert result["status"] != "WORKER_CREW_DOMAIN_EXHAUSTED_NO_CANDIDATE"


def test_module_contains_no_window_or_factory_specific_ids():
    source = Path(expansion.__file__).read_text(encoding="utf-8")
    for forbidden in ("251_2065", "251_2066", "Window 14", "Factory 2"):
        assert forbidden not in source
