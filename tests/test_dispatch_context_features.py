"""
Regression tests for the Section 0E feature-engineering follow-up: exposing
the seeder's existing causal delay factors (machine health, employee leave,
queue congestion, deadline proximity) as GNN operation-node features, sourced
only from information available BEFORE a lot executes.

Covers: correct mean aggregation for queue delay across an operation's many
lots, DECISIVE-LOT (max planned_end_time) selection for breakdown-history
and leave status -- not a mean/fraction across all ~19 lots (the
aggregation-mismatch fix, see chat/DELAY_LABEL_FIX_PROPOSAL.md: a mean
diluted a real per-lot causal signal to near-zero at the operation level,
since the delay label is MAX-based and driven by one decisive lot, not an
average of all of them) -- missing-data defaults, the 9-dimensional
operation feature vector, deadline-proximity computation, the GNN's input
layer actually matching that dimension (a hardcoded Linear(3, ...) would
crash on the first forward pass otherwise), and the two seeder-side formula
fixes (reversal 8): queue_delay_mins_at_dispatch measured against prev_end
instead of a dead shift-snap no-op, and machine health reframed as
persistent breakdown-count history after machine_status_at_dispatch was
measured to be identically 0.0 for every operation in the real database.
"""

import random

import numpy as np
import pandas as pd
import pytest

from data_generation.seed_database_fixed import compute_queue_delay_mins
from graph.node_features import _aggregate_dispatch_context, build_operation_node_features


# ---------------------------------------------------------------------------
# compute_queue_delay_mins (seeder-side formula fix)
# ---------------------------------------------------------------------------

def test_queue_delay_is_the_gap_between_ready_and_available():
    prev_end = pd.Timestamp('2026-01-01 08:00')      # lot was precedence-ready here
    earliest_possible = pd.Timestamp('2026-01-01 09:30')  # machine/employee busy until here
    assert compute_queue_delay_mins(earliest_possible, prev_end) == pytest.approx(90.0)


def test_queue_delay_is_zero_when_the_resource_was_already_free():
    same_time = pd.Timestamp('2026-01-01 08:00')
    assert compute_queue_delay_mins(same_time, same_time) == 0.0


def test_queue_delay_never_goes_negative():
    # earliest_possible = max(machine_free, employee_free, prev_end) can never
    # be earlier than prev_end by construction, but the floor is defensive.
    earliest_possible = pd.Timestamp('2026-01-01 08:00')
    prev_end = pd.Timestamp('2026-01-01 09:00')  # deliberately "later" to probe the floor
    assert compute_queue_delay_mins(earliest_possible, prev_end) == 0.0


# ---------------------------------------------------------------------------
# _aggregate_dispatch_context
# ---------------------------------------------------------------------------

_DEFAULT_PLANNED_END = pd.Timestamp('2026-01-01 00:00')


def _lot(order_id, op_id, on_leave, queue_delay, breakdown_count=0.0, machine_status='Active',
         planned_end_time=_DEFAULT_PLANNED_END):
    return {
        'production_order_id': order_id, 'operation_id': op_id,
        'machine_status_at_dispatch': machine_status,
        'employee_on_leave_at_dispatch': on_leave,
        'queue_delay_mins_at_dispatch': queue_delay,
        'machine_breakdown_count_at_dispatch': breakdown_count,
        'planned_end_time': planned_end_time,
    }


def test_none_or_empty_input_returns_empty_frame_not_a_crash():
    empty = _aggregate_dispatch_context(None)
    assert list(empty.columns) == [
        'production_order_id', 'operation_id',
        'decisive_lot_employee_on_leave',
        'mean_queue_delay_mins', 'decisive_lot_breakdown_count_at_dispatch',
        'mean_breakdown_count_at_dispatch',
        # Batch-4 fix 10a: the pre-fill missingness flag, so an operation
        # with genuinely null context can't be marked "context present".
        'context_observed',
    ]
    assert empty.empty
    assert _aggregate_dispatch_context(pd.DataFrame()).empty


