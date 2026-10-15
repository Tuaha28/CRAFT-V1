from datetime import datetime, timedelta

from crafter.repair import calendar_engine_v2 as v2
from crafter.repair.resource_configs import ResourceConfiguration


def _config(key, worker, machine):
    return ResourceConfiguration(
        key, 2, 10, 1, (machine,), (str(worker),), 60, 600,
        ("test",), 10.0)


def _state():
    origin = datetime(2026, 1, 1)
    return {
        "keys": ("o",),
        "operator_index": {
            "crew_of": {1: (0, 1), 2: (0, 1), 3: (1, 1)},
            "shift_of": {},
        },
        "_worker_res": {},
        "_machine_res": {},
        "origin": origin,
    }


def test_equivalent_calendar_crews_share_one_temporal_representative(monkeypatch):
    state = _state()
    c1, c2 = _config("o", 1, 101), _config("o", 2, 102)
    calls = []

    def fake_v1(state, configurations, **kwargs):
        calls.append(configurations)
        regimes = {}
        for key, configs in configurations.items():
            regimes[key] = tuple({
                "key": key, "config": config, "base": config,
                "lo": 0, "hi": 100, "formula": ((1, 600),),
                "segments": 0,
            } for config in configs)
        return regimes, [{"operation_key": "o"}], sum(map(len, regimes.values())), {
            "checks": [], "mismatches": 0, "all_equivalent": True,
        }

    monkeypatch.setattr(v2, "generate_calendar_regimes", fake_v1)
    regimes, summary, total, evidence = v2.generate_calendar_regimes_v2(
        state, {"o": (c1, c2)})

    assert len(calls[0]["o"]) == 1
    assert {row["config"] for row in regimes["o"]} == {c1, c2}
    assert total == 2
    assert summary[0]["calendar_behavior_classes"] == 1
    assert evidence["calendar_engine_v2"]["representative_reduction"] == 1


def test_reservations_and_calendar_identity_prevent_unsafe_sharing():
    state = _state()
    c1, c2 = _config("o", 1, 101), _config("o", 2, 102)
    assert v2.configuration_calendar_signature(state, c1) == v2.configuration_calendar_signature(state, c2)

    origin = state["origin"]
    state["_worker_res"] = {"2": ((origin, origin + timedelta(hours=1)),)}
    assert v2.configuration_calendar_signature(state, c1) != v2.configuration_calendar_signature(state, c2)

    state["_worker_res"] = {}
    state["_machine_res"] = {102: ((0, 100, "order", "owner"),)}
    assert v2.configuration_calendar_signature(state, c1) != v2.configuration_calendar_signature(state, c2)


def test_calendar_engine_v2_is_deterministic(monkeypatch):
    state = _state()
    configs = {"o": (_config("o", 2, 102), _config("o", 1, 101))}

    def fake_v1(state, configurations, **kwargs):
        rows = tuple({"key": "o", "config": config, "base": config,
                      "lo": 0, "hi": 1, "formula": ((1, 1),), "segments": 0}
                     for config in configurations["o"])
        return {"o": rows}, [{"operation_key": "o"}], len(rows), {
            "checks": [], "mismatches": 0, "all_equivalent": True}

    monkeypatch.setattr(v2, "generate_calendar_regimes", fake_v1)
    first = v2.generate_calendar_regimes_v2(state, configs)
    second = v2.generate_calendar_regimes_v2(state, configs)
    assert first[:3] == second[:3]

