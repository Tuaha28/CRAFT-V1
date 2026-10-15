"""Committed productive worker segments survive DB/load/decode/export boundary."""
import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import agent.decision_engine as decision_module
from agent.decision_engine import AIDecisionEngine
from database import queries
from ga.fitness import evaluate_schedule
from validation.schedule_validator import (
    ScheduleValidationContext,
    validate_exported_schedule,
)

OUT = Path("outputs/integration_audit/worker_segment_carry_forward.json")
EXPORT_DIR = Path("outputs/integration_audit/worker_segment_carry_export")
MONDAY = datetime(2026, 3, 2, 6)
TUESDAY = datetime(2026, 3, 3, 6)
WINDOW_2_ORIGIN = datetime(2026, 3, 3, 5)
WORKER = 7001
OPTYPE = "body assembly"
MACHINE_TYPE = "single-needle lockstitch"


def main():
    frame = pd.DataFrame([{
        "schedule_item_id": 9,
        "production_order_id": 1,
        "operation_id": 11,
        "asset_id": 101,
        "employee_id": WORKER,
        "planned_start_time": MONDAY,
        "planned_end_time": datetime(2026, 3, 3, 8),
        "planned_work_segments": json.dumps([
            [MONDAY.isoformat(), datetime(2026, 3, 2, 14).isoformat()],
            [TUESDAY.isoformat(), datetime(2026, 3, 3, 8).isoformat()],
        ]),
        "execution_status": "Running",
    }])
    original_engine = queries.get_engine
    original_read_sql = queries.pd.read_sql
    queries.get_engine = lambda: object()
    queries.pd.read_sql = lambda *args, **kwargs: frame
    try:
        records = queries.fetch_operator_reservation_records(WINDOW_2_ORIGIN)
        carried = queries.fetch_committed_operator_reservations(WINDOW_2_ORIGIN)
    finally:
        queries.get_engine = original_engine
        queries.pd.read_sql = original_read_sql

    operator_index = {
        "by_line": {(1, OPTYPE): [WORKER]},
        "by_factory": {(1, OPTYPE): [WORKER]},
        "by_skill": {OPTYPE: [WORKER]},
        "line_factory": {1: 1},
        "shift_of": {WORKER: (6.0, 14.0)},
        "crew_of": {},
        "efficiency_of": {(WORKER, OPTYPE): 84.0},
    }
    chromosome = ["2_1", 1, 1]
    details = {
        "2_1": {
            "setup_mins": 0.0,
            "serial_processing_mins": 60.0,
            "required_capability": OPTYPE,
        }
    }
    common = dict(
        num_ops=1, op_details=details, gnn_predictions={},
        all_line_ids=[1], order_deadlines={},
        machine_pools={"2_1": {1: [102]}}, machine_line_map={102: 1},
        usable_machine_count=1, present_operator_count=1,
        initial_machine_free={}, initial_machine_last_order={},
        order_release_minutes={"2": 0.0}, op_sequence_map={"2_1": 1},
        op_optype={"2_1": OPTYPE}, base_time=WINDOW_2_ORIGIN,
        operator_index=operator_index,
        initial_operator_reservations=carried,
    )
    (_fitness,), phenotype = evaluate_schedule(
        chromosome, return_details=True, **common
    )
    decoded = phenotype["phenotype_operations"][0]

    routings = pd.DataFrame([{
        "production_order_id": 2,
        "operation_id": 1,
        "operation_sequence_number": 1,
        "required_machine_type": MACHINE_TYPE,
        "release_date": WINDOW_2_ORIGIN,
    }])
    original_output = decision_module.OUTPUT_DIR
    decision_module.OUTPUT_DIR = str(EXPORT_DIR)
    try:
        summary = AIDecisionEngine()._export_proposed_schedule(
            chromosome, 1, details, {},
            pd.DataFrame({
                "line_id": [1], "line_number": ["L1"], "is_active": [1]
            }),
            {"2_1": [1]}, {}, routings,
            pd.DataFrame({"asset_id": [102], "line_id": [1]}),
            label="worker_carry",
            machine_pools=common["machine_pools"],
            operator_index=operator_index,
            present_operator_count=1, usable_machine_count=1,
            op_optype=common["op_optype"],
            initial_machine_free={}, initial_machine_last_order={},
            initial_operator_reservations=carried,
            base_time=WINDOW_2_ORIGIN,
        )
    finally:
        decision_module.OUTPUT_DIR = original_output
    rows = json.loads(Path(summary["schedule_file"]).read_text(encoding="utf-8"))

    context = ScheduleValidationContext(
        expected_operations=[{
            "production_order_id": "2", "operation_id": "1",
            "operation_sequence_number": 1,
            "release_time": WINDOW_2_ORIGIN,
            "eligible_line_ids": [1],
            "required_machine_type": MACHINE_TYPE,
            "required_operation_type": OPTYPE,
            "required_machine_count": 1,
            "required_workers": 1,
            "productive_minutes": 60.0,
            "setup_minutes": 0.0,
        }],
        ownership_by_order={"2": 1}, line_factory={1: 1},
        machines={102: {
            "line_id": 1, "factory_id": 1,
            "machine_type": MACHINE_TYPE,
            "capabilities": [MACHINE_TYPE],
        }},
        workers={WORKER: {"factory_id": 1, "skills": [OPTYPE]}},
        committed_worker_intervals=[{
            "resource_id": str(WORKER),
            "start": TUESDAY,
            "end": datetime(2026, 3, 3, 8),
        }],
        worker_is_available=lambda _eid, when: 6 <= when.hour < 14,
    )
    validation = validate_exported_schedule(rows, context)
    expected_segment = (TUESDAY, datetime(2026, 3, 3, 8))
    result = {
        "database_record_employee_id": records[0].employee_id,
        "database_record_segments": [
            [start.isoformat(), end.isoformat()]
            for start, end in records[0].segments
        ],
        "loaded_reservation_keys": sorted(carried),
        "loaded_reservations": {
            key: [[start.isoformat(), end.isoformat()] for start, end in value]
            for key, value in carried.items()
        },
        "future_resumed_segment_preserved": records[0].segments == (expected_segment,),
        "continuous_span_not_substituted": records[0].segments[0][0] == TUESDAY,
        "new_operation_machine_start": (
            WINDOW_2_ORIGIN
            + __import__("datetime").timedelta(minutes=decoded["start_minutes"])
        ).isoformat(),
        "new_productive_work_starts": min(
            start for segments in decoded["worker_segments_by_employee"].values()
            for start, _end in segments
        ).isoformat(),
        "new_worker_segments": {
            employee: [
                [start.isoformat(), end.isoformat()] for start, end in segments
            ]
            for employee, segments
            in decoded["worker_segments_by_employee"].items()
        },
        "decoder_feasible": phenotype["execution_feasible"],
        "phenotype_hash_equal": (
            phenotype["phenotype_hash"] == summary["phenotype_hash"]
        ),
        "independent_validator": validation.as_dict(),
    }
    assert result["database_record_employee_id"] == str(WORKER)
    assert result["loaded_reservation_keys"] == [str(WORKER)]
    assert result["future_resumed_segment_preserved"]
    assert result["continuous_span_not_substituted"]
    assert result["new_operation_machine_start"] == "2026-03-03T05:00:00"
    assert result["new_productive_work_starts"] == "2026-03-03T08:00:00"
    assert result["new_worker_segments"][str(WORKER)] == [[
        "2026-03-03T08:00:00", "2026-03-03T09:00:00"
    ]]
    assert result["decoder_feasible"]
    assert result["phenotype_hash_equal"]
    assert validation.valid, validation.errors
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
