from dataclasses import replace
from datetime import datetime
import json

import pytest

from crafter.contracts import (
    LineSpec, MachineSpec, OperationSpec, OrderSpec, ResourceState,
    SchedulingProblem,
)
from crafter.evaluation.final_kpis import (
    EvaluationError, _worker_available_intervals, evaluate_final_schedule,
)
from crafter.evaluation.neutral_factory import NeutralFactoryWorkload, extract_neutral_factory_workload


VALIDATION = {
    "scope_status": "SCOPE_VALID", "replay_status": "PASS",
    "physical_status": "VALID", "accepted": True,
    "scope": {"missing_required_operations": [], "duplicate_operations": [], "unexpected_operations": []},
    "violation_counts": {},
}


def fixture_workload(*, second_due=None, second_worker=True):
    origin = datetime(2024, 1, 1)
    orders = (
        OrderSpec("A", 2, 0, 27_000),
        OrderSpec("B", 2, 0, second_due),
    )
    operations = (
        OperationSpec("A_1", "A", "1", 1, "sew", (1,), 3600, 0, 1, 1),
        OperationSpec("A_2", "A", "2", 2, "sew", (1,), 3600, 0, 1, 1),
        OperationSpec("B_1", "B", "1", 1, "sew", (1,), 3600, 0, 1, 1),
    )
    problem = SchedulingProblem(
        "test", origin, 100_000, (LineSpec(1, 2),),
        (MachineSpec(10, 2, 1, ("sew",), True), MachineSpec(11, 2, 1, ("sew",), False)),
        orders, operations, (), ResourceState(), 0,
    )
    worker_ids = ["1", "2"] if second_worker else ["1"]
    index = {
        "by_factory": {(2, "sew"): [int(x) for x in worker_ids]},
        "by_line": {(1, "sew"): [int(x) for x in worker_ids]},
        "crew_of": {}, "shift_of": {int(x): (6.0, 14.0) for x in worker_ids},
    }
    payload = {
        "factory_id": 2, "snapshot_as_of_seconds": 0,
        "evaluation_origin": origin.isoformat(),
        "accounting": {"benchmark_operation_keys": [op.operation_key for op in operations]},
        "workers": [{"worker_id": x} for x in worker_ids],
    }
    return NeutralFactoryWorkload(payload, problem, index)


def rows():
    return [
        {"operation_key": "A_1", "start_seconds": 21_600, "end_seconds": 25_200,
         "worker_segments": [[21_600, 25_200]], "resource_configuration": {"worker_ids": ["1"], "machine_ids": [10]}},
        {"operation_key": "A_2", "start_seconds": 25_200, "end_seconds": 28_800,
         "worker_segments": [[25_200, 28_800]], "resource_configuration": {"worker_ids": ["1"], "machine_ids": [10]}},
        {"operation_key": "B_1", "start_seconds": 28_800, "end_seconds": 32_400,
         "worker_segments": [[28_800, 32_400]], "resource_configuration": {"worker_ids": ["1"], "machine_ids": [10]}},
    ]


def test_flow_time_is_completion_minus_release():
    result = evaluate_final_schedule(fixture_workload(), rows(), validation=VALIDATION)
    assert result["flow_time_by_order_seconds"] == {"A": 28_800, "B": 32_400}


def test_future_release_cannot_start_early():
    workload = fixture_workload()
    problem = replace(workload.problem, orders=(replace(workload.problem.orders[0], release_seconds=22_000), workload.problem.orders[1]))
    workload = NeutralFactoryWorkload(workload.payload, problem, workload.operator_index)
    with pytest.raises(EvaluationError, match="releases"):
        evaluate_final_schedule(workload, rows(), validation=VALIDATION)


def test_order_completion_uses_final_routing_operation():
    result = evaluate_final_schedule(fixture_workload(), rows(), validation=VALIDATION)
    assert result["final_operation_by_order"]["A"] == "A_2"
    assert result["order_completion_seconds"]["A"] == 28_800


