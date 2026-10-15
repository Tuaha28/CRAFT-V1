"""Split-scoped Phase-B label access for GNN v3.

This is the only v3 module allowed to query actual execution timestamps.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import bindparam, text

from database.db import get_engine


def fetch_label_timing_for_orders_v3(order_ids) -> pd.DataFrame:
    """Return label timing for exactly the explicitly supplied order IDs."""
    if order_ids is None:
        raise ValueError("Explicit non-empty order_ids are required")
    ids = [int(value) for value in order_ids]
    if not ids:
        raise ValueError("Explicit non-empty order_ids are required")
    query = text("""
        SELECT
            production_order_id,
            operation_id,
            planned_start_time,
            planned_end_time,
            actual_start_time,
            actual_end_time
        FROM Job_Shop_Schedules
        WHERE production_order_id IN :order_ids
        ORDER BY production_order_id, operation_id, lot_index
    """).bindparams(bindparam("order_ids", expanding=True))
    return pd.read_sql(query, get_engine(), params={"order_ids": ids})


class SplitLabelAccessV3:
    """Auditable train/validation gate with checkpoint-sealed test access."""

    def __init__(self, order_splits: dict):
        required = {"train", "validation", "test"}
        if set(order_splits) != required:
            raise ValueError("Label gate requires train/validation/test splits")
        self._orders = {
            name: tuple(str(value) for value in order_splits[name])
            for name in required
        }
        if any(not values for values in self._orders.values()):
            raise ValueError("Every split must contain at least one order")
        sets = {name: set(values) for name, values in self._orders.items()}
        if (
            sets["train"] & sets["validation"]
            or sets["train"] & sets["test"]
            or sets["validation"] & sets["test"]
        ):
            raise ValueError("Label-gate order splits overlap")
        self._checkpoint_sha256 = None
        self.requests = []

    def _load(self, split_name: str):
        ids = self._orders[split_name]
        self.requests.append((split_name, ids))
        return fetch_label_timing_for_orders_v3(ids)

    def load_train(self):
        return self._load("train")

    def load_validation(self):
        return self._load("validation")

    def seal_final_checkpoint(self, checkpoint_sha256: str) -> None:
        value = str(checkpoint_sha256).lower()
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("A valid frozen checkpoint SHA-256 is required")
        self._checkpoint_sha256 = value

    def load_test_after_checkpoint(self):
        if self._checkpoint_sha256 is None:
            raise RuntimeError(
                "Test labels are sealed until a final checkpoint SHA-256 is recorded"
            )
        return self._load("test")

    @property
    def test_accessed(self) -> bool:
        return any(name == "test" for name, _ in self.requests)
