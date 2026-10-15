from types import SimpleNamespace

import crafter.repair.balanced_resource_rescue as rescue
from crafter.repair.resource_configs import ResourceConfiguration


def _config(key, worker, machine):
    return ResourceConfiguration(
        key, 1, 1, 1, (machine,), (worker,), 0, 60,
        ("TEST",), 1.0)


def _state(keys):
    operations = {
        key: SimpleNamespace(order_id=key, required_capability="sew")
        for key in keys}
    orders = {key: SimpleNamespace(release_seconds=0) for key in keys}
    return {
        "keys": tuple(keys),
        "operator_index": {"crew_of": {}, "shift_of": {}},
        "problem": SimpleNamespace(
            operation_by_key=operations, order_by_id=orders),
        "after_index": SimpleNamespace(
            route_predecessor={key: None for key in keys}),
        "committed_routing_completion": {},
    }


def _audit(*keys):
    return tuple({
        "operation_key": key,
        "domain_rows": ({
            "total_legal_worker_crews": 20,
            "total_legal_machine_sets": 5,
            "more_worker_crews_available": True,
            "more_machine_sets_available": True,
            "worker_pool": ("w1", "w2", "w3"),
        },),
    } for key in keys)


def _regimes(configurations):
    return {
        key: tuple({"lo": index * 10, "config": config}
                   for index, config in enumerate(rows))
        for key, rows in configurations.items()
    }


def test_coverage_report_exposes_imbalance_without_enumerating_product():
    state = _state(("thin", "wide"))
    configurations = {
        "thin": (_config("thin", "w1", 1),),
        "wide": tuple(_config("wide", f"w{i}", i) for i in range(1, 13)),
    }
    rows = rescue.resource_coverage_report(
        state, configurations, _regimes(configurations),
        _audit("thin", "wide"))
    by_key = {row["operation_key"]: row for row in rows}
    assert by_key["thin"]["exposed_resource_configurations"] == 1
    assert by_key["wide"]["exposed_resource_configurations"] == 12
    assert by_key["thin"]["legal_resource_source_alternatives"] == 100
    floor = rescue.derive_configuration_floor(
        rows, rescue.BalancedResourceRescuePolicyV1())
    assert 8 <= floor <= 12
    assert "thin" in rescue.operations_below_balanced_coverage(
        rows, floor, rescue.BalancedResourceRescuePolicyV1())


def test_balanced_rescue_solves_feasibility_before_objective(monkeypatch):
    state = _state(("op",))
    configurations = {"op": (_config("op", "w1", 1),)}
    regimes = _regimes(configurations)
    calls = []

    monkeypatch.setattr(
        rescue, "_routing_aware_earliest",
        lambda *args: {"earliest_end": {"op": 100}})
    monkeypatch.setattr(
        rescue, "_route_ready_seconds", lambda *args: (0, "ORDER_RELEASE"))
    monkeypatch.setattr(
        rescue, "authoritative_start_probes", lambda *args: (0,))
    monkeypatch.setattr(
        rescue, "build_worker_contention", lambda *args: {})
    monkeypatch.setattr(
        rescue, "generate_additional_machine_configurations",
        lambda *args, **kwargs: (
            (_config("op", "w2", 2),),
            {"new_machine_alternatives_accepted": 1}))
    monkeypatch.setattr(
        rescue, "generate_additional_crew_configurations",
        lambda *args, **kwargs: (
            tuple(_config("op", f"w{i}", 1) for i in range(3, 10)),
            {"new_crews_accepted": 7, "selected_start_probes": (0,)}))

    observed_probes = []

    def build(expanded, probes):
        observed_probes.append(dict(probes))
        rows = _regimes(expanded)
        return rows, {
            "calendar_regime_count": sum(len(value) for value in rows.values()),
            "equivalence": {},
        }

    selected = ({"operation_key": "op"},)

    def feasibility(*args):
        calls.append("feasibility")
        return {"accepted": True, "candidate_exists": True, "candidate": {
            "acceptance": {
                "winning_stage": {"status": "OPTIMAL"},
                "selected_schedule": selected,
                "materialization": {"mismatch_count": 0},
                "replay": {"status": "PASS"},
                "validator": {"status": "VALID"},
            }}}

    def objective(_configs, _regimes, _meta, hint):
        calls.append("objective")
        assert tuple(hint) == selected
        return {"accepted": True, "candidate_exists": True, "candidate": {
            "acceptance": {
                "winning_stage": {"status": "OPTIMAL"},
                "selected_schedule": selected,
                "materialization": {"mismatch_count": 0},
                "replay": {"status": "PASS"},
                "validator": {"status": "VALID"},
            }}}

    result = rescue.run_balanced_resource_feasibility_rescue(
        state, object(), configurations, regimes, _audit("op"), build,
        feasibility, objective, existing_start_probes={"op": (5,)})
    assert calls == ["feasibility", "objective"]
    assert observed_probes == [{"op": (0, 5)}]
    assert result["status"] == "BALANCED_RESOURCE_RESCUE_ACCEPTED"
    assert result["resource_configuration_count_after"] == 9
    assert result["calendar_regime_count_after"] == 9


def test_infeasible_partial_rescue_is_computational_limit(monkeypatch):
    state = _state(("op",))
    configurations = {"op": (_config("op", "w1", 1),)}
    regimes = _regimes(configurations)
    monkeypatch.setattr(rescue, "_routing_aware_earliest", lambda *a: {})
    monkeypatch.setattr(rescue, "_route_ready_seconds", lambda *a: (0, "ORDER_RELEASE"))
    monkeypatch.setattr(rescue, "authoritative_start_probes", lambda *a: (0,))
    monkeypatch.setattr(rescue, "build_worker_contention", lambda *a: {})
    monkeypatch.setattr(
        rescue, "generate_additional_machine_configurations",
        lambda *a, **k: ((), {"new_machine_alternatives_accepted": 0}))
    monkeypatch.setattr(
        rescue, "generate_additional_crew_configurations",
        lambda *a, **k: ((_config("op", "w2", 1),),
                         {"new_crews_accepted": 1,
                          "selected_start_probes": (0,)}))

    def build(expanded, probes):
        rows = _regimes(expanded)
        return rows, {"calendar_regime_count": 2, "equivalence": {}}

    result = rescue.run_balanced_resource_feasibility_rescue(
        state, object(), configurations, regimes, _audit("op"), build,
        lambda *a: {"accepted": False, "candidate_exists": False,
                    "candidate": {"acceptance": {
                        "winning_stage": {"status": "INFEASIBLE"}}}},
        lambda *a: (_ for _ in ()).throw(
            AssertionError("objective must not run")))
    assert result["status"] == "COMPUTATIONAL_LIMIT"
    assert result["feasibility"]["cp_status"] == "INFEASIBLE"
    assert result["domain_exhausted"] is False
