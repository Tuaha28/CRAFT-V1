from types import SimpleNamespace

import crafter.repair.solve as solve


def test_replay_uses_the_same_worker_reservation_view_as_cp(monkeypatch):
    reservations = {"w1": ("authoritative-boundary",)}
    captured = {}

    def fake_replay(*args, worker_reservations=None):
        captured["reservations"] = worker_reservations
        return 60, (), [{"type": "SENTINEL"}]

    monkeypatch.setattr(solve, "_replay_mode", fake_replay)
    monkeypatch.setattr(solve, "_phenotype", lambda *args: None)
    monkeypatch.setattr(solve, "_factory_isolation", lambda *args: [])

    state = {
        "problem": SimpleNamespace(),
        "operator_index": SimpleNamespace(),
        "after_index": SimpleNamespace(),
        "keys": ("op",),
        "_worker_res": reservations,
    }
    mode = SimpleNamespace(end_seconds=60)
    replay, validator, _events, _isolation = solve._accept(
        state, {"op": mode}, [{"operation_key": "op"}], [{"mismatches": []}]
    )

    assert replay["status"] == "FAIL"
    assert captured["reservations"] is reservations
    assert validator["status"] == "NOT_REACHED"


def test_planning_acceptance_uses_scoped_future_outside_validator(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        solve,
        "_replay_mode",
        lambda *args, worker_reservations=None: (60, (), []),
    )
    monkeypatch.setattr(solve, "_factory_isolation", lambda *args: [])

    def fake_scoped(candidate, state, *, required_operation_keys,
                    boundary_facts, future_outside_operation_keys):
        captured["required"] = tuple(required_operation_keys)
        captured["future"] = tuple(future_outside_operation_keys)
        return {
            "physical_status": "VALID",
            "physical": {"report": {"valid": True, "errors": []}},
        }

    import crafter.repair.scoped_validation as scoped
    monkeypatch.setattr(scoped, "validate_canonical_planning_candidate", fake_scoped)
    monkeypatch.setattr(
        solve,
        "_phenotype",
        lambda *args: (_ for _ in ()).throw(AssertionError("full baseline path used")),
    )

    state = {
        "problem": SimpleNamespace(),
        "operator_index": SimpleNamespace(),
        "after_index": SimpleNamespace(),
        "keys": ("selected",),
        "classified": {
            "selected": {"state": "MOVABLE_WINDOW"},
            "future": {"state": "FUTURE_OUTSIDE_WINDOW"},
        },
        "planning_context": {"mode": "INITIAL_PLAN"},
    }
    mode = SimpleNamespace(end_seconds=60)
    replay, validator, _events, _isolation = solve._accept(
        state, {"selected": mode}, [{"operation_key": "selected"}], [{"mismatches": []}]
    )

    assert replay["status"] == "PASS"
    assert validator["status"] == "VALID"
    assert captured == {"required": ("selected",), "future": ("future",)}
