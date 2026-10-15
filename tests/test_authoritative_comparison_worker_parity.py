import ast
from pathlib import Path


SOURCE = Path("run_authoritative_comparison.py")


def _calls(name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _keywords(call):
    return {keyword.arg: keyword.value for keyword in call.keywords}


def test_authoritative_comparison_freezes_one_committed_state_for_every_arm():
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count("fetch_committed_machine_state(ORIGIN)") == 1
    assert source.count("fetch_committed_operator_reservations(ORIGIN)") == 1

    calls = _calls("run_rolling_horizon") + _calls(
        "run_rolling_horizon_baseline"
    )
    assert len(calls) == 2
    required = {
        "export", "decision_engine", "operator_index", "ownership_snapshot",
        "committed_machine_state", "committed_machine_last_order",
        "initial_operator_reservations",
    }
    for call in calls:
        keywords = _keywords(call)
        assert required <= set(keywords)
        assert isinstance(keywords["export"], ast.Constant)
        assert keywords["export"].value is True
        assert isinstance(keywords["operator_index"], ast.Name)
        assert keywords["operator_index"].id == "operator_index"
        assert isinstance(keywords["ownership_snapshot"], ast.Attribute)
        reservations = ast.unparse(keywords["initial_operator_reservations"])
        assert reservations == "copy.deepcopy(base_operator_reservations)"
        assert ast.unparse(keywords["committed_machine_state"]) == (
            "dict(base_machine_free)"
        )
        assert ast.unparse(keywords["committed_machine_last_order"]) == (
            "dict(base_machine_last_order)"
        )


def test_authoritative_report_no_longer_claims_baselines_are_worker_blind():
    source = SOURCE.read_text(encoding="utf-8")
    stale = [
        "STAFFING FEASIBILITY -- GA ARM ONLY",
        "which performs NO worker assignment at all",
        "operator assignment happens afterwards",
        "Baseline shortage counts therefore DO NOT EXIST",
    ]
    for phrase in stale:
        assert phrase not in source
    assert "AUTHORITATIVE DECODER" in source
    assert 'RESULTS["staffing_feasibility"] = staffing_by_arm' in source
