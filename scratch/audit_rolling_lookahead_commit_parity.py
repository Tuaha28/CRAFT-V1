"""Show whether overlapping-lookahead fitness equals committed state."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ga.fitness import evaluate_schedule
from ga.rolling_horizon import extract_committed_subschedule

OUT = Path("outputs/integration_audit/rolling_lookahead_commit_parity.json")


def score(chromosome, num_ops, scored):
    details = {
        "A_1": {"setup_mins": 0.0, "serial_processing_mins": 10.0,
                "required_capability": "body assembly"},
        "B_1": {"setup_mins": 0.0, "serial_processing_mins": 10.0,
                "required_capability": "body assembly"},
    }
    common = dict(
        op_details=details,
        gnn_predictions={"A_1": {"delay": 0.0, "priority": 0.0},
                         "B_1": {"delay": 0.0, "priority": 0.0}},
        all_line_ids=[1],
        order_deadlines={"A": 1000.0, "B": 1000.0},
        machine_pools={"A_1": {1: ["m1"]}, "B_1": {1: ["m1"]}},
        machine_line_map={"m1": 1},
        usable_machine_count=1,
        present_operator_count=1,
        scored_order_ids=scored,
        op_optype={"A_1": "body assembly", "B_1": "body assembly"},
        order_release_minutes={"A": 0.0, "B": 0.0},
        op_sequence_map={"A_1": 1, "B_1": 1},
        base_time=datetime(2026, 9, 14, 6, 0),
        return_details=True,
    )
    fit, det = evaluate_schedule(chromosome, num_ops, **common)
    return float(fit[0]), det


def main():
    # Full overlapping lookahead puts provisional B before committed A.
    full = ["B_1", "A_1", 1, 1, 1, 1]
    full_fit, full_det = score(full, 2, {"A"})
    committed, n = extract_committed_subschedule(full, 2, {"A"})
    commit_fit, commit_det = score(committed, n, {"A"})
    result = {
        "full_lookahead": {
            "chromosome": full,
            "scored_orders": ["A"],
            "A_completion_minutes": full_det["order_completion_minutes"]["A"],
            "scored_makespan_minutes": full_det["scored_makespan_minutes"],
            "fitness": full_fit,
        },
        "carried_commit": {
            "chromosome": list(committed),
            "A_completion_minutes": commit_det["order_completion_minutes"]["A"],
            "scored_makespan_minutes": commit_det["scored_makespan_minutes"],
            "fitness": commit_fit,
        },
        "same_completion": (
            full_det["order_completion_minutes"]["A"]
            == commit_det["order_completion_minutes"]["A"]
        ),
        "default_authoritative_mode_affected": False,
        "scope": "Only lookahead_size > commit_size; current default and release-gated modes enforce L == C.",
        "interpretation": "Provisional B delays A during optimization, then is removed before committed state is reconstructed."
    }
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
