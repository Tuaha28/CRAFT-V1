"""Verify the authoritative comparison gives every arm one frozen problem."""
import ast
import hashlib
import json
from pathlib import Path

SOURCE = Path("run_authoritative_comparison.py")
OUT = Path("outputs/integration_audit/authoritative_arm_parity.json")


def named_calls(tree, name):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def main():
    text = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    calls = named_calls(tree, "run_rolling_horizon") + named_calls(
        tree, "run_rolling_horizon_baseline"
    )
    required = {
        "export", "decision_engine", "operator_index", "ownership_snapshot",
        "committed_machine_state", "committed_machine_last_order",
        "initial_operator_reservations",
    }
    call_results = []
    for call in calls:
        kwargs = {item.arg: item.value for item in call.keywords}
        call_results.append({
            "function": call.func.id,
            "required_arguments_present": sorted(required) == sorted(
                required.intersection(kwargs)
            ),
            "export_true": (
                isinstance(kwargs.get("export"), ast.Constant)
                and kwargs["export"].value is True
            ),
            "operator_index": ast.unparse(kwargs.get("operator_index")),
            "ownership_snapshot": ast.unparse(kwargs.get("ownership_snapshot")),
            "machine_state": ast.unparse(kwargs.get("committed_machine_state")),
            "machine_last_order": ast.unparse(
                kwargs.get("committed_machine_last_order")
            ),
            "worker_state": ast.unparse(
                kwargs.get("initial_operator_reservations")
            ),
        })
    result = {
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "calls_found": len(calls),
        "committed_machine_state_loaded_once": (
            text.count("fetch_committed_machine_state(ORIGIN)") == 1
        ),
        "committed_worker_state_loaded_once": (
            text.count("fetch_committed_operator_reservations(ORIGIN)") == 1
        ),
        "calls": call_results,
        "same_decoder": all(
            row["required_arguments_present"]
            and row["export_true"]
            and row["operator_index"] == "operator_index"
            and row["ownership_snapshot"] == "builder.ownership_snapshot"
            for row in call_results
        ),
        "same_committed_state": all(
            row["machine_state"] == "dict(base_machine_free)"
            and row["machine_last_order"] == "dict(base_machine_last_order)"
            and row["worker_state"]
            == "copy.deepcopy(base_operator_reservations)"
            for row in call_results
        ),
        "same_objective_and_deadline_path": (
            "evaluate_schedule(" in Path("ga/rolling_horizon.py").read_text(
                encoding="utf-8"
            )
            and text.count("builder.df_routings") >= 2
        ),
        "stale_worker_blind_report_claim_absent": (
            "which performs NO worker assignment at all" not in text
        ),
    }
    assert result["calls_found"] == 2
    assert result["same_decoder"]
    assert result["same_committed_state"]
    assert result["same_objective_and_deadline_path"]
    assert result["stale_worker_blind_report_claim_absent"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
