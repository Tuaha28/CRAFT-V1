import copy
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

import agent.decision_engine as decision_module
from agent.decision_engine import AIDecisionEngine
from database import queries
from test_schedule_exporter import _build_scenario
from validation.schedule_validator import (
    ScheduleValidationContext,
    validate_exported_schedule,
)


MONDAY = datetime(2026, 3, 2, 6, 0)
TUESDAY = datetime(2026, 3, 3, 6, 0)
WORKER = 7001
MACHINE_TYPE = "single-needle lockstitch"


def _index():
    return {
        "by_line": {(1, "body assembly"): [WORKER]},
        "by_factory": {(1, "body assembly"): [WORKER]},
        "by_skill": {"body assembly": [WORKER]},
        "line_factory": {1: 1},
        "shift_of": {WORKER: (6.0, 14.0)},
        "crew_of": {},
        "efficiency_of": {(WORKER, "body assembly"): 84.0},
    }


def _day_worker(_index, _employee, when):
    return 6 <= when.hour < 14


def _export(tmp_path, monkeypatch, orders, serial_by_key, *, base_time=MONDAY,
            reservations=None, shared_machine=False, label="seg"):
    monkeypatch.setattr(decision_module, "OUTPUT_DIR", str(tmp_path))
    kwargs = _build_scenario(orders)
    kwargs["operator_index"] = _index()
    for key, work in serial_by_key.items():
        kwargs["op_details"][key] = {
            "setup_mins": 0.0,
            "serial_processing_mins": float(work),
        }
    kwargs["df_routings"]["required_machine_type"] = MACHINE_TYPE
    if shared_machine:
        machine = int(kwargs["df_machines"].iloc[0]["asset_id"])
        kwargs["df_machines"] = pd.DataFrame(
            {"asset_id": [machine], "line_id": [1]}
        )
        for key in kwargs["machine_pools"]:
            kwargs["machine_pools"][key] = {1: [machine]}
    summary = AIDecisionEngine()._export_proposed_schedule(
        **kwargs,
        initial_operator_reservations=reservations or {},
        base_time=base_time,
        label=label,
    )
    rows = json.loads(
        __import__("pathlib").Path(summary["schedule_file"]).read_text()
    )
    return summary, rows


def _dt(value):
    return datetime.fromisoformat(value)


def test_one_segment_operation_keeps_one_exact_worker_segment(tmp_path, monkeypatch):
    _summary, rows = _export(
        tmp_path, monkeypatch, {"1": {"num_ops": 1}}, {"1_1": 120}
    )
    row = rows[0]
    assert row["worker_segments_by_employee"][str(WORKER)] == [{
        "start": MONDAY.isoformat(),
        "end": (MONDAY + timedelta(minutes=120)).isoformat(),
    }]
    assert _dt(row["cushioned_end_time"]) == MONDAY + timedelta(minutes=120)


def test_cross_shift_pause_resume_holds_machine_and_completion(tmp_path, monkeypatch):
    _summary, rows = _export(
        tmp_path, monkeypatch, {"1": {"num_ops": 1}}, {"1_1": 600}
    )
    row = rows[0]
    assert row["worker_segments_by_employee"][str(WORKER)] == [
        {"start": MONDAY.isoformat(),
         "end": datetime(2026, 3, 2, 14, 0).isoformat()},
        {"start": TUESDAY.isoformat(),
         "end": datetime(2026, 3, 3, 8, 0).isoformat()},
    ]
    assert _dt(row["cushioned_end_time"]) == datetime(2026, 3, 3, 8, 0)


def test_multi_day_pause_resume(tmp_path, monkeypatch):
    _summary, rows = _export(
        tmp_path, monkeypatch, {"1": {"num_ops": 1}}, {"1_1": 1000}
    )
    segments = rows[0]["worker_segments_by_employee"][str(WORKER)]
    assert len(segments) == 3
    assert _dt(rows[0]["cushioned_end_time"]) == datetime(2026, 3, 4, 6, 40)


def test_successor_and_second_machine_job_wait_for_segmented_completion(
        tmp_path, monkeypatch):
    _summary, rows = _export(
        tmp_path,
        monkeypatch,
        {"1": {"num_ops": 2}},
        {"1_1": 600, "1_2": 60},
        shared_machine=True,
    )
    first = next(r for r in rows if r["operation_id"] == "1")
    second = next(r for r in rows if r["operation_id"] == "2")
    assert _dt(first["cushioned_end_time"]) == datetime(2026, 3, 3, 8, 0)
    assert _dt(second["proposed_start_time"]) >= _dt(first["cushioned_end_time"])


