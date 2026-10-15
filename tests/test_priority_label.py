"""
Regression tests for PROJECT_STATUS.txt Section 0H.10: re-grounding the GNN
Priority head (Head 3) in raw, percentile-normalized deadline slack, instead
of `max(0.1, 1.0 - operation_sequence_number * 0.15)`.

Two designs were tried, in order, both measured against the real database
before either was kept:

  1. agent/fuzzy_logic.py::urgency_score's day-scale trapezoid. Rejected --
     its anchors (tuned for the LIVE breakdown-triage agent's real-time
     judgment) collapsed 97.8% of this dataset's 7,485 real operations to
     an identical 0.0, because every order here has at least 21 days of
     release-to-deadline budget and even the tightest operation in the
     whole dataset never dropped below ~14.6 days of slack -- nowhere near
     the anchors' 3/20-day span.
  2. Raw slack, PERCENTILE-RANKED within the operation set actually being
     labelled, rather than compared to any fixed day-scale constant. This
     is what's implemented and tested below -- self-calibrating by
     construction, so it can't repeat failure mode #1 regardless of what
     day-scale a future dataset happens to use.

The old label was a non-functional target for a second reason too: it was
fully determined by routing position alone, with no dependence on the
order's actual deadline -- a body-assembly step on a slack-rich order
always scored 0.85 and a tubular-sleeve-join step on a deadline-critical
order always scored 0.25, backwards from what ga/fitness.py's
total_priority_reward actually wants ("pull genuinely urgent work
earlier"). These tests confirm the label now (a) spans the full [0,1]
range regardless of the dataset's absolute day-scale, (b) varies WITHIN an
order by each operation's own planned_start_time, not just between
orders, (c) falls back sensibly when data is missing, and (d) excludes
missing-data operations from the ranking population so they can't distort
everyone else's percentile.
"""
import numpy as np
import pandas as pd
import pytest

from graph.labels import extract_gnn_labels, _slack_days_for, _percentile_rank_priority


def _merged_row(order_id, op_id, order_deadline=None, release_date=None, seq=1):
    return {
        'unique_op_key': f"{order_id}_{op_id}",
        'operation_sequence_number': seq,
        'sam': 30,
        'order_deadline': order_deadline,
        'release_date': release_date,
    }


def _avp_row(order_id, op_id, planned_start, planned_end):
    """Minimal df_actual_vs_planned row -- only the planned span matters for
    priority; actual_* columns are required by _aggregate_actual_vs_planned's
    dropna but irrelevant here, so set them equal to planned (on-time)."""
    return {
        'production_order_id': order_id, 'operation_id': op_id,
        'planned_start_time': pd.Timestamp(planned_start),
        'planned_end_time': pd.Timestamp(planned_end),
        'actual_start_time': pd.Timestamp(planned_start),
        'actual_end_time': pd.Timestamp(planned_end),
    }


# ---------------------------------------------------------------------------
# _slack_days_for
# ---------------------------------------------------------------------------

def test_slack_prefers_planned_start_over_release_date():
    deadline = pd.Timestamp('2026-01-20')
    planned_start = pd.Timestamp('2026-01-17')   # 3 days
    release_date = pd.Timestamp('2026-01-01')    # 19 days
    assert _slack_days_for(deadline, planned_start, release_date) == pytest.approx(3.0)


def test_slack_falls_back_to_release_date_when_planned_start_missing():
    deadline = pd.Timestamp('2026-01-20')
    release_date = pd.Timestamp('2026-01-05')  # 15 days
    assert _slack_days_for(deadline, None, release_date) == pytest.approx(15.0)


def test_slack_is_none_not_zero_when_deadline_missing():
    # A genuinely zero-slack order (deadline == reference) and a no-evidence
    # operation (no deadline at all) must never be indistinguishable.
    assert _slack_days_for(None, pd.Timestamp('2026-01-01'), pd.Timestamp('2026-01-01')) is None
    assert _slack_days_for(pd.NaT, pd.Timestamp('2026-01-01'), pd.Timestamp('2026-01-01')) is None


