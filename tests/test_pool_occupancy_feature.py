"""
Regression tests for graph/node_features.py::_compute_pool_occupancy and
the resulting pool_occupancy_norm feature (model/feature audit step 3, see
chat) -- motivated by mean_queue_delay_norm proving itself completion's
dominant signal (permutation R2 drop 0.82, a HISTORICAL AVERAGE of wait
time). pool_occupancy_norm is a different kind of signal: a structural
count of how many OTHER operations of the same type actually shared a
physical machine with this one, built from the same leak-free per-lot
assignment data machine_parallelism_norm already uses (fetch_operation_
assignments(), all lots, no execution_status filter) -- deliberately NOT
built from planned-time overlap, which would need either the Done-filtered
df_schedule (already documented, 0H.4, to understate planned spans for
partially-done operations) or the LABEL-ONLY df_actual_vs_planned.
"""
import pandas as pd
import pytest
import torch

from graph.node_features import _compute_pool_occupancy, build_operation_node_features


def _assignments(rows):
    """rows: [(order_id, op_id, asset_id), ...] -- one row per lot."""
    return pd.DataFrame(rows, columns=['production_order_id', 'operation_id', 'asset_id'])


def _routings_row(order_id='1', op_id='1', sam=30.0, qty=100,
                   order_deadline='2026-03-01', release_date='2026-01-01',
                   operation_sequence_number=1, required_machine_type='single-needle lockstitch'):
    return {
        'production_order_id': order_id, 'operation_id': op_id,
        'sam': sam, 'target_quantity': qty,
        'order_deadline': pd.Timestamp(order_deadline),
        'release_date': pd.Timestamp(release_date),
        'operation_sequence_number': operation_sequence_number,
        'required_machine_type': required_machine_type,
    }


# ---------------------------------------------------------------------------
# _compute_pool_occupancy directly
# ---------------------------------------------------------------------------

def test_two_same_type_operations_sharing_a_machine_count_each_other():
    assignments = _assignments([('1', '1', 100), ('2', '1', 100)])
    optype_by_key = {'1_1': 'body assembly', '2_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 1
    assert occ['2_1'] == 1


def test_different_type_operations_sharing_a_machine_do_not_count_each_other():
    # Same asset_id, but different operation_type -- the pool restriction
    # (same type only) must exclude this, not just "shares any machine".
    assignments = _assignments([('1', '1', 100), ('2', '1', 100)])
    optype_by_key = {'1_1': 'body assembly', '2_1': 'seam edge overlock'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 0
    assert occ['2_1'] == 0


def test_operation_never_counts_itself():
    # Multiple lots of the SAME operation on the SAME asset -- self-overlap
    # must not inflate its own count.
    assignments = _assignments([('1', '1', 100), ('1', '1', 100), ('1', '1', 101)])
    optype_by_key = {'1_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 0


def test_no_shared_machine_gives_zero_occupancy():
    assignments = _assignments([('1', '1', 100), ('2', '1', 200)])
    optype_by_key = {'1_1': 'body assembly', '2_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 0
    assert occ['2_1'] == 0


def test_distinct_other_operations_counted_once_even_with_multiple_shared_assets():
    # Op 1 and Op 2 share TWO assets -- still counts as ONE competitor each,
    # not two.
    assignments = _assignments([
        ('1', '1', 100), ('1', '1', 101),
        ('2', '1', 100), ('2', '1', 101),
    ])
    optype_by_key = {'1_1': 'body assembly', '2_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 1
    assert occ['2_1'] == 1


def test_three_way_pool_gets_full_pairwise_count():
    assignments = _assignments([('1', '1', 100), ('2', '1', 100), ('3', '1', 100)])
    optype_by_key = {'1_1': 'body assembly', '2_1': 'body assembly', '3_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ['1_1'] == 2
    assert occ['2_1'] == 2
    assert occ['3_1'] == 2


def test_empty_or_none_assignments_returns_empty_not_a_crash():
    optype_by_key = {'1_1': 'body assembly'}
    assert _compute_pool_occupancy(pd.DataFrame(), optype_by_key) == {}
    assert _compute_pool_occupancy(None, optype_by_key) == {}


def test_unmapped_optype_is_excluded_not_a_crash():
    assignments = _assignments([('1', '1', 100), ('2', '1', 100)])
    optype_by_key = {'1_1': None, '2_1': 'body assembly'}
    occ = _compute_pool_occupancy(assignments, optype_by_key)
    assert occ.get('1_1', 0) == 0
    assert occ.get('2_1', 0) == 0


# ---------------------------------------------------------------------------
# End-to-end: pool_occupancy_norm as column 8 of the feature vector
# ---------------------------------------------------------------------------

def test_pool_occupancy_norm_is_column_eight_and_reflects_real_sharing():
    df_routings = pd.DataFrame([
        _routings_row(op_id='1', required_machine_type='single-needle lockstitch'),
        _routings_row(op_id='2', required_machine_type='single-needle lockstitch'),
    ])
    assignments = _assignments([('1', '1', 100), ('1', '2', 100)])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_assignments=assignments)
    assert op_x.shape == (2, 12)  # 10 values + 2 missingness indicators
    expected = 1 / 1000.0  # one shared competitor, /1000.0 normalization
    assert op_x[op_map['1_1'], 8].item() == pytest.approx(expected)
    assert op_x[op_map['1_2'], 8].item() == pytest.approx(expected)


def test_pool_occupancy_norm_defaults_to_zero_without_assignments():
    df_routings = pd.DataFrame([_routings_row()])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_x[0, 8].item() == 0.0


def test_pool_occupancy_norm_is_clipped_not_unbounded():
    # A large pool (many operations, all sharing one asset) must not blow
    # up the feature scale past the clip.
    rows = [(str(i), '1', 100) for i in range(1, 5000)]
    assignments = _assignments(rows)
    df_routings = pd.DataFrame([
        _routings_row(order_id=str(i), op_id='1', required_machine_type='single-needle lockstitch')
        for i in range(1, 5000)
    ])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_assignments=assignments)
    assert op_x[:, 8].max() <= 3.0


# ---------------------------------------------------------------------------
# models.gnn -- the input layer must match the new feature count
# ---------------------------------------------------------------------------

def test_gnn_operation_input_layer_matches_ten_plus_embedding():
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
    # The trainable type embedding and training controls remain shared.
    # Split-encoder architecture (2026-08-23): separate encoder branches,
    # each with its own 'operation' projection -- both must match.
    assert model.lin_dict_delay['operation'].in_features == 16
    assert model.lin_dict_cp['operation'].in_features == 16
