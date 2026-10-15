import json
from datetime import datetime

import pandas as pd
import pytest

import agent.decision_engine as decision_module
from agent.decision_engine import AIDecisionEngine
from ga.fitness import evaluate_schedule


ORIGIN = datetime(2026, 3, 2, 6, 0)


def operator_index(two_lines=False, mixed=False):
    workers = [101, 102] if mixed else [101]
    by_line = {(1, "body assembly"): workers}
    by_factory = {(1, "body assembly"): list(workers)}
    if two_lines:
        by_line[(2, "body assembly")] = [201]
        by_factory[(1, "body assembly")].append(201)
    shifts = {101: (6.0, 14.0)}
    if mixed:
        shifts[102] = (8.0, 16.0)
    if two_lines:
        # Missing roster means always available, intentionally.
        pass
    return {
        "by_line": by_line,
        "by_factory": by_factory,
        "by_skill": {"body assembly": list(by_factory[(1, "body assembly")])},
        "line_factory": {1: 1, 2: 1},
        "shift_of": shifts,
        "crew_of": {},
        "efficiency_of": {
            (employee, "body assembly"): 84.0
            for employee in by_factory[(1, "body assembly")]
        },
    }


def eval_kwargs(num_ops=1, work=600.0, setup=0.0, machines=None, lines=None,
                index=None, deadline=None):
    operations = [f"1_{i + 1}" for i in range(num_ops)]
    machines = machines or {key: {1: [1001]} for key in operations}
    lines = lines or [1]
    individual = operations + list(lines) + [1] * num_ops
    return individual, {
        "num_ops": num_ops,
        "op_details": {
            key: {"setup_mins": setup if i == 0 else 0.0,
                  "serial_processing_mins": work if i == 0 else 60.0,
                  "required_capability": "body assembly"}
            for i, key in enumerate(operations)
        },
        "gnn_predictions": {},
        "all_line_ids": sorted({
            line for pools in machines.values() for line in pools
        }),
        "order_deadlines": {"1": deadline} if deadline is not None else {},
        "machine_pools": machines,
        "machine_line_map": {
            asset: line
            for _key, pools in machines.items()
            for line, assets in pools.items()
            for asset in assets
        },
        "usable_machine_count": len({
            asset for pools in machines.values()
            for assets in pools.values() for asset in assets
        }),
        "present_operator_count": 10,
        "order_release_minutes": {"1": 0.0},
        "op_sequence_map": {key: i + 1 for i, key in enumerate(operations)},
        "op_optype": {key: "body assembly" for key in operations},
        "base_time": ORIGIN,
        "operator_index": index or operator_index(),
        "initial_operator_reservations": {},
    }


def test_setup_is_machine_only_and_worker_starts_after_setup():
    individual, kwargs = eval_kwargs(work=120.0, setup=60.0)
    _fitness, details = evaluate_schedule(
        individual, return_details=True, **kwargs
    )
    row = details["phenotype_operations"][0]
    segments = row["worker_segments_by_employee"]["101"]
    assert row["start_minutes"] == 0.0
    assert row["productive_start_minutes"] == 60.0
    assert segments == ((datetime(2026, 3, 2, 7, 0),
                         datetime(2026, 3, 2, 9, 0)),)
    assert row["end_minutes"] == 180.0


def test_two_worker_crew_advances_only_on_common_availability():
    machines = {"1_1": {1: [1001, 1002]}}
    individual = ["1_1", 1, 2]
    _base, kwargs = eval_kwargs(
        work=1200.0, machines=machines, index=operator_index(mixed=True)
    )
    # MC=2 means 600 productive minutes with two assigned workers.
    fitness, details = evaluate_schedule(
        individual, return_details=True, **kwargs
    )
    assert fitness[0] < float("inf")
    row = details["phenotype_operations"][0]
    expected = (
        (datetime(2026, 3, 2, 8, 0), datetime(2026, 3, 2, 14, 0)),
        (datetime(2026, 3, 3, 8, 0), datetime(2026, 3, 3, 12, 0)),
    )
    assert set(row["employee_ids"]) == {"101", "102"}
    assert row["worker_segments_by_employee"]["101"] == expected
    assert row["worker_segments_by_employee"]["102"] == expected
    assert row["end_minutes"] == 1800.0


def test_score_and_export_share_one_phenotype_hash_and_timing(tmp_path, monkeypatch):
    individual, kwargs = eval_kwargs(num_ops=2, work=600.0, deadline=1500.0)
    fitness, details = evaluate_schedule(
        individual, return_details=True, **kwargs
    )
    rows = details["phenotype_operations"]
    assert rows[0]["end_minutes"] == 1560.0
    assert rows[1]["start_minutes"] == 1560.0
    assert rows[1]["end_minutes"] == 1620.0
    assert details["order_completion_minutes"]["1"] == 1620.0
    assert details["order_lateness_hours"]["1"] == 2.0

    monkeypatch.setattr(decision_module, "OUTPUT_DIR", str(tmp_path))
    routings = pd.DataFrame([
        {
            "production_order_id": 1,
            "operation_id": i + 1,
            "operation_sequence_number": i + 1,
            "required_machine_type": "single-needle lockstitch",
        }
        for i in range(2)
    ])
    summary = AIDecisionEngine()._export_proposed_schedule(
        individual, 2, kwargs["op_details"], {}, 
        pd.DataFrame({"line_id": [1], "line_number": ["L1"], "is_active": [1]}),
        {"1_1": [1], "1_2": [1]},
        {"1": datetime(2026, 3, 3, 7, 0)},
        routings,
        pd.DataFrame({"asset_id": [1001], "line_id": [1]}),
        label="parity",
        machine_pools=kwargs["machine_pools"],
        operator_index=kwargs["operator_index"],
        present_operator_count=10,
        usable_machine_count=1,
        op_optype=kwargs["op_optype"],
        base_time=ORIGIN,
    )
    exported = json.loads(
        __import__("pathlib").Path(summary["schedule_file"]).read_text()
    )
    assert summary["phenotype_hash"] == details["phenotype_hash"]
    assert exported[0]["cushioned_end_time"] == "2026-03-03T08:00:00"
    assert exported[1]["proposed_start_time"] == "2026-03-03T08:00:00"
    assert exported[1]["cushioned_end_time"] == "2026-03-03T09:00:00"
    assert summary["fitness"] == pytest.approx(fitness[0])


def test_calendar_aware_line_choice_changes_candidate_ranking():
    machines = {
        "1_1": {1: [1001], 2: [2001]},
    }
    index = operator_index(two_lines=True)
    slow, kwargs = eval_kwargs(
        work=600.0, machines=machines, lines=[1], index=index, deadline=720.0
    )
    fast = ["1_1", 2, 1]
    slow_fitness, slow_details = evaluate_schedule(
        slow, return_details=True, **kwargs
    )
    fast_fitness, fast_details = evaluate_schedule(
        fast, return_details=True, **kwargs
    )
    assert slow_details["phenotype_operations"][0]["end_minutes"] == 1560.0
    assert fast_details["phenotype_operations"][0]["end_minutes"] == 600.0
    assert fast_fitness[0] < slow_fitness[0]
