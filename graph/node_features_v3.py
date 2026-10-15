"""Frozen-Phase-A operation features for the leak-free GNN v3 package."""

from __future__ import annotations

import numpy as np
import pandas as pd
import scipy.sparse as sp
import torch

from graph.feature_schema_v3 import operation_schema_v3
from utils.config import GA_LOTS_PER_OPERATION
from utils.machine_taxonomy import (
    machine_type_to_operation_type,
    operation_type_index,
)


FORBIDDEN_INPUT_COLUMNS = frozenset({
    "actual_start_time",
    "actual_end_time",
    "execution_status",
    "queue_delay_mins_at_dispatch",
    "machine_breakdown_count_at_dispatch",
    "machine_status_at_dispatch",
    "employee_on_leave_at_dispatch",
})


def _assert_phase_a_only(frame: pd.DataFrame, name: str) -> None:
    leaked = FORBIDDEN_INPUT_COLUMNS.intersection(frame.columns)
    if leaked:
        raise ValueError(
            f"{name} contains forbidden Phase-B fields: {sorted(leaked)}"
        )


def compute_pool_occupancy_v3(
    df_assignments: pd.DataFrame,
    operation_type_by_key: dict,
) -> dict:
    """Count same-type operations sharing a frozen Phase-A machine.

    This pure helper performs no query. Its only input is the explicit
    Phase-A assignment frame validated by build_operation_node_features_v3.
    """
    _assert_phase_a_only(df_assignments, "frozen assignments")
    if df_assignments.empty:
        return {}

    frame = df_assignments[
        ["production_order_id", "operation_id", "asset_id"]
    ].drop_duplicates().copy()
    frame["unique_op_key"] = (
        frame["production_order_id"].astype(str)
        + "_"
        + frame["operation_id"].astype(str)
    )
    operation_keys = sorted(frame["unique_op_key"].unique())
    asset_ids = sorted(frame["asset_id"].dropna().unique())
    operation_index = {
        key: index for index, key in enumerate(operation_keys)
    }
    asset_index = {
        asset: index for index, asset in enumerate(asset_ids)
    }
    rows = frame["unique_op_key"].map(operation_index).to_numpy()
    columns = frame["asset_id"].map(asset_index).to_numpy()
    if pd.isna(columns).any():
        raise ValueError("Frozen assignment contains a missing machine identity")
    incidence = sp.csr_matrix(
        (np.ones(len(rows), dtype=np.int8), (rows, columns.astype(int))),
        shape=(len(operation_keys), len(asset_ids)),
    )
    shared = (incidence @ incidence.T).tocsr()
    types = np.array(
        [operation_type_by_key.get(key) for key in operation_keys],
        dtype=object,
    )
    occupancy = {}
    for operation_type in pd.unique(types[types != None]):
        indices = np.where(types == operation_type)[0]
        same_type_shared = shared[indices][:, indices]
        counts = np.asarray(
            (same_type_shared > 0).sum(axis=1)
        ).ravel() - 1
        for local_index, global_index in enumerate(indices):
            occupancy[operation_keys[global_index]] = int(counts[local_index])
    return occupancy


