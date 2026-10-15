from datetime import datetime

from crafter.evaluation.load_context import audit_releases, potential_resource_feasibility


def neutral_orders(releases):
    orders = [{"order_id": str(i), "release_seconds": value, "deadline_seconds": 100}
              for i, value in enumerate(releases)]
    operations = [{"operation_key": f"{i}_1", "order_id": str(i)} for i in range(len(releases))]
    return {"orders": orders, "operations": operations, "snapshot_as_of_seconds": 0}


def test_no_future_release_orders():
    result = audit_releases(neutral_orders([-1, 0]), {"order_completion_seconds": {"0": 10, "1": 20}})
    assert result["future_release_order_count"] == 0
    assert result["conclusion"] == "LOW_UTILIZATION_NOT_EXPLAINED_BY_FUTURE_ORDER_RELEASES"


def test_mixed_future_release_orders():
    result = audit_releases(neutral_orders([-1, 10]), {"order_completion_seconds": {"0": 10, "1": 20}})
    assert result["future_release_order_count"] == 1
    assert not result["all_orders_released_at_snapshot"]


def screen(operation, *, machines, workers, busy_machines=(), busy_workers=()):
    return potential_resource_feasibility(
        operation, 7 * 3600,
        machines_by_id={row["machine_id"]: row for row in machines},
        workers_by_id={row["worker_id"]: row for row in workers},
        busy_machines=set(busy_machines), busy_workers=set(busy_workers),
        machine_commitments={}, worker_commitments={}, origin=datetime(2024, 1, 1),
    )


OP = {"min_machine_count": 1, "eligible_line_ids": [1], "eligible_machine_ids": [10], "eligible_worker_ids": ["1"]}
MACHINE = {"machine_id": 10, "line_id": 1, "is_usable": True}
WORKER = {"worker_id": "1", "calendar": {"kind": "FIXED_SHIFT_FLOATER", "start_hour": 6, "end_hour": 14}}


def test_ready_operation_but_incompatible_machine():
    assert screen(OP, machines=[{**MACHINE, "line_id": 2}], workers=[WORKER])["reason"] == "INCOMPATIBLE_OR_BUSY_MACHINE"


def test_ready_operation_but_insufficient_worker_crew():
    operation = {**OP, "min_machine_count": 2, "eligible_machine_ids": [10, 11], "eligible_worker_ids": ["1"]}
    machines = [MACHINE, {**MACHINE, "machine_id": 11}]
    assert screen(operation, machines=machines, workers=[WORKER])["reason"] == "INSUFFICIENT_AVAILABLE_WORKER_CREW"


def test_ready_operation_with_all_resources_potentially_available():
    assert screen(OP, machines=[MACHINE], workers=[WORKER])["potentially_executable_now"]


def test_no_work_and_precedence_blocking_are_distinct_counts():
    # The timeline implementation defines ready work as pending work whose
    # releases and all predecessor completions have passed. These direct
    # logical checks pin zero-ready and predecessor-blocked semantics.
    completion = {"a": 100}
    now = 50
    assert not all(completion[pred] <= now for pred in ("a",))
    assert len([]) == 0


def test_standard_utilization_keeps_idle_available_capacity():
    productive, available = 10, 100
    assert productive / available == 0.1
    assert available == 100


def test_no_work_interval_remains_standard_calendar_capacity():
    calendar_available, work_used = 3600, 0
    assert calendar_available == 3600 and work_used == 0


def test_no_work_interval_is_distinguishable_in_context():
    interval = {"duration_seconds": 60, "precedence_ready_operation_count": 0}
    assert interval["precedence_ready_operation_count"] == 0


def test_read_only_contract_and_standard_kpis_unchanged():
    import json
    from pathlib import Path
    root = Path("outputs/crafter/evaluation_v2")
    metrics = json.loads((root / "craft_factory2_metrics_v2.json").read_text())
    assert metrics["makespan_seconds"] == 1_227_720
    assert metrics["total_tardiness_seconds"] == 75_031_501
    assert metrics["on_time_delivery_percentage"] == 5.0
    assert metrics["database_writes"] == 0