def test_decisive_lot_is_the_one_with_max_planned_end_time_not_a_fraction():
    # AGGREGATION-MISMATCH FIX (see chat): nine early lots on leave, but the
    # DECISIVE lot (latest planned_end_time -- the one whose exposure should
    # drive the feature, since the delay label is MAX-based) is NOT on leave.
    # The old mean/fraction aggregation would have reported 0.9 ("high leave
    # risk"); the fix must report exactly the decisive lot's own status (0),
    # ignoring the other nine entirely.
    rows = [
        _lot('1', '1', on_leave=1, queue_delay=5.0,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=i))
        for i in range(9)
    ] + [
        _lot('1', '1', on_leave=0, queue_delay=5.0,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(days=1))
    ]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    val = agg.loc[agg['operation_id'] == '1', 'decisive_lot_employee_on_leave'].iloc[0]
    assert val == 0.0, "must reflect the decisive (latest-planned-end) lot, not a mean/fraction across all lots"


def test_decisive_lot_on_leave_is_captured_even_as_a_minority():
    # Mirror case: only the decisive lot (out of ten) is on leave. The old
    # mean/fraction aggregation would have reported 0.1 ("low leave risk");
    # the fix must report 1.0, since that lot alone determines the label.
    rows = [
        _lot('1', '1', on_leave=0, queue_delay=5.0,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=i))
        for i in range(9)
    ] + [
        _lot('1', '1', on_leave=1, queue_delay=5.0,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(days=1))
    ]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    val = agg.loc[agg['operation_id'] == '1', 'decisive_lot_employee_on_leave'].iloc[0]
    assert val == 1.0


def test_all_lots_dispatched_during_leave_reads_as_high_risk():
    rows = [_lot('2', '1', 1, 5.0, planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=i))
            for i in range(10)]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    assert agg.loc[agg['operation_id'] == '1', 'decisive_lot_employee_on_leave'].iloc[0] == 1.0


def test_null_dispatch_fields_are_unavailable_not_measured_zeros():
    # Pre-migration lots (seeded before these columns existed) come back as
    # NULL/NaN -- must read as "no evidence", not as a positive signal.
    # A NULL planned_end_time additionally means this lot can't be ranked for
    # decisive-lot selection at all; with no other lots for this operation,
    # the decisive_lot_* columns come back NaN here ("no rankable lot"), not
    # already 0.0 -- the 0-fill happens one layer up, in
    # build_operation_node_features's merge (see the dedicated test below),
    # consistent with every other missing-data case in that function.
    rows = [{
        'production_order_id': '4', 'operation_id': '1',
        'machine_status_at_dispatch': None,
        'employee_on_leave_at_dispatch': np.nan,
        'queue_delay_mins_at_dispatch': np.nan,
        'machine_breakdown_count_at_dispatch': np.nan,
        'planned_end_time': pd.NaT,
    }]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    row = agg.loc[agg['operation_id'] == '1'].iloc[0]
    # Batch-5 fix 4: a NULL context field is NOT a measured zero. The
    # guarded aggregates are made UNAVAILABLE (NaN) and the indicator says
    # so, instead of the old behaviour where a null silently became a
    # confident "no queue, no breakdowns" observation.
    assert row['context_observed'] == 0.0
    assert pd.isna(row['mean_queue_delay_mins'])
    assert pd.isna(row['mean_breakdown_count_at_dispatch'])
    assert pd.isna(row['decisive_lot_employee_on_leave'])
    assert pd.isna(row['decisive_lot_breakdown_count_at_dispatch'])


def test_queue_delay_is_meaned_not_saturated():
    # Deliberately still a MEAN across all lots -- out of scope for the
    # aggregation-mismatch fix (see _aggregate_dispatch_context's docstring).
    rows = [_lot('5', '1', 0, q) for q in (0.0, 10.0, 20.0, 30.0)]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    assert agg.loc[agg['operation_id'] == '1', 'mean_queue_delay_mins'].iloc[0] == pytest.approx(15.0)


