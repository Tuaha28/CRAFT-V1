"""Reproduce authoritative same-worker phenotype/export/validator parity."""
import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import agent.decision_engine as decision_module
from agent.decision_engine import AIDecisionEngine
from ga.fitness import evaluate_schedule
from validation.schedule_validator import (
    ScheduleValidationContext,
    validate_exported_schedule,
)

OUT = Path("outputs/integration_audit/authoritative_phenotype_parity.json")
EXPORT_DIR = Path("outputs/integration_audit/authoritative_phenotype_export")
ORIGIN = datetime(2026, 3, 2, 6, 0)
DEADLINE = datetime(2026, 3, 3, 7, 0)
WORKER = 7001
MACHINE_TYPE = "single-needle lockstitch"
OPTYPE = "body assembly"


def main():
    operations = ["1_1", "1_2"]
    chromosome = operations + [1, 1] + [1, 1]
    details = {
        "1_1": {"setup_mins": 0.0, "serial_processing_mins": 600.0,
                 "required_capability": OPTYPE},
        "1_2": {"setup_mins": 0.0, "serial_processing_mins": 60.0,
                 "required_capability": OPTYPE},
    }
    predictions = {key: {"delay": 0.0, "priority": 0.5} for key in operations}
    machine_pools = {"1_1": {1: [101]}, "1_2": {1: [102]}}
    operator_index = {
        "by_line": {(1, OPTYPE): [WORKER]},
        "by_factory": {(1, OPTYPE): [WORKER]},
        "by_skill": {OPTYPE: [WORKER]},
        "line_factory": {1: 1},
        "shift_of": {WORKER: (6.0, 14.0)},
        "crew_of": {},
        "efficiency_of": {(WORKER, OPTYPE): 84.0},
    }
    common = dict(
        num_ops=2,
        op_details=details,
        gnn_predictions=predictions,
        all_line_ids=[1],
        order_deadlines={"1": (DEADLINE - ORIGIN).total_seconds() / 60.0},
        machine_pools=machine_pools,
        machine_line_map={101: 1, 102: 1},
        usable_machine_count=2,
        present_operator_count=1,
        initial_machine_free={},
        initial_machine_last_order={},
        scored_order_ids={"1"},
        op_optype={key: OPTYPE for key in operations},
        order_release_minutes={"1": 0.0},
        op_sequence_map={"1_1": 1, "1_2": 2},
        base_time=ORIGIN,
        operator_index=operator_index,
        initial_operator_reservations={},
    )
    (fitness,), phenotype = evaluate_schedule(
        chromosome, return_details=True, **common
    )

    routings = pd.DataFrame([
        {
            "production_order_id": 1,
            "operation_id": index,
            "operation_sequence_number": index,
            "required_machine_type": MACHINE_TYPE,
            "release_date": ORIGIN,
        }
        for index in (1, 2)
    ])
    original_output = decision_module.OUTPUT_DIR
    decision_module.OUTPUT_DIR = str(EXPORT_DIR)
    try:
        summary = AIDecisionEngine()._export_proposed_schedule(
            chromosome, 2, details, predictions,
            pd.DataFrame({
                "line_id": [1], "line_number": ["L1"], "is_active": [1]
            }),
            {"1_1": [1], "1_2": [1]},
            {"1": DEADLINE},
            routings,
            pd.DataFrame({
                "asset_id": [101, 102], "line_id": [1, 1]
            }),
            label="authoritative_parity",
            machine_pools=machine_pools,
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
        decision_module.OUTPUT_DIR = original_output

    rows = json.loads(Path(summary["schedule_file"]).read_text(encoding="utf-8"))
    by_key = {
        f"{row['production_order_id']}_{row['operation_id']}": row
        for row in rows
    }
    expected = [
        {
            "production_order_id": "1",
            "operation_id": str(index),
            "operation_sequence_number": index,
            "release_time": ORIGIN,
            "deadline": DEADLINE,
            "eligible_line_ids": [1],
            "required_machine_type": MACHINE_TYPE,
            "required_operation_type": OPTYPE,
            "required_machine_count": 1,
            "required_workers": 1,
            "productive_minutes": details[f"1_{index}"]["serial_processing_mins"],
            "setup_minutes": 0.0,
        }
        for index in (1, 2)
    ]
    context = ScheduleValidationContext(
        expected_operations=expected,
        ownership_by_order={"1": 1},
        line_factory={1: 1},
        machines={
            machine_id: {
                "line_id": 1,
                "factory_id": 1,
                "machine_type": MACHINE_TYPE,
                "capabilities": [MACHINE_TYPE],
            }
            for machine_id in (101, 102)
        },
        workers={WORKER: {"factory_id": 1, "skills": [OPTYPE]}},
        worker_is_available=lambda _eid, when: 6 <= when.hour < 14,
    )
    validation = validate_exported_schedule(rows, context)

    decoded = phenotype["phenotype_operations"]
    parity = {
        "phenotype_hash": summary["phenotype_hash"] == phenotype["phenotype_hash"],
        "fitness": abs(summary["fitness"] - fitness) < 1e-12,
        "line": all(
            by_key[row["operation_key"]]["assigned_line_id"] == row["line_id"]
            for row in decoded
        ),
        "machines": all(
            [str(v) for v in by_key[row["operation_key"]]["machine_ids_used"]]
            == [str(v) for v in row["machine_ids"]]
            for row in decoded
        ),
        "employees": all(
            [str(v) for v in by_key[row["operation_key"]]["employee_ids_used"]]
            == row["employee_ids"]
            for row in decoded
        ),
        "start_end": all(
            by_key[row["operation_key"]]["proposed_start_time"]
            == (ORIGIN + __import__("datetime").timedelta(
                minutes=row["start_minutes"]
            )).isoformat()
            and by_key[row["operation_key"]]["cushioned_end_time"]
            == (ORIGIN + __import__("datetime").timedelta(
                minutes=row["end_minutes"]
            )).isoformat()
            for row in decoded
        ),
    }
    result = {
        "contract": "same-worker pause/resume; machine held through pauses",
        "fitness": fitness,
        "fitness_components": phenotype["fitness_components"],
        "phenotype_hash": phenotype["phenotype_hash"],
        "export_phenotype_hash": summary["phenotype_hash"],
        "operation_1_end": by_key["1_1"]["cushioned_end_time"],
        "successor_start": by_key["1_2"]["proposed_start_time"],
        "order_completion": (
            ORIGIN + __import__("datetime").timedelta(
                minutes=phenotype["order_completion_minutes"]["1"]
            )
        ).isoformat(),
        "tardiness_hours": phenotype["order_lateness_hours"]["1"],
        "worker_segments": by_key["1_1"]["worker_segments_by_employee"],
        "parity": parity,
        "parity_all": all(parity.values()),
        "independent_validator": validation.as_dict(),
    }
    assert result["operation_1_end"] == "2026-03-03T08:00:00"
    assert result["successor_start"] == "2026-03-03T08:00:00"
    assert result["order_completion"] == "2026-03-03T09:00:00"
    assert result["tardiness_hours"] == 2.0
    assert result["parity_all"]
    assert validation.valid, validation.errors
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
