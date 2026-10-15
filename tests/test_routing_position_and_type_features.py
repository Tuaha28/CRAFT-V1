"""
Regression tests for the graph-module review's step 3: routing_step_norm,
steps_remaining_norm, and the operation_type embedding index -- all three
independently flagged as gaps by both this project's own structure audit
and an external review before either party knew of the other's finding.

routing_step_norm/steps_remaining_norm: how far into its OWN order's
routing this operation sits, computed relative to that order's own routing
length (not a fixed scale) -- now a real, variable quantity since routing
length varies 6-10 steps by garment type (routing_tables.py), where it used
to be a fixed 5 for every product.

operation_type: which of the canonical machine/operation types this
operation itself requires -- previously shaped only the graph's TOPOLOGY
(which machine nodes connect, which disjunctive pool), never appeared in
the operation's own feature vector. Returned as an integer index (not
one-hot, not folded into the float feature tensor) because models/gnn.py
turns it into a learned nn.Embedding -- growable if a 6th type is ever
added, unlike a one-hot whose width is fixed at construction. Index
resolution goes through the single shared utils/machine_taxonomy.py
mapping, not a second independently-computed vocabulary.
"""
import pandas as pd
import pytest
import torch

from graph.node_features import build_operation_node_features
from utils.machine_taxonomy import (
    operation_type_index, NUM_OPERATION_TYPES, UNKNOWN_OPERATION_TYPE_INDEX,
    OPERATION_TYPE_TO_INDEX,
)


def _routings_row(order_id='1', op_id='1', seq=1, required_machine_type='single-needle lockstitch'):
    return {
        'production_order_id': order_id, 'operation_id': op_id,
        'sam': 30.0, 'target_quantity': 100,
        'order_deadline': pd.Timestamp('2026-03-01'),
        'release_date': pd.Timestamp('2026-01-01'),
        'operation_sequence_number': seq,
        'required_machine_type': required_machine_type,
    }


# ---------------------------------------------------------------------------
# routing_step_norm / steps_remaining_norm -- columns 6, 7 of the (now)
# 9-dim vector (were 7, 8 of the original 9 before frac_employee_leave's
# removal shifted everything after it down by one; unaffected by
# pool_occupancy_norm's later addition since that's appended at column 8,
# after these two -- see test_dispatch_context_features.py)
# ---------------------------------------------------------------------------

def test_first_and_last_step_of_a_four_step_routing():
    df_routings = pd.DataFrame([_routings_row(op_id=str(i), seq=i) for i in range(1, 5)])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())

    first_idx = op_map['1_1']
    last_idx = op_map['1_4']
    assert op_x[first_idx, 6].item() == pytest.approx(1 / 4)   # routing_step_norm
    assert op_x[first_idx, 7].item() == pytest.approx(3 / 4)   # steps_remaining_norm
    assert op_x[last_idx, 6].item() == pytest.approx(1.0)
    assert op_x[last_idx, 7].item() == pytest.approx(0.0)


def test_routing_step_norm_and_steps_remaining_norm_always_sum_to_one():
    df_routings = pd.DataFrame([_routings_row(op_id=str(i), seq=i) for i in range(1, 8)])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    for i in range(op_x.shape[0]):
        assert op_x[i, 6].item() + op_x[i, 7].item() == pytest.approx(1.0)


def test_relative_position_is_comparable_across_different_routing_lengths():
    # The whole point of normalizing by each order's OWN length rather than
    # a fixed scale: step 2-of-4 and step 3-of-6 are both "about halfway",
    # even though routing length differs (6-10 steps varies by garment
    # type in the real data).
    df_routings = pd.DataFrame([
        _routings_row(order_id='A', op_id='2', seq=2),  # 2-of-4 -> 0.5
        _routings_row(order_id='A', op_id='1', seq=1),
        _routings_row(order_id='A', op_id='3', seq=3),
        _routings_row(order_id='A', op_id='4', seq=4),
        _routings_row(order_id='B', op_id='3', seq=3),  # 3-of-6 -> 0.5
        _routings_row(order_id='B', op_id='1', seq=1),
        _routings_row(order_id='B', op_id='2', seq=2),
        _routings_row(order_id='B', op_id='4', seq=4),
        _routings_row(order_id='B', op_id='5', seq=5),
        _routings_row(order_id='B', op_id='6', seq=6),
    ])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    a_half = op_x[op_map['A_2'], 6].item()
    b_half = op_x[op_map['B_3'], 6].item()
    assert a_half == pytest.approx(0.5)
    assert b_half == pytest.approx(0.5)


