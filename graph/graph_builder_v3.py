"""True-inductive, leak-free graph construction for unseen-order GNN v3."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import torch
from torch_geometric.data import HeteroData

from graph.data_access_v3 import (
    fetch_all_orders_and_routings_v3,
    fetch_dataset_identity_v3,
    fetch_frozen_assignments_v3,
    fetch_frozen_plan_v3,
    fetch_machine_state_v3,
    fetch_worker_state_v3,
)
from graph.edge_builder import (
    build_assignment_edges_into_operations,
    build_disjunctive_edges,
    build_precedence_edges,
)
from graph.feature_schema_v3 import operation_schema_v3
from graph.label_access_v3 import SplitLabelAccessV3
from graph.labels import extract_gnn_labels
from graph.node_features_v3 import (
    build_machine_node_features_v3,
    build_operation_node_features_v3,
    build_worker_node_features_v3,
)
from utils.factory_ownership import resolve_ownership
from utils.logger import get_logger


SPLIT_SEED_V3 = 42
TRAIN_FRACTION_V3 = 0.70
VALIDATION_FRACTION_V3 = 0.15
TEST_FRACTION_V3 = 0.15
SPLIT_NAMES = ("train", "validation", "test")

logger = get_logger("gnn_v3_graph", "gnn_v3_leakfree.log")


@dataclass
class SplitGraphBundle:
    name: str
    graph: HeteroData
    labels: dict
    label_meta: dict
    operation_mapping: dict
    merged_operations: object
    order_ids: tuple
    graph_fingerprint: str


def build_order_splits_v3(order_ids, seed: int = SPLIT_SEED_V3) -> dict:
    """Deterministic 70/15/15 whole-order split without label stratification."""
    unique = np.array(
        sorted({str(value) for value in order_ids}), dtype=object
    )
    if len(unique) < 3:
        raise ValueError("At least three orders are required")
    shuffled = unique[
        np.random.default_rng(seed).permutation(len(unique))
    ]
    train_end = int(len(unique) * TRAIN_FRACTION_V3)
    validation_end = train_end + int(
        len(unique) * VALIDATION_FRACTION_V3
    )
    splits = {
        "train": tuple(sorted(shuffled[:train_end].tolist())),
        "validation": tuple(
            sorted(shuffled[train_end:validation_end].tolist())
        ),
        "test": tuple(sorted(shuffled[validation_end:].tolist())),
    }
    sets = {name: set(values) for name, values in splits.items()}
    if (
        sets["train"] & sets["validation"]
        or sets["train"] & sets["test"]
        or sets["validation"] & sets["test"]
    ):
        raise AssertionError("Order split overlap")
    if set().union(*sets.values()) != set(unique):
        raise AssertionError("Order split does not cover the cohort")
    return splits


def order_id_hash(order_ids) -> str:
    payload = ",".join(sorted(str(value) for value in order_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_tensor(hasher, tensor: torch.Tensor) -> None:
    value = tensor.detach().cpu().contiguous().numpy()
    hasher.update(str(value.dtype).encode("ascii"))
    hasher.update(str(value.shape).encode("ascii"))
    hasher.update(value.tobytes())


def fingerprint_split_graph(
    graph: HeteroData,
    operation_mapping: dict,
    split_name: str,
) -> str:
    hasher = hashlib.sha256()
    hasher.update(split_name.encode("utf-8"))
    hasher.update(operation_schema_v3()["sha256"].encode("ascii"))
    hasher.update(
        ",".join(sorted(operation_mapping)).encode("utf-8")
    )
    for node_type in sorted(graph.node_types):
        hasher.update(node_type.encode("utf-8"))
        _hash_tensor(hasher, graph[node_type].x)
    _hash_tensor(hasher, graph["operation"].type_idx)
    for edge_type in sorted(graph.edge_types):
        hasher.update("|".join(edge_type).encode("utf-8"))
        _hash_tensor(hasher, graph[edge_type].edge_index)
    return hasher.hexdigest()


class FactoryGraphBuilderV3:
    """Independent order graphs sharing only known factory resource identities."""

    def __init__(
        self,
        *,
        ownership_snapshot=None,
        seed: int = SPLIT_SEED_V3,
    ):
        self.seed = int(seed)
        self.feature_schema = operation_schema_v3()
        self.df_machines = fetch_machine_state_v3()
        self.df_workers = fetch_worker_state_v3()
        self.df_routings = fetch_all_orders_and_routings_v3()
        self.df_plan = fetch_frozen_plan_v3()
        self.df_assignments = fetch_frozen_assignments_v3()
        self.dataset_identity = fetch_dataset_identity_v3()
        self.order_splits = build_order_splits_v3(
            self.df_routings["production_order_id"], self.seed
        )
        self.label_access = SplitLabelAccessV3(self.order_splits)
        self.ownership_snapshot = resolve_ownership(
            self.df_routings[
                "production_order_id"
            ].astype(str).unique(),
            ownership_snapshot,
        )
        self._assert_prediction_time_resource_state()

    def _assert_prediction_time_resource_state(self) -> None:
        forbidden = {
            "actual_start_time",
            "actual_end_time",
            "queue_delay_mins_at_dispatch",
            "machine_breakdown_count_at_dispatch",
            "machine_status_at_dispatch",
            "employee_on_leave_at_dispatch",
            "execution_status",
        }
        for frame, name in (
            (self.df_machines, "machine state"),
            (self.df_workers, "worker state"),
        ):
            leaked = forbidden.intersection(frame.columns)
            if leaked:
                raise ValueError(
                    f"{name} contains forbidden fields: {sorted(leaked)}"
                )

    @staticmethod
    def _filter_orders(frame, order_ids):
        allowed = {str(value) for value in order_ids}
        return frame[
            frame["production_order_id"].astype(str).isin(allowed)
        ].copy()

    def _assemble_split(
        self,
        split_name: str,
        timing=None,
        *,
        priority_cutpoints=None,
        unlabeled: bool = False,
    ) -> SplitGraphBundle:
        order_ids = self.order_splits[split_name]
        routings = self._filter_orders(self.df_routings, order_ids)
        plan = self._filter_orders(self.df_plan, order_ids)
        assignments = self._filter_orders(
            self.df_assignments, order_ids
        )

        machine_x, machine_map = build_machine_node_features_v3(
            self.df_machines
        )
        worker_x, worker_map = build_worker_node_features_v3(
            self.df_workers
        )
        operation_x, operation_map, merged, type_index = (
            build_operation_node_features_v3(
                routings, plan, assignments
            )
        )

        graph = HeteroData()
        graph["machine"].x = machine_x
        graph["worker"].x = worker_x
        graph["operation"].x = operation_x
        graph["operation"].type_idx = type_index
        graph[
            "operation", "precedes", "operation"
        ].edge_index = build_precedence_edges(merged, operation_map)
        graph[
            "operation", "contends_with", "operation"
        ].edge_index = build_disjunctive_edges(
            routings,
            operation_map,
            ownership_snapshot=self.ownership_snapshot,
        )
        machine_edges, worker_edges = (
            build_assignment_edges_into_operations(
                assignments,
                operation_map,
                machine_map,
                worker_map,
            )
        )
        graph[
            "machine", "operates", "operation"
        ].edge_index = machine_edges
        graph[
            "worker", "staffs", "operation"
        ].edge_index = worker_edges
        graph[
            "operation", "runs_on", "machine"
        ].edge_index = machine_edges.flip(0)
        graph[
            "operation", "staffed_by", "worker"
        ].edge_index = worker_edges.flip(0)

        local_mask = torch.ones(
            len(operation_map), dtype=torch.bool
        )
        if unlabeled:
            if timing is not None:
                raise ValueError("Unlabeled graph must not receive label timing")
            labels = {}
            label_meta = {"labels_loaded": False}
            graph["operation"].delay_valid_mask = torch.zeros_like(local_mask)
            graph["operation"].train_mask = torch.zeros_like(local_mask)
            graph["operation"].val_mask = torch.zeros_like(local_mask)
            graph["operation"].test_mask = torch.zeros_like(local_mask)
        elif split_name == "train":
            labels, label_meta = extract_gnn_labels(
                merged,
                operation_map,
                timing,
                train_mask=local_mask,
            )
        else:
            if priority_cutpoints is None:
                raise ValueError(
                    "Held-out priority labels need train cutpoints"
                )
            labels, label_meta = extract_gnn_labels(
                merged,
                operation_map,
                timing,
                priority_cutpoints=priority_cutpoints,
            )

        if not unlabeled:
            graph["operation"].delay_valid_mask = labels["delay_valid"]
            graph["operation"].train_mask = (
                local_mask if split_name == "train" else torch.zeros_like(local_mask)
            )
            graph["operation"].val_mask = (
                local_mask if split_name == "validation" else torch.zeros_like(local_mask)
            )
            graph["operation"].test_mask = (
                local_mask if split_name == "test" else torch.zeros_like(local_mask)
            )
        fingerprint = fingerprint_split_graph(
            graph, operation_map, split_name
        )
        return SplitGraphBundle(
            name=split_name,
            graph=graph,
            labels=labels,
            label_meta=label_meta,
            operation_mapping=operation_map,
            merged_operations=merged,
            order_ids=order_ids,
            graph_fingerprint=fingerprint,
        )

    def build_split(
        self,
        split_name: str,
        *,
        priority_cutpoints=None,
    ) -> SplitGraphBundle:
        """Build train/validation only. Test has a separate sealed API."""
        if split_name == "train":
            timing = self.label_access.load_train()
        elif split_name == "validation":
            timing = self.label_access.load_validation()
        elif split_name == "test":
            raise RuntimeError(
                "Use build_test_after_checkpoint for test data"
            )
        else:
            raise ValueError(f"Unknown split: {split_name}")
        return self._assemble_split(
            split_name,
            timing,
            priority_cutpoints=priority_cutpoints,
        )

    def build_train_validation(self):
        train = self.build_split("train")
        validation = self.build_split(
            "validation",
            priority_cutpoints=train.label_meta[
                "priority_cutpoints"
            ],
        )
        return train, validation

    def build_test_graph_unlabeled(self) -> SplitGraphBundle:
        """Build test topology/features without reading or sealing test labels."""
        return self._assemble_split("test", unlabeled=True)

    def build_test_after_checkpoint(
        self,
        *,
        priority_cutpoints,
        checkpoint_sha256: str,
    ) -> SplitGraphBundle:
        """The sole public final-test graph/label loader."""
        self.label_access.seal_final_checkpoint(
            checkpoint_sha256
        )
        timing = self.label_access.load_test_after_checkpoint()
        return self._assemble_split(
            "test",
            timing,
            priority_cutpoints=priority_cutpoints,
        )
