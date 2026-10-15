from types import SimpleNamespace

from ortools.sat.python import cp_model

from crafter.production.controller_v1 import _candidate_valid
from crafter.repair import solve as solve_module


class _FakeSolver:
    def __init__(self, code, model):
        self.code = code
        self.model_seen = model
        self.parameters = SimpleNamespace(max_time_in_seconds=0.0)

    def Solve(self, model):
        assert model is self.model_seen
        return self.code

    def StatusName(self, code):
        return {cp_model.UNKNOWN: "UNKNOWN", cp_model.FEASIBLE: "FEASIBLE",
                cp_model.OPTIMAL: "OPTIMAL", cp_model.INFEASIBLE: "INFEASIBLE"}[code]

    def WallTime(self):
        return 0.01

    def ResponseProto(self):
        return SimpleNamespace(deterministic_time=0.01)

    def NumBranches(self):
        return 1

    def NumConflicts(self):
        return 0

    def ObjectiveValue(self):
        return 0.0

    def BestObjectiveBound(self):
        return 0.0


def _run(monkeypatch, statuses):
    monkeypatch.setenv("CRAFT_ADAPTIVE_CP_BUDGETS", "1")
    monkeypatch.setenv("CRAFT_ADAPTIVE_CP_RETRY_SECONDS", "120,240")
    model = object()
    sequence = iter(statuses)
    monkeypatch.setattr(solve_module, "_solver",
                        lambda: _FakeSolver(next(sequence), model))
    solver, row, attempts = solve_module._solve_stage_with_adaptive_budget(
        {"model": model}, 60.0)
    return solver, row, attempts


def test_normal_success_does_not_escalate(monkeypatch):
    solver, row, attempts = _run(monkeypatch, [cp_model.OPTIMAL])
    assert solver is not None
    assert [x["time_limit_seconds"] for x in attempts] == [60.0]
    assert row["status"] == "OPTIMAL"


def test_unknown_triggers_120_second_retry(monkeypatch):
    solver, row, attempts = _run(monkeypatch, [cp_model.UNKNOWN, cp_model.OPTIMAL])
    assert solver is not None
    assert [x["time_limit_seconds"] for x in attempts] == [60.0, 120.0]
    assert row["status"] == "OPTIMAL"


def test_120_success_stops_escalation(monkeypatch):
    _, _, attempts = _run(monkeypatch, [cp_model.UNKNOWN, cp_model.FEASIBLE, cp_model.OPTIMAL])
    assert [x["time_limit_seconds"] for x in attempts] == [60.0, 120.0]


def test_120_unknown_triggers_240_second_retry(monkeypatch):
    solver, row, attempts = _run(monkeypatch, [cp_model.UNKNOWN, cp_model.UNKNOWN, cp_model.OPTIMAL])
    assert solver is not None
    assert [x["time_limit_seconds"] for x in attempts] == [60.0, 120.0, 240.0]
    assert row["status"] == "OPTIMAL"


def test_validation_failure_is_not_accepted():
    rejected = {"acceptance": {"selected_schedule": [{"operation_key": "x"}],
                                "materialization": {"mismatch_count": 0},
                                "replay": {"status": "FAIL"},
                                "validator": {"status": "VALID"}}}
    assert not _candidate_valid(rejected)


def test_repeated_unknown_remains_unresolved_not_infeasible(monkeypatch):
    solver, row, attempts = _run(monkeypatch, [cp_model.UNKNOWN, cp_model.UNKNOWN, cp_model.UNKNOWN])
    assert solver is None
    assert row["status"] == "UNKNOWN"
    assert len(attempts) == 3
    assert all(x["status"] == "UNKNOWN" for x in attempts)
    assert row["status"] != "INFEASIBLE"
