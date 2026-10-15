from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from validation.schedule_validator import ScheduleValidationContext, validate_exported_schedule

T0 = datetime(2026, 1, 5, 6, 0)


def expected(order="o1", op="1", seq=1, line=10, release=None,
             deadline=None, productive=60, workers=1):
    return {
        "production_order_id": order, "operation_id": op,
        "operation_sequence_number": seq, "release_time": release,
        "deadline": deadline, "eligible_line_ids": [line],
        "required_machine_type": "sewing",
        "required_operation_type": "stitch",
        "required_machine_count": 1, "required_workers": workers,
        "setup_minutes": 15, "productive_minutes": productive,
    }


def row(order="o1", op="1", line=10, machine=101, worker="e1",
        start=T0, productive=60):
    work_start = start + timedelta(minutes=15)
    end = work_start + timedelta(minutes=productive)
    return {
        "production_order_id": order, "operation_id": op,
        "assigned_line_id": line, "machines_used": 1,
        "machine_ids_used": [machine], "operators_used": 1,
        "employee_ids_used": [worker],
        "proposed_start_time": start.isoformat(),
        "base_end_time": end.isoformat(), "cushioned_end_time": end.isoformat(),
        "worker_segments_by_employee": {
            worker: [(work_start.isoformat(), end.isoformat())]},
    }


def context(expected_rows=None, **changes):
    values = dict(
        expected_operations=expected_rows or [expected()],
        ownership_by_order={"o1": 1, "o2": 1},
        line_factory={10: 1, 20: 2},
        machines={
            101: {"line_id": 10, "factory_id": 1, "machine_type": "sewing",
                  "capabilities": {"sewing"}},
            102: {"line_id": 10, "factory_id": 1, "machine_type": "sewing",
                  "capabilities": {"sewing"}},
            201: {"line_id": 20, "factory_id": 2, "machine_type": "cutting",
                  "capabilities": {"cutting"}},
        },
        workers={
            "e1": {"factory_id": 1, "skills": {"stitch"}},
            "e2": {"factory_id": 1, "skills": {"stitch"}},
            "e3": {"factory_id": 2, "skills": {"cut"}},
        },
        worker_is_available=lambda _e, t: 6 <= t.hour < 14,
    )
    values.update(changes)
    return ScheduleValidationContext(**values)


def codes(report):
    return {e["code"] for e in report.errors}


def test_single_operation_valid_and_reconstructs_completion():
    scheduled = row()
    report = validate_exported_schedule([scheduled], context())
    assert report.valid, report.errors
    assert report.counts["matched_operations"] == 1
    assert report.reconstructed["order_completion"]["o1"] == scheduled["cushioned_end_time"]


def test_missing_duplicate_and_unexpected_are_rejected():
    ex = [expected(), expected(op="2", seq=2)]
    duplicate = row()
    surprise = row(order="o2", op="9", machine=102, worker="e2")
    report = validate_exported_schedule([duplicate, duplicate, surprise], context(ex))
    assert {"duplicate_scheduled_operation", "missing_operation",
            "unexpected_operation"} <= codes(report)


def test_precedence_and_release_are_checked():
    ex = [expected(op="1", seq=1, release=T0),
          expected(op="2", seq=2, release=T0)]
    first = row(op="1", machine=101, worker="e1")
    second = row(op="2", machine=102, worker="e2",
                 start=T0+timedelta(minutes=30))
    assert "precedence_violation" in codes(
        validate_exported_schedule([first, second], context(ex)))
    ex[0]["release_time"] = T0+timedelta(hours=1)
    assert "release_violation" in codes(
        validate_exported_schedule([first, second], context(ex)))


def test_machine_overlap_and_committed_interval_are_rejected():
    ex = [expected(order="o1"), expected(order="o2")]
    a = row(order="o1", machine=101, worker="e1")
    b = row(order="o2", machine=101, worker="e2",
            start=T0+timedelta(minutes=20))
    assert "machine_overlap" in codes(
        validate_exported_schedule([a, b], context(ex)))
    committed = [{"resource_id": 102, "start": T0,
                  "end": T0+timedelta(hours=2)}]
    b = row(order="o2", machine=102, worker="e2")
    assert "machine_overlap" in codes(validate_exported_schedule(
        [a, b], context(ex, committed_machine_intervals=committed)))


