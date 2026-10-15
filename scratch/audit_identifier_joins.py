"""Read-only identifier/join audit against the live production database."""

import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings,
    fetch_assignable_operators,
    fetch_machine_state,
    fetch_operation_assignments,
    fetch_worker_state,
)
from utils.factory_ownership import resolve_ownership
from utils.machine_taxonomy import machine_type_to_operation_type


ACTIVE = ("Pending", "Processing", "Delayed")


def duplicate_count(frame, columns):
    return int(frame.duplicated(columns, keep=False).sum())


def main():
    engine = get_engine()
    routes = fetch_active_orders_and_routings()
    machines = fetch_machine_state()
    workers = fetch_worker_state()
    assignable = fetch_assignable_operators()
    assignments = fetch_operation_assignments()
    order_ids = routes["production_order_id"].astype(str).unique().tolist()
    ownership = resolve_ownership(order_ids)
    owned = ownership.for_orders(order_ids)

    routes = routes.copy()
    routes["order_key"] = routes["production_order_id"].astype(str)
    routes["operation_key"] = (
        routes["production_order_id"].astype(str) + "_" +
        routes["operation_id"].astype(str)
    )
    routes["operation_type"] = routes["required_machine_type"].map(
        machine_type_to_operation_type
    )
    machines = machines.copy()
    machines["operation_type_norm"] = (
        machines["operation_type"].astype(str).str.strip().str.lower()
    )
    machine_factory_type = {
        (int(r.factory_id), r.operation_type_norm)
        for r in machines.dropna(subset=["factory_id", "operation_type"]).itertuples(index=False)
    }
    infeasible = [
        key for key, order, optype in zip(
            routes["operation_key"], routes["order_key"], routes["operation_type"]
        )
        if optype is None or (int(owned[order]), optype) not in machine_factory_type
    ]

    with engine.connect() as conn:
        db_counts = {
            name: int(pd.read_sql(text(f"SELECT COUNT(*) n FROM {name}"), conn).iloc[0].n)
            for name in (
                "Production_Orders", "Routing_Operations", "Job_Shop_Schedules",
                "Machinery_Assets", "Employees", "Order_Factory_Ownership",
                "Machine_Capabilities", "Operator_Skill_Matrix"
            )
        }
        expected = pd.read_sql(text("""
            SELECT po.production_order_id, ro.operation_id,
                   ro.operation_sequence_number
            FROM Production_Orders po
            JOIN Routing_Operations ro ON ro.product_id=po.product_id
            WHERE po.status IN ('Pending','Processing','Delayed')
        """), conn)
        scheduled_pairs = pd.read_sql(text("""
            SELECT production_order_id, operation_id, COUNT(*) AS lot_rows,
                   COUNT(DISTINCT lot_index) AS distinct_lot_indices
            FROM Job_Shop_Schedules
            GROUP BY production_order_id, operation_id
        """), conn)
        schedule = pd.read_sql(text("""
            SELECT j.schedule_item_id, j.production_order_id, j.operation_id,
                   j.lot_index, j.line_id, j.asset_id, j.employee_id,
                   ma.line_id AS asset_line_id, ma.factory_id AS asset_factory_id,
                   pl.department_id, d.factory_id AS line_factory_id,
                   ofo.factory_id AS owned_factory_id, ro.required_machine_type
            FROM Job_Shop_Schedules j
            JOIN Machinery_Assets ma ON ma.asset_id=j.asset_id
            JOIN Production_Lines pl ON pl.line_id=j.line_id
            JOIN Departments d ON d.department_id=pl.department_id
            JOIN Order_Factory_Ownership ofo
              ON ofo.production_order_id=CAST(j.production_order_id AS CHAR)
            JOIN Routing_Operations ro ON ro.operation_id=j.operation_id
        """), conn)
        capabilities = pd.read_sql(text(
            "SELECT asset_id, operation_type FROM Machine_Capabilities"
        ), conn)
        skills = pd.read_sql(text(
            "SELECT employee_id, operation_type FROM Operator_Skill_Matrix"
        ), conn)

    expected_keys = set(
        expected["production_order_id"].astype(str) + "_" +
        expected["operation_id"].astype(str)
    )
    route_keys = set(routes["operation_key"])
    schedule_keys = set(
        scheduled_pairs["production_order_id"].astype(str) + "_" +
        scheduled_pairs["operation_id"].astype(str)
    )
    cap_keys = {
        (str(r.asset_id), str(r.operation_type).strip().lower())
        for r in capabilities.itertuples(index=False)
    }
    skill_keys = {
        (str(r.employee_id), str(r.operation_type).strip().lower())
        for r in skills.itertuples(index=False)
    }
    schedule["operation_type"] = schedule["required_machine_type"].map(
        machine_type_to_operation_type
    )
    incompatible_machine = schedule.apply(
        lambda r: (str(r.asset_id), r.operation_type) not in cap_keys, axis=1
    )
    unqualified_worker = schedule.apply(
        lambda r: (
            (str(r.employee_id), r.operation_type) not in skill_keys
            if pd.notna(r.employee_id) and r.operation_type is not None
            else True
        ),
        axis=1,
    )

    output = {
        "measured_populations": {
            "active_statuses": list(ACTIVE),
            "database_counts": db_counts,
            "active_orders": int(routes["production_order_id"].nunique()),
            "active_route_rows": len(routes),
            "expected_active_route_rows_from_product_join": len(expected),
            "scheduled_order_operation_pairs_all_statuses": len(scheduled_pairs),
        },
        "routing_join": {
            "duplicate_order_operation_rows": duplicate_count(
                routes, ["production_order_id", "operation_id"]
            ),
            "duplicate_order_sequence_rows": duplicate_count(
                routes, ["production_order_id", "operation_sequence_number"]
            ),
            "expected_missing_from_production_query": len(expected_keys - route_keys),
            "unexpected_in_production_query": len(route_keys - expected_keys),
            "active_expected_operations_missing_all_schedule_lots":
                len(expected_keys - schedule_keys),
            "scheduled_pairs_with_duplicate_lot_index": int(
                (scheduled_pairs["lot_rows"] != scheduled_pairs["distinct_lot_indices"]).sum()
            ),
        },
        "ownership_and_feasibility": {
            "ownership_rows_loaded": len(ownership.entries),
            "active_orders_requested": len(order_ids),
            "active_orders_unowned": len(set(order_ids) - set(owned)),
            "active_operation_rows_without_owned_factory_capability": len(infeasible),
            "examples": infeasible[:10],
        },
        "machine_identifiers": {
            "machine_query_rows": len(machines),
            "unique_asset_ids": int(machines["asset_id"].nunique()),
            "duplicate_asset_capability_pairs": duplicate_count(
                machines, ["asset_id", "operation_type_norm"]
            ),
            "schedule_asset_line_mismatches": int(
                (schedule["line_id"] != schedule["asset_line_id"]).sum()
            ),
            "schedule_asset_factory_mismatches": int(
                (schedule["asset_factory_id"] != schedule["owned_factory_id"]).sum()
            ),
            "schedule_line_factory_mismatches": int(
                (schedule["line_factory_id"] != schedule["owned_factory_id"]).sum()
            ),
            "schedule_machine_capability_mismatches": int(incompatible_machine.sum()),
        },
        "worker_identifiers": {
            "worker_query_rows": len(workers),
            "unique_worker_ids": int(workers["worker_id"].nunique()),
            "duplicate_worker_rows": duplicate_count(workers, ["worker_id"]),
            "assignable_rows": len(assignable),
            "duplicate_assignable_employee_skill_rows": duplicate_count(
                assignable, ["employee_id", "operation_type"]
            ),
            "schedule_worker_skill_mismatches_or_null": int(unqualified_worker.sum()),
        },
        "graph_assignment_input": {
            "assignment_rows": len(assignments),
            "duplicate_order_operation_asset_worker_rows": duplicate_count(
                assignments,
                ["production_order_id", "operation_id", "asset_id", "employee_id"],
            ),
            "null_asset_rows": int(assignments["asset_id"].isna().sum()),
            "null_employee_rows": int(assignments["employee_id"].isna().sum()),
        },
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
