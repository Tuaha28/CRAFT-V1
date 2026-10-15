"""Consistency gates for the leak-free, pre-execution GNN v3 path."""

from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd
import pytest

from graph import data_access_v3, label_access_v3
from graph.feature_schema_v3 import (
    FORBIDDEN_PHASE_B_FEATURES,
    OPERATION_FEATURE_NAMES_V3,
    operation_schema_v3,
)
from graph.graph_builder_v3 import (
    FactoryGraphBuilderV3,
    build_order_splits_v3,
    order_id_hash,
)
from graph.label_access_v3 import SplitLabelAccessV3
from graph.node_features_v3 import (
    FORBIDDEN_INPUT_COLUMNS,
    build_machine_node_features_v3,
    build_operation_node_features_v3,
    build_worker_node_features_v3,
    compute_pool_occupancy_v3,
)


EXPECTED_FEATURES = (
    "sam_norm",
    "qty_norm",
    "machine_parallelism_norm",
    "deadline_budget_norm",
    "routing_step_norm",
    "steps_remaining_norm",
    "pool_occupancy_norm",
)


def test_schema_v3_is_exactly_seven_safe_features():
    schema = operation_schema_v3()
    assert schema["version"] == 3
    assert schema["count"] == 7
    assert tuple(schema["names"]) == EXPECTED_FEATURES
    assert OPERATION_FEATURE_NAMES_V3 == EXPECTED_FEATURES
    assert len(schema["sha256"]) == 64
    forbidden = set(FORBIDDEN_PHASE_B_FEATURES) | {"has_assignment_history"}
    assert forbidden.isdisjoint(schema["names"])