def test_half_open_machine_intervals_allow_exact_boundary():
    ex = [expected(order="o1"), expected(order="o2")]
    a = row(order="o1", machine=101, worker="e1")
    b = row(order="o2", machine=101, worker="e2",
            start=datetime.fromisoformat(a["cushioned_end_time"]))
    assert "machine_overlap" not in codes(
        validate_exported_schedule([a, b], context(ex)))


@pytest.mark.parametrize("change,code", [
    (lambda r: r.update(assigned_line_id=20), "line_factory_mismatch"),
    (lambda r: r.update(machine_ids_used=[201]), "machine_line_mismatch"),
    (lambda r: r.update(machine_ids_used=[999]), "unknown_machine"),
])
def test_factory_line_and_machine_identity(change, code):
    scheduled = row()
    change(scheduled)
    assert code in codes(validate_exported_schedule([scheduled], context()))


def test_capability_and_line_eligibility_are_checked():
    scheduled = row()
    ctx = context()
    ctx.machines[101]["machine_type"] = "cutting"
    ctx.machines[101]["capabilities"] = {"cutting"}
    assert "machine_capability_mismatch" in codes(
        validate_exported_schedule([scheduled], ctx))
    scheduled = row(line=20)
    assert "ineligible_line" in codes(
        validate_exported_schedule([scheduled], context()))


def test_worker_factory_skill_shortage_and_overlap():
    ex = [expected(order="o1"), expected(order="o2")]
    a = row(order="o1", worker="e1")
    b = row(order="o2", machine=102, worker="e1",
            start=T0+timedelta(minutes=20))
    assert "worker_overlap" in codes(
        validate_exported_schedule([a, b], context(ex)))
    bad = row(worker="e3")
    report = validate_exported_schedule([bad], context())
    assert {"worker_factory_mismatch", "worker_skill_mismatch"} <= codes(report)
    bad = row()
    bad["employee_ids_used"] = []
    assert "worker_shortage" in codes(
        validate_exported_schedule([bad], context()))


def test_segmented_execution_requires_explicit_segment_evidence():
    scheduled = row()
    del scheduled["worker_segments_by_employee"]
    report = validate_exported_schedule([scheduled], context())
    assert not report.valid
    assert "worker_segments_missing" in codes(report)


def test_shift_crossing_segments_and_calendar():
    ex = [expected(productive=600)]
    scheduled = row(productive=600)
    end = T0+timedelta(days=1, hours=2, minutes=15)
    scheduled["base_end_time"] = scheduled["cushioned_end_time"] = end.isoformat()
    scheduled["worker_segments_by_employee"]["e1"] = [
        ((T0+timedelta(minutes=15)).isoformat(), T0.replace(hour=14).isoformat()),
        ((T0+timedelta(days=1)).isoformat(), end.isoformat()),
    ]
    report = validate_exported_schedule([scheduled], context(ex))
    assert report.valid, report.errors
    bad = deepcopy(scheduled)
    bad["worker_segments_by_employee"]["e1"][1] = (
        (T0+timedelta(hours=9)).isoformat(),
        (T0+timedelta(hours=11, minutes=15)).isoformat())
    assert "worker_calendar_violation" in codes(
        validate_exported_schedule([bad], context(ex)))


def test_committed_worker_segment_conflict_is_rejected():
    committed = [{"resource_id": "e1", "start": T0+timedelta(minutes=30),
                  "end": T0+timedelta(minutes=45)}]
    assert "worker_overlap" in codes(validate_exported_schedule(
        [row()], context(committed_worker_intervals=committed)))


def test_tardiness_uses_sequence_not_operation_id():
    deadline = T0+timedelta(hours=2)
    ex = [expected(op="99", seq=1, deadline=deadline),
          expected(op="10", seq=2, deadline=deadline)]
    first = row(op="99", machine=101, worker="e1")
    second = row(op="10", machine=102, worker="e2",
                 start=T0+timedelta(hours=2))
    report = validate_exported_schedule([first, second], context(ex))
    assert report.reconstructed["late_orders"] == 1
    assert report.reconstructed["order_completion"]["o1"] == second["cushioned_end_time"]


def test_productive_minutes_and_setup_boundary_are_checked():
    scheduled = row()
    scheduled["worker_segments_by_employee"]["e1"] = [
        (T0.isoformat(), (T0+timedelta(minutes=30)).isoformat())]
    report = validate_exported_schedule([scheduled], context())
    assert {"worker_segment_during_setup",
            "worker_productive_minutes_mismatch"} <= codes(report)