def test_breakdown_count_reflects_the_decisive_lots_history_not_a_mean():
    # AGGREGATION-MISMATCH FIX: four lots with increasing breakdown-count
    # history as the machine ages; the LATEST-planned-end (decisive) lot has
    # the highest count (3). The feature must report that lot's own count
    # (3), not the mean across all four (1.5) -- the old behavior, which
    # diluted a real per-lot signal (r=0.0406 per-lot) to near-zero at the
    # operation level (r=-0.0019) because most of the ~19 lots averaged in
    # aren't the one that actually determines the MAX-based delay label.
    rows = [
        _lot('6', '1', 0, 0.0, breakdown_count=c,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=c))
        for c in (0, 1, 2, 3)
    ]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    assert agg.loc[agg['operation_id'] == '1', 'decisive_lot_breakdown_count_at_dispatch'].iloc[0] == pytest.approx(3.0)


def test_mean_breakdown_count_is_a_true_mean_not_the_decisive_lots_value():
    # Delay-label fix trade-off decision, option (b) (see
    # DELAY_LABEL_FIX_PROPOSAL.md's "IMPLEMENTATION STATUS"): completion gets
    # its own separate MEAN-based breakdown column, restored alongside (not
    # instead of) the decisive-lot one. Same four lots as the decisive-lot
    # test above (counts 0,1,2,3; decisive/latest-planned-end lot has count
    # 3) -- this column must report the mean (1.5), not the decisive lot's
    # own value (3.0).
    rows = [
        _lot('6', '1', 0, 0.0, breakdown_count=c,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=c))
        for c in (0, 1, 2, 3)
    ]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    assert agg.loc[agg['operation_id'] == '1', 'mean_breakdown_count_at_dispatch'].iloc[0] == pytest.approx(1.5)


def test_machine_status_at_dispatch_column_no_longer_drives_a_feature():
    # Section 0E / reversal 8: machine_status_at_dispatch was measured to
    # read 'Breakdown' for a scheduled machine NEVER (0.0 across all 7,500
    # real operations) -- the aggregation no longer builds a feature from it
    # at all, replaced by decisive_lot_breakdown_count_at_dispatch. Every lot
    # here reads 'Breakdown' and the aggregate must still show zero risk from
    # that column, since machine_status_at_dispatch is deliberately unused.
    rows = [_lot('7', '1', 0, 0.0, breakdown_count=0, machine_status='Breakdown') for _ in range(5)]
    df = pd.DataFrame(rows)
    agg = _aggregate_dispatch_context(df)
    assert 'frac_dispatched_machine_down' not in agg.columns
    assert agg.loc[agg['operation_id'] == '1', 'decisive_lot_breakdown_count_at_dispatch'].iloc[0] == 0.0


# ---------------------------------------------------------------------------
# build_operation_node_features -- the assembled 10-dim feature vector.
# History: was 7, grew to 9 (Section 0E dispatch-context features), grew to
# 9 again differently (routing_step_norm/steps_remaining_norm, graph-module
# review step 3), dropped to 8 (frac_employee_leave REMOVED -- model/
# feature audit step 1, confirmed genuine dead weight by two independent
# methods: real variance present, ~zero permutation importance on the
# trained model across all three heads), back to 9 with pool_occupancy_norm
# appended at the END (model/feature audit step 3) -- coincidence of counts
# with an earlier version, not a reversal. Now 10 with mean_breakdown_
# count_norm appended at the END (delay-label fix trade-off decision,
# option (b) -- completion's own separate mean-based aggregation of the
# same raw breakdown-count column, alongside the existing decisive-lot
# version; see DELAY_LABEL_FIX_PROPOSAL.md's "IMPLEMENTATION STATUS").
# routing_step_norm/steps_remaining_norm are columns 6,7 (unchanged by
# pool_occupancy_norm's or mean_breakdown_count_norm's addition, since both
# are appended after them, at columns 8 and 9).
# op_type_idx is returned as a 4th value, a separate LongTensor, not part
# of the float feature matrix at all.
# ---------------------------------------------------------------------------

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


