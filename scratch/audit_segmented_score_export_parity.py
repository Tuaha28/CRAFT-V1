"""Deterministic score-versus-export audit for same-worker pause/resume."""
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import agent.decision_engine as decision_module
from agent.decision_engine import AIDecisionEngine
from ga.fitness import evaluate_schedule
from utils.run_artifacts import current_run_id


OUT = Path("outputs/integration_audit/segmented_score_export_parity.json")
EXPORT_DIR = Path("outputs/integration_audit/segmented_score_export_probe")
ORIGIN = datetime(2026, 3, 2, 6, 0)
DEADLINE = datetime(2026, 3, 3, 7, 0)
WORKER = 7001
MACHINE_TYPE = "single-needle lockstitch"


def available(_index, _employee, when):
    return 6 <= when.hour < 14


def main():
    op_keys = ["1_1", "1_2"]
    chromosome = op_keys + [1, 1] + [1, 1]
    details = {
        "1_1": {"setup_mins": 0.0, "serial_processing_mins": 600.0},
        "1_2": {"setup_mins": 0.0, "serial_processing_mins": 60.0},
    }
    preds = {key: {"delay": 0.0, "priority": 0.5} for key in op_keys}
    common = dict(
        all_line_ids=[1],
        order_deadlines={"1": (DEADLINE - ORIGIN).total_seconds() / 60.0},
        machine_pools={"1_1": {1: [101]}, "1_2": {1: [102]}},
        machine_line_map={101: 1, 102: 1},
        usable_machine_count=2,
        present_operator_count=1,
        initial_machine_free={},
        initial_machine_last_order={},
        scored_order_ids={"1"},
        op_optype={"1_1": "body assembly", "1_2": "body assembly"},
        order_release_minutes={"1": 0.0},
        op_sequence_map={"1_1": 1, "1_2": 2},
        base_time=ORIGIN,
    )
    (score,), scored = evaluate_schedule(
        chromosome, 2, details, preds, return_details=True, **common
    )
    (oracle_score,), oracle = evaluate_schedule(
        chromosome,
        2,
        details,
        preds,
        return_details=True,
        worker_availability=lambda when: 6 <= when.hour < 14,
        **common,
    )

    lines = pd.DataFrame({"line_id": [1], "line_number": ["L1"]})
    machines = pd.DataFrame({
        "asset_id": [101, 102], "line_id": [1, 1]
    })
    routings = pd.DataFrame([
        {"production_order_id": "1", "operation_id": "1",
         "operation_sequence_number": 1,
         "required_machine_type": MACHINE_TYPE, "release_date": ORIGIN},
        {"production_order_id": "1", "operation_id": "2",
         "operation_sequence_number": 2,
         "required_machine_type": MACHINE_TYPE, "release_date": ORIGIN},
    ])
    operator_index = {
        "by_line": {(1, "body assembly"): [WORKER]},
        "by_factory": {(1, "body assembly"): [WORKER]},
        "by_skill": {"body assembly": [WORKER]},
        "line_factory": {1: 1},
        "shift_of": {},
        "crew_of": {},
        "efficiency_of": {(WORKER, "body assembly"): 84.0},
    }
    engine = object.__new__(AIDecisionEngine)
    engine.run_id = current_run_id()
    original_available = AIDecisionEngine._available_at
    original_output = decision_module.OUTPUT_DIR
    AIDecisionEngine._available_at = staticmethod(available)
    decision_module.OUTPUT_DIR = str(EXPORT_DIR)
    try:
        summary = engine._export_proposed_schedule(
            chromosome,
            2,
            details,
            preds,
            lines,
            {"1_1": [1], "1_2": [1]},
            {"1": DEADLINE},
            routings,
            machines,
            label="segmented",
            machine_pools=common["machine_pools"],
            operator_index=operator_index,
            present_operator_count=1,
            usable_machine_count=2,
            op_optype=common["op_optype"],
            initial_machine_free={},
            initial_machine_last_order={},
            initial_operator_reservations={},
            base_time=ORIGIN,
        )
    finally:
        AIDecisionEngine._available_at = original_available
        decision_module.OUTPUT_DIR = original_output

    rows = json.loads(Path(summary["schedule_file"]).read_text())
    by_op = {
        f"{row['production_order_id']}_{row['operation_id']}": row
        for row in rows
    }
    completion = json.loads(
        (EXPORT_DIR / "order_completions_segmented.json").read_text()
    )[0]
    result = {
        "configuration": {
            "origin": ORIGIN.isoformat(),
            "deadline": DEADLINE.isoformat(),
            "productive_minutes": {"1_1": 600.0, "1_2": 60.0},
            "worker_availability": "daily 06:00-14:00",
        },
        "current_fitness": {
            "fitness": score,
            "operation_1_end": (
                ORIGIN + timedelta(
                    minutes=scored["final_machine_free"][101])
            ).isoformat(),
            "operation_2_start": (
                ORIGIN + timedelta(
                    minutes=scored["final_machine_free"][102] - 60.0)
            ).isoformat(),
            "order_completion": (
                ORIGIN + timedelta(
                    minutes=scored["order_completion_minutes"]["1"])
            ).isoformat(),
            "order_tardiness_hours": scored["order_lateness_hours"]["1"],
            "fitness_components": scored["fitness_components"],
            "final_machine_free": scored["final_machine_free"],
        },
        "segmented_oracle_evaluator": {
            "fitness": oracle_score,
            "operation_1_end": (
                ORIGIN + timedelta(
                    minutes=oracle["final_machine_free"][101])
            ).isoformat(),
            "operation_2_start": (
                ORIGIN + timedelta(
                    minutes=oracle["final_machine_free"][102] - 60.0)
            ).isoformat(),
            "order_completion": (
                ORIGIN + timedelta(
                    minutes=oracle["order_completion_minutes"]["1"])
            ).isoformat(),
            "order_tardiness_hours": oracle["order_lateness_hours"]["1"],
            "fitness_components": oracle["fitness_components"],
            "final_machine_free": oracle["final_machine_free"],
        },
        "production_export": {
            "operation_1_start": by_op["1_1"]["proposed_start_time"],
            "operation_1_end": by_op["1_1"]["cushioned_end_time"],
            "operation_2_start": by_op["1_2"]["proposed_start_time"],
            "operation_2_end": by_op["1_2"]["cushioned_end_time"],
            "order_completion": completion["predicted_completion"],
            "order_tardiness_hours": completion["schedule_added_late_hours"],
            "execution_feasible": summary["execution_feasible"],
            "worker_segments": by_op["1_1"]["worker_segments_by_employee"],
        },
    }
    result["contract_mismatch"] = (
        result["current_fitness"]["order_completion"]
        != result["production_export"]["order_completion"]
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
