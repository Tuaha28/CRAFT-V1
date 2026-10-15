import json
from option_b.pilot_v1 import generate_pilot


def test_bounded_non_final_pilot_emits_clean_isolated_records(tmp_path):
    summary = generate_pilot(tmp_path, seed=42, state_count=2, actions_per_state=5)
    assert summary["unique_states"] == 2
    assert summary["clean_actions"] > 0
    assert summary["authoritative_evaluations"] >= summary["clean_actions"]
    actions = [json.loads(x) for x in (tmp_path / "actions_outcomes.jsonl").read_text().splitlines()]
    assert {a["action_type"] for a in actions} <= {"OS", "LA", "MC"}
    assert all(len(a["operator_diff"]) == (2 if a["action_type"] == "OS" else 1) for a in actions)
    assert all(a["feasibility_result"] == "evaluator_completed" for a in actions)
    assert all(not a["repair_fully_reversed_proposal"] for a in actions)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["final_evaluation_snapshot_used"] is False
    assert manifest["snapshot_database"] is None
