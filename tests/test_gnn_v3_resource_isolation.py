"""Resource-state isolation gates for leak-free GNN v3."""

import pandas as pd
import pytest

from graph import data_access_v3
from graph.node_features_v3 import (
    build_machine_node_features_v3,
    build_worker_node_features_v3,
)


def test_v3_resource_queries_exclude_phase_b_state(monkeypatch):
    observed = []

    def fake_read_sql(query, engine):
        observed.append(" ".join(str(query).lower().split()))
        return pd.DataFrame()

    monkeypatch.setattr(data_access_v3.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(data_access_v3, "get_engine", lambda: object())
    data_access_v3.fetch_machine_state_v3()
    data_access_v3.fetch_worker_state_v3()

    assert "current_status" not in observed[0]
    forbidden = {
        "actual_start_time",
        "actual_end_time",
        "queue_delay_mins_at_dispatch",
        "machine_breakdown_count_at_dispatch",
        "machine_status_at_dispatch",
        "employee_on_leave_at_dispatch",
        "execution_status",
    }
    assert all(
        not any(value in query for value in forbidden)
        for query in observed
    )


def test_v3_machine_features_reject_post_execution_status():
    machines = pd.DataFrame({"asset_id": [1], "max_rpm": [4500]})
    machine_x, machine_map = build_machine_node_features_v3(machines)
    assert tuple(machine_x.shape) == (1, 1)
    assert machine_map == {1: 0}

    machines["current_status"] = "Breakdown"
    with pytest.raises(ValueError, match="post-execution"):
        build_machine_node_features_v3(machines)


def test_v3_worker_features_use_prediction_time_static_fields_only():
    workers = pd.DataFrame({
        "worker_id": [2],
        "is_on_leave_at_prediction": [0],
        "efficiency_score": [85.0],
        "skill_breadth": [2],
    })
    worker_x, worker_map = build_worker_node_features_v3(workers)
    assert tuple(worker_x.shape) == (1, 4)
    assert worker_map == {2: 0}

    workers["actual_end_time"] = pd.Timestamp("2026-09-11")
    with pytest.raises(ValueError, match="forbidden Phase-B"):
        build_worker_node_features_v3(workers)
