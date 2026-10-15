"""Durable two-phase DGP proof: actual randomness cannot change planning."""
import hashlib
import json
import random
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_generation.seed_database_fixed import (
    build_frozen_planned_schedule,
    simulate_actual_from_frozen_plan,
)

OUT = Path("outputs/integration_audit/seed_plan_actual_invariance_proof.json")


def inputs():
    base = datetime(2026, 9, 1)
    return dict(
        order_ids=[1, 2],
        prod_order_details={
            1: {"product_id": 10, "target_qty": 40, "start_date": base.date()},
            2: {"product_id": 10, "target_qty": 20, "start_date": base.date()},
        },
        product_routing_map={10: [
            {"op_id": 101, "sam": 10.0, "machine_type": "Sewing",
             "operation_type": "Stitch"},
            {"op_id": 102, "sam": 4.0, "machine_type": "Finish",
             "operation_type": "Finish"},
        ]},
        base_date=base.date(), base_time=base,
        factory_capacity={1: 10},
        capable_machines_by_type={"Sewing": [1, 2], "Finish": [3]},
        asset_to_line_map={1: 11, 2: 11, 3: 12},
        line_to_factory_map={11: 1, 12: 1},
        qualified_operators_by_op_factory={
            ("Stitch", 1): [1, 2], ("Finish", 1): [3],
        },
        employee_leave_lookup={1: False, 2: False, 3: False},
        employee_batch_lookup={1: None, 2: None, 3: None},
        line_to_batches={11: [], 12: []}, batch_offsets={},
        employee_shift_lookup={
            1: ("day", time(6), time(14)),
            2: ("evening", time(14), time(22)),
            3: ("night", time(22), time(6)),
        },
        employee_skill_lookup={
            1: {"Stitch": 80.0}, 2: {"Stitch": 90.0},
            3: {"Finish": 85.0},
        },
        order_deadlines={
            1: datetime(2026, 9, 20, 23, 59, 59),
            2: datetime(2026, 9, 20, 23, 59, 59),
        },
        duration_calibration_factor=0.825454,
        target_lot_size=20, max_lots_per_order=60,
    )


def plan_projection(rows):
    return [{
        "order": row.production_order_id,
        "lot": row.lot_index,
        "routing_sequence": row.operation_index + 1,
        "operation": row.operation_id,
        "factory": row.factory_id,
        "line": row.line_id,
        "machine": row.asset_id,
        "employee": row.employee_id,
        "planned_start": row.planned_start_time.isoformat(),
        "planned_end": row.planned_end_time.isoformat(),
        "planned_segments": [
            [start.isoformat(), end.isoformat()]
            for start, end in row.planned_work_segments
        ],
    } for row in rows]


def actual_projection(rows):
    return [{
        "actual_start": row.actual_start_time.isoformat(),
        "actual_end": row.actual_end_time.isoformat(),
        "breakdowns": row.machine_breakdown_count_at_dispatch,
    } for row in rows]


def digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main():
    base_inputs = inputs()
    plans, actuals, ownerships, stats = [], [], [], []
    for actual_seed in (1, 999):
        planned, ownership, stat = build_frozen_planned_schedule(
            **base_inputs, planning_rng=random.Random(42)
        )
        realized = list(simulate_actual_from_frozen_plan(
            planned, base_time=base_inputs["base_time"],
            now_time=datetime(2026, 9, 10),
            actual_seed=actual_seed,
            asset_status_lookup={1: "Active", 2: "Active", 3: "Active"},
        ))
        plans.append(plan_projection(planned))
        actuals.append(actual_projection(realized))
        ownerships.append(ownership)
        stats.append(stat)

    plan_hashes = [digest(value) for value in plans]
    actual_hashes = [digest(value) for value in actuals]
    fields = [
        "order", "lot", "routing_sequence", "operation", "factory", "line",
        "machine", "employee", "planned_start", "planned_end",
        "planned_segments",
    ]
    field_equal = {
        field: [row[field] for row in plans[0]]
        == [row[field] for row in plans[1]]
        for field in fields
    }
    result = {
        "production_functions": [
            "build_frozen_planned_schedule",
            "simulate_actual_from_frozen_plan",
        ],
        "planning_seed": 42,
        "actual_seeds": [1, 999],
        "planned_rows": len(plans[0]),
        "plan_hashes": plan_hashes,
        "plan_hash_equal": plan_hashes[0] == plan_hashes[1],
        "field_equal": field_equal,
        "ownership_equal": ownerships[0] == ownerships[1],
        "planning_stats_equal": stats[0] == stats[1],
        "actual_hashes": actual_hashes,
        "actual_hash_different": actual_hashes[0] != actual_hashes[1],
        "scope": (
            "Deterministic small production-function staging proof; it does "
            "not exercise the full MySQL insertion/validation transaction."
        ),
    }
    assert result["plan_hash_equal"]
    assert all(field_equal.values())
    assert result["ownership_equal"] and result["planning_stats_equal"]
    assert result["actual_hash_different"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