def test_segments_and_string_id_survive_actual_export_boundary(
        tmp_path, monkeypatch):
    first, rows = _export(
        tmp_path, monkeypatch, {"1": {"num_ops": 1}}, {"1_1": 600},
        label="window1",
    )
    carried = first["operator_reservations_out"]
    assert set(carried) == {str(WORKER)}
    assert carried[str(WORKER)][1] == (
        TUESDAY, datetime(2026, 3, 3, 8, 0)
    )

    second, second_rows = _export(
        tmp_path,
        monkeypatch,
        {"2": {"num_ops": 1}},
        {"2_1": 60},
        base_time=TUESDAY,
        reservations=carried,
        label="window2",
    )
    second_segments = second_rows[0]["worker_segments_by_employee"][str(WORKER)]
    assert second_segments == [{
        "start": datetime(2026, 3, 3, 8, 0).isoformat(),
        "end": datetime(2026, 3, 3, 9, 0).isoformat(),
    }]
    assert second["operator_reservations_out"][str(WORKER)][1] == (
        TUESDAY, datetime(2026, 3, 3, 9, 0)
    )


def test_database_loader_preserves_only_future_productive_segments(monkeypatch):
    frame = pd.DataFrame([{
        "schedule_item_id": 9,
        "production_order_id": 1,
        "operation_id": 11,
        "asset_id": 21,
        "employee_id": WORKER,
        "planned_start_time": MONDAY,
        "planned_end_time": datetime(2026, 3, 3, 8, 0),
        "planned_work_segments": json.dumps([
            [MONDAY.isoformat(), datetime(2026, 3, 2, 14, 0).isoformat()],
            [TUESDAY.isoformat(), datetime(2026, 3, 3, 8, 0).isoformat()],
        ]),
        "execution_status": "Running",
    }])
    monkeypatch.setattr(queries, "get_engine", lambda: object())
    monkeypatch.setattr(queries.pd, "read_sql", lambda *a, **k: frame)
    now = datetime(2026, 3, 3, 5, 0)
    records = queries.fetch_operator_reservation_records(now)
    assert records[0].employee_id == str(WORKER)
    assert records[0].segments == ((
        TUESDAY, datetime(2026, 3, 3, 8, 0)
    ),)
    carried = queries.fetch_committed_operator_reservations(now)
    assert carried == {str(WORKER): [
        (TUESDAY, datetime(2026, 3, 3, 8, 0))
    ]}


def _validator_context():
    return ScheduleValidationContext(
        expected_operations=[{
            "production_order_id": "1",
            "operation_id": "1",
            "operation_sequence_number": 1,
            "release_time": MONDAY,
            "deadline": datetime(2026, 3, 5),
            "eligible_line_ids": [1],
            "required_machine_type": MACHINE_TYPE,
            "required_operation_type": "body assembly",
            "required_machine_count": 1,
            "required_workers": 1,
            "productive_minutes": 600.0,
            "setup_minutes": 0.0,
        }],
        ownership_by_order={"1": 1},
        line_factory={1: 1},
        machines={1001: {
            "line_id": 1, "factory_id": 1,
            "machine_type": MACHINE_TYPE,
            "capabilities": [MACHINE_TYPE],
        }},
        workers={WORKER: {
            "factory_id": 1, "skills": ["body assembly"]
        }},
        worker_is_available=lambda _eid, when: 6 <= when.hour < 14,
    )


def test_independent_validator_accepts_segmented_export_and_rejects_bad_evidence(
        tmp_path, monkeypatch):
    _summary, rows = _export(
        tmp_path, monkeypatch, {"1": {"num_ops": 1}}, {"1_1": 600}
    )
    report = validate_exported_schedule(rows, _validator_context())
    assert report.valid, report.errors

    missing = copy.deepcopy(rows)
    del missing[0]["worker_segments_by_employee"][str(WORKER)]
    report = validate_exported_schedule(missing, _validator_context())
    assert "worker_segments_missing" in {e["code"] for e in report.errors}

    wrong_end = copy.deepcopy(rows)
    wrong_end[0]["cushioned_end_time"] = datetime(
        2026, 3, 3, 9, 0).isoformat()
    report = validate_exported_schedule(wrong_end, _validator_context())
    assert "operation_completion_worker_mismatch" in {
        e["code"] for e in report.errors
    }
