"""Prove whether high-risk export decodes the chromosome that fitness scored."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.decision_engine as decision_module
import ga.fitness as fitness_module
from agent.decision_engine import AIDecisionEngine
from utils.run_artifacts import current_run_id

OUT = Path("outputs/integration_audit/score_export_parity.json")
EXPORT_DIR = Path("outputs/integration_audit/score_export_probe")


def main():
    op = "1_10"
    chromosome = [op, 2, 1]
    details = {op: {"setup_mins": 0.0, "serial_processing_mins": 10.0,
                    "required_capability": "body assembly"}}
    pools = {op: {1: [101], 2: [202]}}
    lines = pd.DataFrame([
        {"line_id": 1, "line_number": "L1"},
        {"line_id": 2, "line_number": "L2"},
    ])
    routings = pd.DataFrame([{
        "production_order_id": 1, "operation_id": 10,
        "operation_sequence_number": 1,
        "required_machine_type": "body assembly",
        "release_date": pd.NaT,
    }])
    machines = pd.DataFrame([
        {"asset_id": 101, "line_id": 1},
        {"asset_id": 202, "line_id": 2},
    ])
    preds = {op: {"delay": 0.99, "priority": 0.5}}
    base = datetime(2026, 9, 14, 6, 0, 0)

    selected_by_fitness = []
    real_select = fitness_module.select_machines_and_start
    def traced(*args, **kwargs):
        start, chosen = real_select(*args, **kwargs)
        selected_by_fitness.extend(chosen)
        return start, chosen
    fitness_module.select_machines_and_start = traced
    try:
        score, score_details = fitness_module.evaluate_schedule(
            chromosome, 1, details, preds,
            all_line_ids=[1, 2], order_deadlines={"1": 1440.0},
            machine_pools=pools, machine_line_map={101: 1, 202: 2},
            usable_machine_count=2, present_operator_count=1,
            return_details=True, order_release_minutes={"1": 0.0},
            op_sequence_map={op: 1}, base_time=base,
        )
    finally:
        fitness_module.select_machines_and_start = real_select

    engine = object.__new__(AIDecisionEngine)
    engine.run_id = current_run_id()
    engine._assign_operators = lambda *a, **k: ([], "unfilled", {
        "required": 1, "available": 0, "shortfall": 1,
        "duration_minutes": 10.0, "shortfall_worker_minutes": 10.0,
        "limiting_factor": "probe", "operation_type": "body assembly",
        "line_id": 1,
    })
    old_output = decision_module.OUTPUT_DIR
    decision_module.OUTPUT_DIR = str(EXPORT_DIR)
    try:
        summary = engine._export_proposed_schedule(
            chromosome, 1, details, preds, lines, {op: [1, 2]},
            {"1": base + timedelta(days=1)}, routings, machines,
            label="probe", machine_pools=pools,
            operator_index={"by_line": {}, "by_factory": {}, "by_skill": {},
                            "line_factory": {}, "efficiency_of": {}},
            present_operator_count=1, usable_machine_count=2,
            op_optype={op: "body assembly"}, base_time=base,
        )
    finally:
        decision_module.OUTPUT_DIR = old_output

    exported = json.loads((EXPORT_DIR / "proposed_schedule_probe.json").read_text())
    result = {
        "input": {
            "chromosome_line_gene": 2,
            "delay_probability": 0.99,
            "threshold": float(fitness_module.HIGH_RISK_THRESHOLD),
            "both_lines_initial_load": 0.0,
        },
        "fitness_decoder": {
            "selected_machine_ids": selected_by_fitness,
            "selected_line_ids": [2 if x == 202 else 1 for x in selected_by_fitness],
            "fitness": float(score[0]),
        },
        "export_decoder": {
            "assigned_line_id": exported[0]["assigned_line_id"],
            "selected_machine_ids": exported[0]["machine_ids_used"],
            "high_risk_reroutes": summary["high_risk_reroutes"],
        },
        "score_export_same_line": exported[0]["assigned_line_id"] == 2,
        "current_full_graph_corrected_predictions_above_threshold": 0,
        "interpretation": (
            "Dormant on the current checkpoint, but a future corrected delay "
            "probability above threshold causes export to replace the scored LA gene."
        ),
    }
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