def test_single_step_routing_is_both_first_and_last():
    df_routings = pd.DataFrame([_routings_row(seq=1)])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    idx = op_map['1_1']
    assert op_x[idx, 6].item() == pytest.approx(1.0)
    assert op_x[idx, 7].item() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# operation_type_index (utils/machine_taxonomy.py) -- the single shared
# vocabulary the GNN's embedding indexes into.
# ---------------------------------------------------------------------------

def test_every_canonical_operation_type_gets_a_distinct_index():
    indices = set(OPERATION_TYPE_TO_INDEX.values())
    assert len(indices) == len(OPERATION_TYPE_TO_INDEX) == 5
    assert indices == set(range(5))


def test_unknown_operation_type_maps_to_the_reserved_index_not_a_crash():
    assert operation_type_index('a brand new garment process') == UNKNOWN_OPERATION_TYPE_INDEX
    assert operation_type_index(None) == UNKNOWN_OPERATION_TYPE_INDEX


def test_num_operation_types_includes_the_unknown_bucket():
    assert NUM_OPERATION_TYPES == len(OPERATION_TYPE_TO_INDEX) + 1


# ---------------------------------------------------------------------------
# op_type_idx as returned by build_operation_node_features -- aligned to
# the same node ordering as op_x/op_map, routed through the single shared
# taxonomy (not re-derived ad-hoc here).
# ---------------------------------------------------------------------------

def test_op_type_idx_is_a_long_tensor_aligned_with_op_map():
    df_routings = pd.DataFrame([
        _routings_row(op_id='1', seq=1, required_machine_type='single-needle lockstitch'),
        _routings_row(op_id='2', seq=2, required_machine_type='overlock serger (4-thread)'),
    ])
    op_x, op_map, merged, op_type_idx = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_type_idx.dtype == torch.long
    assert op_type_idx.shape == (2,)
    assert op_type_idx[op_map['1_1']].item() == operation_type_index('body assembly')
    assert op_type_idx[op_map['1_2']].item() == operation_type_index('seam edge overlock')


def test_op_type_idx_falls_back_to_unknown_for_an_unmapped_machine_type():
    df_routings = pd.DataFrame([_routings_row(required_machine_type='some-future-machine-class')])
    op_x, op_map, merged, op_type_idx = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_type_idx[0].item() == UNKNOWN_OPERATION_TYPE_INDEX


# ---------------------------------------------------------------------------
# models.gnn -- the operation_type embedding itself
# ---------------------------------------------------------------------------

def test_gnn_operation_type_embedding_table_has_room_for_growth():
    from models.gnn import MultiTaskFactoryGNN
    metadata = (
        ['machine', 'worker', 'operation'],
        [
            ('operation', 'precedes', 'operation'),
            ('machine', 'operates', 'operation'),
            ('worker', 'staffs', 'operation'),
            ('operation', 'runs_on', 'machine'),
            ('operation', 'staffed_by', 'worker'),
        ],
    )
    model = MultiTaskFactoryGNN(metadata=metadata, hidden_channels=8, operation_in_dim=12)
    assert model.op_type_embedding.num_embeddings == NUM_OPERATION_TYPES == 6
    # Every valid index, including the reserved "unknown" one, must be a
    # legal lookup -- not just the 5 currently-real types.
    idxs = torch.tensor([0, 1, 2, 3, 4, UNKNOWN_OPERATION_TYPE_INDEX])
    emb = model.op_type_embedding(idxs)
    assert emb.shape == (6, 4)  # op_type_embed_dim default
