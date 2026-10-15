"""Resource-free production must never be scored as a feasible schedule."""
import math
from datetime import datetime

import pandas as pd
import pytest

from ga.fitness import evaluate_schedule
from ga.infeasibility import ScheduleInfeasibleError
from ga.scheduler import GAScheduler


def factory_frames(capability="seam edge overlock", active=1):
    routings = pd.DataFrame([{
        "production_order_id": 101, "operation_id": 1,
        "operation_sequence_number": 1, "sam": 2.0,
        "target_quantity": 40, "remaining_frac": 1.0,
        "required_machine_type": "single-needle lockstitch",
    }])
    machines = pd.DataFrame([
        {"asset_id": i, "line_id": 1, "operation_type": capability}
        for i in range(1, 5)
    ])
    lines = pd.DataFrame([{
        "line_id": 1, "is_active": active, "target_efficiency_percentage": 85.0,
    }])
    return routings, machines, lines


@pytest.mark.parametrize("capability,active", [
    ("seam edge overlock", 1),  # old code fabricated line 1 and an empty pool
    ("body assembly", 0),     # no active production line
])
def test_impossible_instance_fails_before_ga_search(capability, active):
    routings, machines, lines = factory_frames(capability, active)
    with pytest.raises(ScheduleInfeasibleError) as caught:
        GAScheduler(routings, machines, lines, {}, current_time=datetime(2026, 1, 1), legacy_mode='legacy_company_wide')
    assert caught.value.result == {
        "code": "missing_machine_capacity",
        "production_order_id": "101",
        "operation_key": "101_1",
        "required_capability": "body assembly",
        "line_id": None,
    }
    assert "101" in str(caught.value)
    assert "body assembly" in str(caught.value)


def test_eligible_instance_still_optimizes_with_finite_fitness():
    routings, machines, lines = factory_frames("body assembly")
    scheduler = GAScheduler(routings, machines, lines, {}, current_time=datetime(2026, 1, 1), legacy_mode='legacy_company_wide')
    solutions, _ = scheduler.optimize(pop_size=6, generations=1, n_solutions=1)
    assert solutions
    assert math.isfinite(solutions[0][1])
    assert scheduler.factory.machine_ids_per_line_map["101_1"][1]


@pytest.mark.parametrize("pools", [None, {}, {"101_1": {1: []}}, {"101_1": {2: [10]}}])
def test_candidate_with_positive_work_and_no_pool_is_rejected(pools):
    args = (["101_1", 1, 1], 1, {
        "101_1": {"setup_mins": 0.0, "serial_processing_mins": 60.0,
                  "required_capability": "body assembly"},
    }, {})
    seq = {"101_1": 1}
    fitness, details = evaluate_schedule(
        *args, machine_pools=pools, return_details=True, op_sequence_map=seq)
    assert fitness == (float("inf"),)
    assert details["infeasibility"] == {
        "code": "missing_machine_capacity", "production_order_id": "101",
        "operation_key": "101_1", "required_capability": "body assembly", "line_id": 1,
    }
    assert evaluate_schedule(*args, machine_pools=pools, op_sequence_map=seq) == (float("inf"),)


def test_zero_work_without_a_pool_is_not_rejected_by_positive_work_guard():
    fitness, details = evaluate_schedule(
        ["101_1", 1, 1], 1,
        {"101_1": {"setup_mins": 0.0, "serial_processing_mins": 0.0}}, {},
        machine_pools={}, return_details=True, op_sequence_map={"101_1": 1},
    )
    assert math.isfinite(fitness[0])
    assert "infeasibility" not in details