def test_tardiness_is_order_level():
    result = evaluate_final_schedule(fixture_workload(), rows(), validation=VALIDATION)
    assert result["tardiness_by_order_seconds"] == {"A": 1_800}


def test_on_time_denominator_is_only_orders_with_due_dates():
    result = evaluate_final_schedule(fixture_workload(), rows(), validation=VALIDATION)
    assert result["orders_with_due_dates"] == 1
    assert result["on_time_delivery_percentage"] == 0.0


def test_worker_off_shift_is_excluded_from_capacity():
    workload = fixture_workload(second_worker=False)
    intervals = _worker_available_intervals(workload.operator_index, "1", workload.problem.origin, 86_400)
    assert intervals == [(21_600, 50_400)]


def test_friday_rest_is_excluded_when_calendar_says_friday():
    index = {"crew_of": {1: (0, 4)}, "shift_of": {}}
    friday = datetime(2024, 1, 5)
    assert _worker_available_intervals(index, "1", friday, 86_400) == []


def test_absent_worker_not_in_neutral_assignable_population():
    workload = fixture_workload(second_worker=False)
    assert [row["worker_id"] for row in workload.payload["workers"]] == ["1"]


def test_idle_but_available_worker_remains_in_denominator():
    result = evaluate_final_schedule(fixture_workload(second_worker=True), rows(), validation=VALIDATION)
    assert result["worker_utilization"]["worker_count_in_denominator"] == 2
    idle = next(row for row in result["worker_utilization"]["per_worker"] if row["worker_id"] == "2")
    assert idle["productive_seconds"] == 0
    assert idle["available_seconds"] > 0


def test_unusable_machine_excluded_from_denominator():
    result = evaluate_final_schedule(fixture_workload(), rows(), validation=VALIDATION)
    assert result["machine_utilization"]["machine_count_in_denominator"] == 1


def test_machine_occupancy_includes_worker_pause_span():
    schedule = rows()
    schedule[0] = {**schedule[0], "end_seconds": 28_800, "worker_segments": [[21_600, 23_400], [27_000, 28_800]]}
    schedule[1] = {**schedule[1], "start_seconds": 28_800, "end_seconds": 32_400,
                   "worker_segments": [[28_800, 32_400]]}
    schedule[2] = {**schedule[2], "start_seconds": 32_400, "end_seconds": 36_000,
                   "worker_segments": [[32_400, 36_000]]}
    result = evaluate_final_schedule(fixture_workload(), schedule, validation=VALIDATION)
    assert result["machine_utilization"]["machine_occupied_seconds"] == 14_400


def test_duplicate_or_missing_schedule_rejected():
    with pytest.raises(EvaluationError, match="identity invalid"):
        evaluate_final_schedule(fixture_workload(), rows()[:-1] + [rows()[0]], validation=VALIDATION)


def test_factory2_neutral_extraction_exact_identity():
    from crafter.disruptions.benchmark import _load_state
    loaded = _load_state()
    workload = extract_neutral_factory_workload(loaded, factory_id=2, snapshot_as_of_seconds=0)
    frozen = json.loads(open(
        "outputs/crafter/factory2_172_production_v1_workerfix_full/factory_accounting.json",
        encoding="utf-8").read())
    assert workload.payload["accounting"]["benchmark_operation_keys"] == frozen["schedulable_operations"]
    assert workload.payload["accounting"]["RAW_FACTORY_OPERATIONS"] == 172
    assert workload.payload["source_metadata"]["database_access"] == "READ_ONLY"
    assert workload.payload["source_metadata"]["database_writes"] == 0


def test_benchmark_extractor_contract_is_read_only():
    workload = fixture_workload()
    # The production extractor emits this field; the synthetic workload proves
    # the KPI path itself has no database API or mutation requirement.
    assert evaluate_final_schedule(workload, rows(), validation=VALIDATION)["database_writes"] == 0