def test_slack_is_none_when_no_reference_at_all():
    assert _slack_days_for(pd.Timestamp('2026-01-20'), None, None) is None
    assert _slack_days_for(pd.Timestamp('2026-01-20'), pd.NaT, pd.NaT) is None


def test_slack_can_be_zero_for_a_genuinely_zero_slack_order():
    deadline = pd.Timestamp('2026-01-20')
    assert _slack_days_for(deadline, deadline, None) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _percentile_rank_priority
# ---------------------------------------------------------------------------

def test_least_slack_ranks_highest_priority():
    # idx 0: 30 days slack (most relaxed) -- idx 1: 2 days (most urgent).
    slack_by_idx = {0: 30.0, 1: 2.0}
    out = _percentile_rank_priority(slack_by_idx, num_nodes=2)
    assert out[1, 0] > out[0, 0]
    assert out[1, 0] == pytest.approx(1.0)  # sole most-urgent -> top percentile
    assert out[0, 0] == pytest.approx(0.0)  # sole most-relaxed -> bottom percentile


def test_full_range_is_used_regardless_of_absolute_day_scale():
    # The whole point of dropping urgency_score(): however large or small
    # the ABSOLUTE slack values are, the ranking spans [0,1] fully -- unlike
    # a fixed-anchor approach, which can collapse an entire dataset whose
    # scale doesn't match the anchors (see this file's module docstring).
    tight_scale = {i: float(i) for i in range(10)}          # 0..9 days
    wide_scale = {i: float(i) * 1000 for i in range(10)}    # 0..9000 days
    tight_out = _percentile_rank_priority(tight_scale, num_nodes=10)
    wide_out = _percentile_rank_priority(wide_scale, num_nodes=10)
    np.testing.assert_allclose(tight_out, wide_out)
    assert tight_out.max() == pytest.approx(1.0)
    assert tight_out.min() == pytest.approx(0.0)


def test_missing_data_defaults_to_neutral_and_is_excluded_from_ranking():
    # idx 2 has no slack at all -- must default to 0.5 and must not distort
    # idx 0/1's percentiles (i.e. ranking is computed over {0, 1} only).
    slack_by_idx = {0: 30.0, 1: 2.0}
    out = _percentile_rank_priority(slack_by_idx, num_nodes=3)
    assert out[2, 0] == pytest.approx(0.5)
    assert out[1, 0] == pytest.approx(1.0)
    assert out[0, 0] == pytest.approx(0.0)


def test_fewer_than_two_valid_values_defaults_everyone_to_neutral():
    assert (_percentile_rank_priority({}, num_nodes=3) == 0.5).all()
    assert (_percentile_rank_priority({0: 10.0}, num_nodes=3) == 0.5).all()


def test_all_identical_slack_values_default_to_neutral_not_an_arbitrary_tiebreak():
    slack_by_idx = {0: 15.0, 1: 15.0, 2: 15.0}
    out = _percentile_rank_priority(slack_by_idx, num_nodes=3)
    assert (out == 0.5).all()


def test_ties_are_averaged_not_arbitrarily_broken():
    slack_by_idx = {0: 5.0, 1: 5.0, 2: 20.0}
    out = _percentile_rank_priority(slack_by_idx, num_nodes=3)
    assert out[0, 0] == pytest.approx(out[1, 0])  # the tied pair rank identically
    assert out[0, 0] > out[2, 0]                  # both still beat the higher-slack one


# ---------------------------------------------------------------------------
# extract_gnn_labels -- Head 3 end to end
# ---------------------------------------------------------------------------

