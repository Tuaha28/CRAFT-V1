from datetime import datetime, time
import random

from data_generation.seed_database_fixed import (
    build_frozen_planned_schedule,
    simulate_actual_from_frozen_plan,
)


def _tiny_problem():
    base = datetime(2026, 9, 1, 0, 0)
    return dict(
        order_ids=[1, 2],
        prod_order_details={
            1: {"product_id": 10, "target_qty": 40, "start_date": base.date()},
            2: {"product_id": 10, "target_qty": 20, "start_date": base.date()},
        },
        product_routing_map={
            10: [
                {"op_id": 101, "sam": 10.0, "machine_type": "Sewing",
                 "operation_type": "Stitch"},
                {"op_id": 102, "sam": 4.0, "machine_type": "Finish",
                 "operation_type": "Finish"},
            ]
        },
        base_date=base.date(),
        base_time=base,
        factory_capacity={1: 10},
        capable_machines_by_type={"Sewing": [1, 2], "Finish": [3]},
        asset_to_line_map={1: 11, 2: 11, 3: 12},
        line_to_factory_map={11: 1, 12: 1},
        qualified_operators_by_op_factory={
            ("Stitch", 1): [1, 2],
            ("Finish", 1): [3],
        },
        employee_leave_lookup={1: False, 2: False, 3: False},
        employee_batch_lookup={1: None, 2: None, 3: None},
        line_to_batches={11: [], 12: []},
        batch_offsets={},
        employee_shift_lookup={
            1: ("day", time(6), time(14)),
            2: ("evening", time(14), time(22)),
            3: ("night", time(22), time(6)),
        },
        employee_skill_lookup={
            1: {"Stitch": 80.0},
            2: {"Stitch": 90.0},
            3: {"Finish": 85.0},
        },
        order_deadlines={
            1: datetime(2026, 9, 20, 23, 59, 59),
            2: datetime(2026, 9, 20, 23, 59, 59),
        },
        duration_calibration_factor=0.825454,
        planning_rng=random.Random(42),
        target_lot_size=20,
        max_lots_per_order=60,
    )


def _plan_projection(rows):
    return [
        (
            row.production_order_id,
            row.lot_index,
            row.operation_index,
            row.operation_id,
            row.factory_id,
            row.line_id,
            row.asset_id,
            row.employee_id,
            row.planned_start_time.isoformat(),
            row.planned_end_time.isoformat(),
            tuple((a.isoformat(), b.isoformat()) for a, b in row.planned_work_segments),
        )
        for row in rows
    ]


def _actual_projection(rows):
    return [
        (
            row.actual_start_time.isoformat(),
            row.actual_end_time.isoformat(),
            row.machine_breakdown_count_at_dispatch,
        )
        for row in rows
    ]


def test_actual_seed_cannot_change_frozen_plan():
    inputs = _tiny_problem()
    plan_a, ownership_a, stats_a = build_frozen_planned_schedule(**inputs)

    inputs["planning_rng"] = random.Random(42)
    plan_b, ownership_b, stats_b = build_frozen_planned_schedule(**inputs)

    actual_a = list(simulate_actual_from_frozen_plan(
        plan_a, base_time=inputs["base_time"],
        now_time=datetime(2026, 9, 10), actual_seed=1,
        asset_status_lookup={1: "Active", 2: "Active", 3: "Active"},
    ))
    actual_b = list(simulate_actual_from_frozen_plan(
        plan_b, base_time=inputs["base_time"],
        now_time=datetime(2026, 9, 10), actual_seed=999,
        asset_status_lookup={1: "Active", 2: "Active", 3: "Active"},
    ))

    assert ownership_a == ownership_b
    assert stats_a == stats_b
    assert _plan_projection(plan_a) == _plan_projection(plan_b)
    assert _plan_projection([row.plan for row in actual_a]) == _plan_projection(
        [row.plan for row in actual_b]
    )
    assert _actual_projection(actual_a) != _actual_projection(actual_b)
