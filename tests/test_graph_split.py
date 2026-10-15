"""
Regression tests for the Section 0F fix: the GNN train/val split must be
ORDER-level, not operation-level.

Context: precedence edges connect operations WITHIN one order only (routing
step i -> i+1). An operation-level split (a flat shuffle with no grouping)
let a validation node sit directly downstream, via message passing, of a
same-order TRAINING node -- measured against the real data before the fix:
66.3% of orders had operations split across both sides, 72.8% of validation
nodes had direct precedence-edge exposure to a same-order training sibling.
These tests encode the invariant the fix establishes: no order's operations
are ever split across train and val.
"""

import numpy as np
import pytest

from graph.graph_builder import _build_split_masks


def _order_ids_for(order_sizes: dict):
    """order_sizes: {order_id: n_operations} -> a flat order_ids array,
    aligned to node index, mirroring how graph_builder.py builds it."""
    ids = []
    for order_id, n in order_sizes.items():
        ids.extend([order_id] * n)
    return np.array(ids)


def test_no_order_is_split_across_train_and_val():
    order_sizes = {f"order{i}": (i % 7) + 1 for i in range(200)}  # varied sizes, 1-7 ops each
    order_ids = _order_ids_for(order_sizes)
    train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=42)

    train_np, val_np = train_mask.numpy(), val_mask.numpy()
    for order_id in set(order_ids):
        idxs = np.where(order_ids == order_id)[0]
        in_train = train_np[idxs]
        in_val = val_np[idxs]
        assert in_train.all() or in_val.all(), (
            f"{order_id} has operations split across train and val"
        )
        assert not (in_train.any() and in_val.any())


def test_every_node_is_exactly_one_of_train_or_val():
    order_ids = _order_ids_for({f"o{i}": 3 for i in range(50)})
    train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=1)
    both = train_mask & val_mask
    neither = ~train_mask & ~val_mask
    assert not both.any()
    assert not neither.any()


def test_val_fraction_is_respected_at_the_order_level():
    # 500 orders, 1 operation each -- with a single-op-per-order dataset,
    # order-level and node-level fractions coincide, giving a clean check.
    order_ids = _order_ids_for({f"o{i}": 1 for i in range(500)})
    _train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=42)
    frac = val_mask.float().mean().item()
    assert 0.15 < frac < 0.25


def test_split_is_reproducible_for_the_same_seed():
    order_ids = _order_ids_for({f"o{i}": 4 for i in range(100)})
    t1, v1 = _build_split_masks(order_ids, val_fraction=0.2, seed=42)
    t2, v2 = _build_split_masks(order_ids, val_fraction=0.2, seed=42)
    assert (t1 == t2).all()
    assert (v1 == v2).all()


def test_single_order_with_many_operations_stays_entirely_on_one_side():
    order_ids = np.array(["only_order"] * 60)
    train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=42)
    assert train_mask.all() or val_mask.all()


def test_empty_input_does_not_crash():
    order_ids = np.array([])
    train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=42)
    assert train_mask.numel() == 0
    assert val_mask.numel() == 0


def test_a_validation_orders_operations_have_no_train_side_same_order_neighbor():
    # The property that actually matters for the precedence-edge leakage
    # this fix closes: for every val node, EVERY other node sharing its
    # order is also a val node (so no same-order train-node neighbor can
    # exist to leak through a precedence edge in the first place).
    order_sizes = {f"order{i}": (i % 5) + 1 for i in range(300)}
    order_ids = _order_ids_for(order_sizes)
    train_mask, val_mask = _build_split_masks(order_ids, val_fraction=0.2, seed=7)
    val_np = val_mask.numpy()

    val_node_orders = set(order_ids[val_np])
    train_node_orders = set(order_ids[train_mask.numpy()])
    assert val_node_orders.isdisjoint(train_node_orders), (
        "an order appears on both sides -- same-order train/val leakage is possible"
    )