def test_two_operations_same_order_differ_by_their_own_planned_start():
    # Same order, same deadline -- but op 2 runs later than op 1, so it has
    # genuinely less runway left. A per-order-only label (or the old
    # seq-based one) cannot express this; a per-operation one must.
    deadline = pd.Timestamp('2026-01-31')
    merged = pd.DataFrame([
        _merged_row('20', '1', order_deadline=deadline, release_date=pd.Timestamp('2026-01-01'), seq=1),
        _merged_row('20', '2', order_deadline=deadline, release_date=pd.Timestamp('2026-01-01'), seq=2),
    ])
    op_map = {'20_1': 0, '20_2': 1}
    df = pd.DataFrame([
        _avp_row('20', '1', '2026-01-05', '2026-01-05 02:00'),   # 26 days slack
        _avp_row('20', '2', '2026-01-28', '2026-01-28 02:00'),   # 3 days slack
    ])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    op1_priority = labels['priority'][0].item()
    op2_priority = labels['priority'][1].item()
    assert op2_priority > op1_priority  # later step, less slack, more urgent
    assert op2_priority == pytest.approx(1.0)
    assert op1_priority == pytest.approx(0.0)


def test_two_orders_same_sequence_position_differ_by_deadline():
    # The core of the fix: the OLD label made every op-1 across every order
    # score identically (0.85), regardless of that order's own urgency. Two
    # different orders, both at sequence position 1, must now differ.
    merged = pd.DataFrame([
        _merged_row('21', '1', order_deadline=pd.Timestamp('2026-01-10'),
                     release_date=pd.Timestamp('2026-01-01'), seq=1),
        _merged_row('22', '1', order_deadline=pd.Timestamp('2026-06-01'),
                     release_date=pd.Timestamp('2026-01-01'), seq=1),
    ])
    op_map = {'21_1': 0, '22_1': 1}
    df = pd.DataFrame([
        _avp_row('21', '1', '2026-01-08', '2026-01-08 02:00'),   # 2 days slack
        _avp_row('22', '1', '2026-01-08', '2026-01-08 02:00'),   # ~144 days slack
    ])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['priority'][0].item() > labels['priority'][1].item()


def test_missing_planned_span_falls_back_to_release_date_end_to_end():
    merged = pd.DataFrame([
        _merged_row('23', '1', order_deadline=pd.Timestamp('2026-01-20'),
                     release_date=pd.Timestamp('2026-01-05'), seq=1),
        _merged_row('23', '2', order_deadline=pd.Timestamp('2026-06-01'),
                     release_date=pd.Timestamp('2026-01-05'), seq=2),
    ])
    op_map = {'23_1': 0, '23_2': 1}
    labels, _ = extract_gnn_labels(merged, op_map, None)  # no df_actual_vs_planned at all
    # op 1 (15 days slack) must rank strictly more urgent than op 2 (~147 days).
    assert labels['priority'][0].item() > labels['priority'][1].item()


def test_missing_order_deadline_end_to_end_is_neutral_not_a_crash():
    merged = pd.DataFrame([_merged_row('24', '1', order_deadline=None, release_date=None, seq=1)])
    op_map = {'24_1': 0}
    labels, _ = extract_gnn_labels(merged, op_map, None)
    assert labels['priority'][0].item() == pytest.approx(0.5)


def test_priority_output_is_always_bounded_zero_to_one():
    merged = pd.DataFrame([
        _merged_row('25', '1', order_deadline=pd.Timestamp('2026-01-05'),
                     release_date=pd.Timestamp('2026-06-01'), seq=1),  # negative slack (overdue)
        _merged_row('25', '2', order_deadline=pd.Timestamp('2030-01-01'),
                     release_date=pd.Timestamp('2026-01-01'), seq=2),  # huge slack
    ])
    op_map = {'25_1': 0, '25_2': 1}
    labels, _ = extract_gnn_labels(merged, op_map, None)
    p = labels['priority'].numpy()
    assert (p >= 0.0).all() and (p <= 1.0).all()