def test_operation_feature_vector_is_twelve_dimensional():
    # 10 value columns + 2 missingness indicators (has_dispatch_context,
    # has_assignment_history) added so absence is distinguishable from a
    # genuine zero -- see graph/node_features.py's note at
    # has_dispatch_context.
    df_routings = pd.DataFrame([_routings_row()])
    op_x, op_map, merged, op_type_idx = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_x.shape == (1, 12)
    assert op_type_idx.shape == (1,)


def test_missing_dispatch_context_sets_its_indicator_to_zero():
    # The whole point of the indicator: the zero-filled value columns look
    # identical to a genuinely healthy operation, so the indicator is what
    # carries "this had no history at all".
    df_routings = pd.DataFrame([_routings_row()])
    op_x, _m, _mg, _t = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=None)
    assert op_x[0, 4].item() == 0.0      # zero-filled value column
    assert op_x[0, 10].item() == 0.0     # has_dispatch_context == absent


def test_present_dispatch_context_sets_its_indicator_to_one():
    df_routings = pd.DataFrame([_routings_row()])
    df_dispatch = pd.DataFrame([_lot('1', '1', 1, 100.0, breakdown_count=2.5)])
    op_x, _m, _mg, _t = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=df_dispatch)
    assert op_x[0, 10].item() == 1.0


def test_missing_dispatch_context_defaults_the_remaining_columns_to_zero():
    df_routings = pd.DataFrame([_routings_row()])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame(), df_dispatch_context=None)
    # columns 4,5 (0-indexed) are decisive_lot_breakdown_count_norm, mean_queue_delay_norm
    # (frac_employee_leave removed -- see module docstring above); column 9
    # is mean_breakdown_count_norm (appended at the end, option (b)).
    assert op_x[0, 4].item() == 0.0
    assert op_x[0, 5].item() == 0.0
    assert op_x[0, 9].item() == 0.0


def test_dispatch_context_values_flow_through_to_the_feature_vector():
    df_routings = pd.DataFrame([_routings_row()])
    df_dispatch = pd.DataFrame([_lot('1', '1', 1, 100.0, breakdown_count=2.5)])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=df_dispatch)
    assert op_x[0, 4].item() == pytest.approx(2.5 / 5.0)     # decisive_lot_breakdown_count_norm
    assert op_x[0, 5].item() == pytest.approx(100.0 / 200.0)  # mean_queue_delay_norm
    assert op_x[0, 9].item() == pytest.approx(2.5 / 5.0)      # mean_breakdown_count_norm (single lot: mean == decisive)


def test_mean_breakdown_count_norm_reflects_the_true_mean_across_lots():
    # Distinguishes column 9 (mean) from column 4 (decisive lot) with a
    # dataset where they genuinely differ -- same four-lot setup as
    # test_mean_breakdown_count_is_a_true_mean_not_the_decisive_lots_value
    # in test_dispatch_context_features's _aggregate_dispatch_context suite:
    # counts 0,1,2,3 -> decisive (latest planned_end_time) is 3, mean is 1.5.
    df_routings = pd.DataFrame([_routings_row()])
    df_dispatch = pd.DataFrame([
        _lot('1', '1', 0, 0.0, breakdown_count=c,
             planned_end_time=_DEFAULT_PLANNED_END + pd.Timedelta(hours=c))
        for c in (0, 1, 2, 3)
    ])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=df_dispatch)
    assert op_x[0, 4].item() == pytest.approx(3.0 / 5.0)  # decisive_lot_breakdown_count_norm
    assert op_x[0, 9].item() == pytest.approx(1.5 / 5.0)  # mean_breakdown_count_norm


def test_breakdown_count_norm_is_clipped_not_unbounded():
    df_routings = pd.DataFrame([_routings_row()])
    df_dispatch = pd.DataFrame([_lot('1', '1', 0, 0.0, breakdown_count=500.0)])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=df_dispatch)
    assert op_x[0, 4].item() <= 3.0


