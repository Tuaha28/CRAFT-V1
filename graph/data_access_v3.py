"""Read-only Phase-A data access for the leak-free GNN v3 experiment."""

from __future__ import annotations

import json

import pandas as pd

from database.db import get_engine
from utils.logger import get_logger


logger = get_logger("gnn_v3_data", "gnn_v3_leakfree.log")


def fetch_all_orders_and_routings_v3() -> pd.DataFrame:
    """All order/routing rows; deliberately no final-status filter."""
    query = """
        SELECT
            po.production_order_id,
            po.target_quantity,
            pu.delivery_deadline AS order_deadline,
            po.scheduled_start_date AS release_date,
            po.scheduled_end_date AS internal_plan_date,
            ro.operation_id,
            ro.operation_sequence_number,
            ro.operation_name,
            ro.standard_allowed_minutes AS sam,
            ro.required_machine_type
        FROM Production_Orders po
        LEFT JOIN Purchase_Orders pu ON pu.po_id = po.po_id
        JOIN Routing_Operations ro ON ro.product_id = po.product_id
        ORDER BY po.production_order_id, ro.operation_sequence_number
    """
    return pd.read_sql(query, get_engine())


def fetch_frozen_plan_v3() -> pd.DataFrame:
    """Phase-A plan fields only; no status, dispatch context, or actual time."""
    query = """
        SELECT
            production_order_id,
            operation_id,
            line_id,
            asset_id,
            employee_id,
            planned_start_time,
            planned_end_time
        FROM Job_Shop_Schedules
        ORDER BY production_order_id, operation_id, lot_index
    """
    return pd.read_sql(query, get_engine())


def fetch_frozen_assignments_v3() -> pd.DataFrame:
    """Distinct machine/worker relations frozen by Phase A for every order."""
    query = """
        SELECT DISTINCT
            production_order_id,
            operation_id,
            asset_id,
            employee_id
        FROM Job_Shop_Schedules
        ORDER BY production_order_id, operation_id, asset_id, employee_id
    """
    return pd.read_sql(query, get_engine())


def fetch_label_timing_v3(order_ids) -> pd.DataFrame:
    """Explicit split-scoped label access; never falls back to all orders."""
    if order_ids is None:
        raise ValueError("Explicit non-empty order_ids are required")
    from graph.label_access_v3 import fetch_label_timing_for_orders_v3
    return fetch_label_timing_for_orders_v3(order_ids)


def fetch_dataset_identity_v3() -> dict:
    """Return the promoted dataset calibration identity for the manifest."""
    rows = pd.read_sql(
        "SELECT payload FROM Dataset_Calibration WHERE singleton_id = 1",
        get_engine(),
    )
    if len(rows) != 1:
        raise RuntimeError("Expected exactly one Dataset_Calibration row")
    payload = json.loads(rows.iloc[0]["payload"])
    required = {"dataset_version", "reference_time"}
    missing = required.difference(payload)
    if missing:
        raise RuntimeError(
            f"Dataset calibration missing identity fields: {sorted(missing)}"
        )
    return payload


def fetch_machine_state_v3() -> pd.DataFrame:
    """Static machine identity/capability only.

    Machinery_Assets.current_status is deliberately excluded: the current
    STRESS seeder synchronizes that field to final Phase-B machine state.
    """
    query = """
        SELECT
            ma.asset_id,
            ma.factory_id,
            ma.line_id,
            ma.machine_model,
            mc.operation_type,
            mc.max_rpm
        FROM Machinery_Assets ma
        LEFT JOIN Machine_Capabilities mc ON ma.asset_id = mc.asset_id
        ORDER BY ma.asset_id
    """
    return pd.read_sql(query, get_engine())


def fetch_worker_state_v3() -> pd.DataFrame:
    """Phase-A-known workforce calendar and static skill state."""
    query = """
        SELECT
            e.employee_id AS worker_id,
            e.role,
            CASE WHEN ws.is_on_leave = TRUE THEN 1 ELSE 0 END
                AS is_on_leave_at_prediction,
            GROUP_CONCAT(DISTINCT osm.operation_type SEPARATOR ',')
                AS skill_type,
            AVG(osm.efficiency_rating) AS efficiency_score,
            COUNT(DISTINCT osm.operation_type) AS skill_breadth
        FROM Employees e
        JOIN Work_Schedules ws ON e.employee_id = ws.employee_id
        LEFT JOIN Operator_Skill_Matrix osm
            ON e.employee_id = osm.employee_id
        WHERE e.is_active = TRUE
          AND ws.shift_date = (SELECT MAX(shift_date) FROM Work_Schedules)
        GROUP BY e.employee_id, e.role, ws.is_on_leave
        ORDER BY e.employee_id
    """
    return pd.read_sql(query, get_engine())