def build_operation_node_features_v3(
    df_routings: pd.DataFrame,
    df_plan: pd.DataFrame,
    df_assignments: pd.DataFrame,
) -> tuple:
    """Build exactly seven schema-v3 features from frozen Phase-A state."""
    for frame, name in (
        (df_routings, "routings"),
        (df_plan, "frozen plan"),
        (df_assignments, "frozen assignments"),
    ):
        if frame is None:
            raise ValueError(f"{name} is required")
        _assert_phase_a_only(frame, name)

    required_routing = {
        "production_order_id", "operation_id", "target_quantity", "sam",
        "order_deadline", "release_date", "operation_sequence_number",
        "required_machine_type",
    }
    required_plan = {
        "production_order_id", "operation_id",
        "planned_start_time", "planned_end_time",
    }
    required_assignments = {
        "production_order_id", "operation_id", "asset_id", "employee_id",
    }
    for frame, required, name in (
        (df_routings, required_routing, "routings"),
        (df_plan, required_plan, "frozen plan"),
        (df_assignments, required_assignments, "frozen assignments"),
    ):
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{name} missing fields: {sorted(missing)}")

    plan_aggregate = (
        df_plan.groupby(
            ["production_order_id", "operation_id"], as_index=False
        )
        .agg(
            planned_start_time=("planned_start_time", "min"),
            planned_end_time=("planned_end_time", "max"),
        )
    )
    merged = pd.merge(
        df_routings.copy(),
        plan_aggregate,
        on=["production_order_id", "operation_id"],
        how="left",
        validate="one_to_one",
    )
    merged["unique_op_key"] = (
        merged["production_order_id"].astype(str)
        + "_"
        + merged["operation_id"].astype(str)
    )
    assignment_keys = (
        df_assignments["production_order_id"].astype(str)
        + "_"
        + df_assignments["operation_id"].astype(str)
    )
    missing_assignments = set(merged["unique_op_key"]) - set(assignment_keys)
    if missing_assignments:
        raise ValueError(
            "Frozen assignments missing for "
            f"{len(missing_assignments)} operation(s)"
        )
    if merged[
        ["planned_start_time", "planned_end_time"]
    ].isna().any().any():
        raise ValueError("A complete frozen Phase-A span is required")

    parallelism = (
        df_assignments.groupby(
            ["production_order_id", "operation_id"]
        )["asset_id"]
        .nunique()
        .rename("planned_machine_count")
        .reset_index()
    )
    merged = pd.merge(
        merged,
        parallelism,
        on=["production_order_id", "operation_id"],
        how="left",
        validate="one_to_one",
    )

    sam_norm = (
        pd.to_numeric(merged["sam"], errors="coerce") / 10.0
    ).fillna(0.0)
    qty_norm = (
        pd.to_numeric(merged["target_quantity"], errors="coerce") / 15000.0
    ).fillna(0.0)
    machine_parallelism_norm = (
        merged["planned_machine_count"] / float(GA_LOTS_PER_OPERATION)
    ).fillna(0.0).clip(upper=1.0)
    deadline_budget_days = (
        pd.to_datetime(merged["order_deadline"])
        - pd.to_datetime(merged["release_date"])
    ).dt.total_seconds() / 86400.0
    deadline_budget_norm = (
        deadline_budget_days / 60.0
    ).fillna(0.0).clip(lower=0.0, upper=3.0)

    routing_length = (
        merged.groupby("production_order_id")["operation_sequence_number"]
        .transform("count")
        .astype(float)
    )
    routing_step_norm = (
        merged["operation_sequence_number"] / routing_length
    ).fillna(0.0)
    steps_remaining_norm = (
        (routing_length - merged["operation_sequence_number"])
        / routing_length
    ).fillna(0.0)

    operation_type_by_key = {
        key: machine_type_to_operation_type(machine_type)
        for key, machine_type in zip(
            merged["unique_op_key"], merged["required_machine_type"]
        )
    }
    occupancy = compute_pool_occupancy_v3(
        df_assignments, operation_type_by_key
    )
    occupancy_raw = merged["unique_op_key"].map(occupancy)
    if occupancy_raw.isna().any():
        raise ValueError("Pool occupancy is missing for an operation")
    pool_occupancy_norm = (
        occupancy_raw / 1000.0
    ).clip(upper=3.0)

    features = np.column_stack([
        sam_norm,
        qty_norm,
        machine_parallelism_norm,
        deadline_budget_norm,
        routing_step_norm,
        steps_remaining_norm,
        pool_occupancy_norm,
    ]).astype(np.float32)
    schema = operation_schema_v3()
    if features.shape[1] != schema["count"]:
        raise AssertionError("Feature width differs from schema v3")
    if not np.isfinite(features).all():
        raise ValueError("Non-finite operation feature")

    mapping = {
        key: index
        for index, key in enumerate(merged["unique_op_key"].tolist())
    }
    type_index = torch.tensor(
        [
            operation_type_index(
                machine_type_to_operation_type(machine_type)
            )
            for machine_type in merged["required_machine_type"]
        ],
        dtype=torch.long,
    )
    return torch.tensor(features), mapping, merged, type_index
def build_machine_node_features_v3(df_machines: pd.DataFrame) -> tuple:
    """Build machine nodes from static capability, never final status."""
    _assert_phase_a_only(df_machines, "machine state")
    if "current_status" in df_machines.columns:
        raise ValueError(
            "machine state contains post-execution current_status"
        )
    required = {"asset_id", "max_rpm"}
    missing = required.difference(df_machines.columns)
    if missing:
        raise ValueError(f"machine state missing fields: {sorted(missing)}")
    rpm = (
        pd.to_numeric(df_machines["max_rpm"], errors="coerce")
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
        .reshape(-1, 1)
        / 5000.0
    )
    mapping = {
        asset_id: index
        for index, asset_id in enumerate(df_machines["asset_id"])
    }
    return torch.tensor(rpm, dtype=torch.float), mapping


def build_worker_node_features_v3(df_workers: pd.DataFrame) -> tuple:
    """Build worker nodes from Phase-A calendar and static skill state."""
    _assert_phase_a_only(df_workers, "worker state")
    required = {
        "worker_id", "is_on_leave_at_prediction",
        "efficiency_score", "skill_breadth",
    }
    missing = required.difference(df_workers.columns)
    if missing:
        raise ValueError(f"worker state missing fields: {sorted(missing)}")
    absent = (
        pd.to_numeric(
            df_workers["is_on_leave_at_prediction"], errors="raise"
        ).astype(bool).to_numpy(dtype=np.float32)
    )
    present = 1.0 - absent
    efficiency = (
        pd.to_numeric(df_workers["efficiency_score"], errors="coerce")
        / 100.0
    )
    observed = efficiency.dropna()
    fallback = float(observed.mean()) if not observed.empty else 0.84
    efficiency = efficiency.fillna(fallback).to_numpy(dtype=np.float32)
    skill = (
        pd.to_numeric(df_workers["skill_breadth"], errors="coerce")
        .fillna(1.0)
        .clip(lower=0.0, upper=5.0)
        .to_numpy(dtype=np.float32)
        / 5.0
    )
    features = np.column_stack([present, absent, efficiency, skill])
    mapping = {
        worker_id: index
        for index, worker_id in enumerate(df_workers["worker_id"])
    }
    return torch.tensor(features, dtype=torch.float), mapping