def test_missing_planned_end_time_marks_context_unobserved_not_partially_measured():
    # AGGREGATION-MISMATCH FIX: a lot with real breakdown/leave evidence but
    # no planned_end_time (can't be ranked for decisive-lot selection) must
    # not silently poison or crash the pipeline -- it falls back to the
    # standard "no evidence" 0-fill, same as no dispatch context at all.
    # queue delay is untouched (still mean-based, doesn't depend on
    # planned_end_time), so it must still flow through normally.
    df_routings = pd.DataFrame([_routings_row()])
    df_dispatch = pd.DataFrame([_lot('1', '1', on_leave=1, queue_delay=50.0,
                                      breakdown_count=3.0, planned_end_time=pd.NaT)])
    op_x, op_map, merged, _ = build_operation_node_features(
        df_routings, pd.DataFrame(), df_dispatch_context=df_dispatch)
    # Batch-5 fix 4: with no planned_end_time there is no rankable decisive
    # lot, so this operation's context is INCOMPLETE. Under the v2 schema
    # contract (see graph/feature_schema.py's `semantics`), the decisive and
    # both aggregate context features become unavailable and zero-filled,
    # and the indicator reports 0 -- the model is told "not observed" rather
    # than handed a real-looking 50-minute queue it cannot trust alongside a
    # fabricated decisive-lot zero.
    assert op_x[0, 4].item() == 0.0   # decisive_lot_breakdown_count_norm
    assert op_x[0, 5].item() == 0.0   # mean_queue_delay_norm -- guarded, zero-filled
    assert op_x[0, 10].item() == 0.0  # has_dispatch_context -- says it is unobserved


def test_deadline_budget_reflects_order_release_to_deadline_span():
    # 59-day budget (Jan 1 -> Mar 1), normalized by /60.
    df_routings = pd.DataFrame([_routings_row(order_deadline='2026-03-01', release_date='2026-01-01')])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    expected = (pd.Timestamp('2026-03-01') - pd.Timestamp('2026-01-01')).days / 60.0
    assert op_x[0, 3].item() == pytest.approx(expected, abs=1e-3)


def test_deadline_budget_is_clipped_not_unbounded():
    # A 2-year budget must not blow up the feature scale.
    df_routings = pd.DataFrame([_routings_row(order_deadline='2028-01-01', release_date='2026-01-01')])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_x[0, 3].item() <= 3.0


def test_deadline_budget_same_order_gives_same_value_across_operations():
    # Deliberately a property of the ORDER, not personalised per operation --
    # every operation in the same order shares one deadline budget.
    df_routings = pd.DataFrame([
        _routings_row(op_id='1', operation_sequence_number=1),
        _routings_row(op_id='2', operation_sequence_number=2),
    ])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_x[0, 3].item() == pytest.approx(op_x[1, 3].item())


def test_existing_three_features_are_unchanged_by_this_addition():
    df_routings = pd.DataFrame([_routings_row(sam=30.0, qty=100)])
    op_x, op_map, merged, _ = build_operation_node_features(df_routings, pd.DataFrame())
    assert op_x[0, 0].item() == pytest.approx(30.0 / 10.0)   # sam_norm
    assert op_x[0, 1].item() == pytest.approx(100.0 / 15000.0)  # qty_norm


# ---------------------------------------------------------------------------
# models.gnn -- the input layer must match the new feature count
# ---------------------------------------------------------------------------

def test_gnn_operation_input_layer_matches_the_new_feature_count():
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
    # each with its own 'operation' projection -- both must match, not just one.
    for branch_name, lin_dict in [('lin_dict_delay', model.lin_dict_delay), ('lin_dict_cp', model.lin_dict_cp)]:
        assert lin_dict['operation'].in_features == 16, (
            f"{branch_name}'s operation Linear layer must match build_operation_node_features' "
            "12-dim continuous output (frac_employee_leave removed, pool_occupancy_norm and "
            "mean_breakdown_count_norm added) plus the 4-dim operation_type embedding (12 + 4 = 16) "
            "or the first forward pass crashes"
        )