def test_label_access_requires_explicit_nonempty_order_ids(monkeypatch):
    with pytest.raises(TypeError):
        data_access_v3.fetch_label_timing_v3()
    with pytest.raises(ValueError):
        data_access_v3.fetch_label_timing_v3(None)
    with pytest.raises(ValueError):
        label_access_v3.fetch_label_timing_for_orders_v3(None)
    with pytest.raises(ValueError):
        label_access_v3.fetch_label_timing_for_orders_v3([])

    observed = {}

    def fake_read_sql(query, engine, params):
        observed["query"] = str(query)
        observed["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(label_access_v3.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(label_access_v3, "get_engine", lambda: object())
    data_access_v3.fetch_label_timing_v3(["11", "12"])
    assert "WHERE production_order_id IN" in observed["query"]
    assert observed["params"] == {"order_ids": [11, 12]}


def test_split_label_gate_restricts_each_stage_and_seals_test(monkeypatch):
    observed = []

    def fake_fetch(order_ids):
        values = tuple(order_ids)
        observed.append(values)
        return values

    monkeypatch.setattr(
        label_access_v3,
        "fetch_label_timing_for_orders_v3",
        fake_fetch,
    )
    splits = {
        "train": ("1", "2"),
        "validation": ("3",),
        "test": ("4",),
    }
    gate = SplitLabelAccessV3(splits)
    assert gate.load_train() == ("1", "2")
    assert gate.load_validation() == ("3",)
    assert observed == [("1", "2"), ("3",)]
    assert not gate.test_accessed

    with pytest.raises(RuntimeError):
        gate.load_test_after_checkpoint()
    for invalid in ("", "abc", "0" * 63, "g" * 64):
        with pytest.raises(ValueError):
            gate.seal_final_checkpoint(invalid)

    gate.seal_final_checkpoint("a" * 64)
    assert gate.load_test_after_checkpoint() == ("4",)
    assert gate.test_accessed
    assert gate.requests == [
        ("train", ("1", "2")),
        ("validation", ("3",)),
        ("test", ("4",)),
    ]


def test_split_label_gate_rejects_missing_or_overlapping_sets():
    with pytest.raises(ValueError):
        SplitLabelAccessV3({"train": ("1",), "validation": ("2",)})
    with pytest.raises(ValueError):
        SplitLabelAccessV3({
            "train": ("1",),
            "validation": ("1",),
            "test": ("3",),
        })
    with pytest.raises(ValueError):
        SplitLabelAccessV3({
            "train": (),
            "validation": ("2",),
            "test": ("3",),
        })


def test_order_split_is_grouped_deterministic_disjoint_and_hashable():
    order_ids = [value for value in range(100) for _ in range(3)]
    first = build_order_splits_v3(order_ids, seed=42)
    second = build_order_splits_v3(order_ids, seed=42)
    assert first == second
    assert {name: len(values) for name, values in first.items()} == {
        "train": 70,
        "validation": 15,
        "test": 15,
    }
    sets = {name: set(values) for name, values in first.items()}
    assert not (sets["train"] & sets["validation"])
    assert not (sets["train"] & sets["test"])
    assert not (sets["validation"] & sets["test"])
    assert set().union(*sets.values()) == {str(value) for value in range(100)}
    assert all(len(order_id_hash(values)) == 64 for values in first.values())


def test_graph_builder_has_separate_final_test_entrypoint():
    builder = FactoryGraphBuilderV3.__new__(FactoryGraphBuilderV3)
    with pytest.raises(RuntimeError, match="build_test_after_checkpoint"):
        builder.build_split("test")
    assert "checkpoint_sha256" in inspect.signature(
        FactoryGraphBuilderV3.build_test_after_checkpoint
    ).parameters


def test_v3_cohort_query_has_no_final_status_filter(monkeypatch):
    observed = {}

    def fake_read_sql(query, engine):
        observed["query"] = str(query)
        return pd.DataFrame()

    monkeypatch.setattr(data_access_v3.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(data_access_v3, "get_engine", lambda: object())
    data_access_v3.fetch_all_orders_and_routings_v3()
    normalized = " ".join(observed["query"].lower().split())
    assert "from production_orders" in normalized
    assert "where po.status" not in normalized
    assert "pending" not in normalized
    assert "processing" not in normalized
    assert "completed" not in normalized


def _safe_feature_frames():
    routings = pd.DataFrame({
        "production_order_id": [1, 1],
        "operation_id": [10, 20],
        "target_quantity": [100, 100],
        "sam": [2.0, 3.0],
        "order_deadline": pd.to_datetime(["2026-10-10", "2026-10-10"]),
        "release_date": pd.to_datetime(["2026-09-10", "2026-09-10"]),
        "operation_sequence_number": [1, 2],
        "required_machine_type": ["Single-Needle Lockstitch", "Single-Needle Lockstitch"],
    })
    plan = pd.DataFrame({
        "production_order_id": [1, 1],
        "operation_id": [10, 20],
        "planned_start_time": pd.to_datetime([
            "2026-09-10 06:00", "2026-09-10 08:00"
        ]),
        "planned_end_time": pd.to_datetime([
            "2026-09-10 08:00", "2026-09-10 10:00"
        ]),
    })
    assignments = pd.DataFrame({
        "production_order_id": [1, 1],
        "operation_id": [10, 20],
        "asset_id": [100, 100],
        "employee_id": [200, 201],
    })
    return routings, plan, assignments


def test_v3_features_are_seven_wide_and_reject_phase_b_columns():
    routings, plan, assignments = _safe_feature_frames()
    features, mapping, merged, type_index = build_operation_node_features_v3(
        routings, plan, assignments
    )
    assert tuple(features.shape) == (2, 7)
    assert len(mapping) == len(merged) == len(type_index) == 2

    for column in FORBIDDEN_INPUT_COLUMNS:
        contaminated = plan.copy()
        contaminated[column] = 0
        with pytest.raises(ValueError, match="forbidden Phase-B"):
            build_operation_node_features_v3(
                routings, contaminated, assignments
            )


def test_pool_occupancy_v3_is_pure_phase_a_and_rejects_contamination():
    _, _, assignments = _safe_feature_frames()
    operation_types = {"1_10": "body assembly", "1_20": "body assembly"}
    assert compute_pool_occupancy_v3(assignments, operation_types) == {
        "1_10": 1,
        "1_20": 1,
    }
    source = inspect.getsource(compute_pool_occupancy_v3)
    assert "read_sql" not in source
    assert "get_engine" not in source
    contaminated = assignments.copy()
    contaminated["execution_status"] = "Done"
    with pytest.raises(ValueError, match="forbidden Phase-B"):
        compute_pool_occupancy_v3(contaminated, operation_types)


def test_actual_timestamp_sql_exists_only_in_dedicated_label_module():
    graph_root = Path(__file__).resolve().parents[1] / "graph"
    direct_query_files = []
    for path in graph_root.glob("*_v3.py"):
        text = path.read_text(encoding="utf-8")
        if "SELECT" in text and (
            "actual_start_time" in text or "actual_end_time" in text
        ):
            direct_query_files.append(path.name)
    assert direct_query_files == ["label_access_v3.py"]

