"""
Regression tests for the GNN delay-label fix (PROJECT_STATUS.txt Section
0D): genuine plan-vs-actual execution variance replacing the old
execution_status-derived label, which was ~99.9% equivalent to "has this
operation started" rather than a real risk signal.

Two things are tested: the seeder's new variance-multiplier helper (bounded,
believable spread), and graph/labels.py's new label derivation (correct
threshold behaviour, and -- the actual point of the fix -- structurally
independent of execution_status/now_time, since the new code path never
reads either).
"""

import random

import numpy as np
import pandas as pd
import pytest

from data_generation.seed_database_fixed import (
    sample_execution_variance_multiplier,
    EXECUTION_VARIANCE_MIN,
    EXECUTION_VARIANCE_MAX,
)
from graph.labels import (
    extract_gnn_labels,
    _aggregate_actual_vs_planned,
    DELAY_LABEL_LATENESS_FRACTION,
)


# ---------------------------------------------------------------------------
# sample_execution_variance_multiplier
# ---------------------------------------------------------------------------

def test_variance_multiplier_is_always_within_the_clipped_range():
    rng = random.Random(1)
    for _ in range(5000):
        m = sample_execution_variance_multiplier(rng)
        assert EXECUTION_VARIANCE_MIN <= m <= EXECUTION_VARIANCE_MAX


def test_variance_multiplier_is_centered_near_one_over_many_draws():
    rng = random.Random(2)
    vals = [sample_execution_variance_multiplier(rng) for _ in range(5000)]
    assert 0.95 < (sum(vals) / len(vals)) < 1.05


def test_variance_multiplier_has_real_spread_not_a_constant():
    # A degenerate "variance" that always returns ~1.0 would defeat the whole
    # point -- there would be nothing for actual_end_time to diverge from
    # planned_end_time, and the label would go right back to being constant.
    rng = random.Random(3)
    vals = [sample_execution_variance_multiplier(rng) for _ in range(2000)]
    assert np.std(vals) > 0.05


# ---------------------------------------------------------------------------
# Delay-label fix (DELAY_LABEL_FIX_PROPOSAL.md, approved -- see chat):
# machine breakdown history, worker leave, and queue delay now shift the
# variance draw's distribution parameters. All three default to their old
# no-effect values (0.0/False/0.0), so every test above (calling the
# function with no extra kwargs) exercises the EXACT old pure-noise
# behaviour unchanged -- this is an additive generalization, not a
# breaking change, and these new tests are what actually exercise the
# new behaviour.
# ---------------------------------------------------------------------------

def test_defaults_reproduce_the_exact_old_pure_noise_distribution():
    rng_old = random.Random(42)
    rng_new = random.Random(42)
    old_style = [min(1.6, max(0.65, rng_old.gauss(1.0, 0.15))) for _ in range(500)]
    new_style_defaults = [sample_execution_variance_multiplier(rng_new) for _ in range(500)]
    assert old_style == new_style_defaults


def test_breakdown_history_shifts_the_mean_up():
    rng_clean = random.Random(10)
    rng_dirty = random.Random(10)
    clean_machine = [sample_execution_variance_multiplier(rng_clean, machine_breakdown_count_at_dispatch=0)
                      for _ in range(3000)]
    dirty_machine = [sample_execution_variance_multiplier(rng_dirty, machine_breakdown_count_at_dispatch=9)
                      for _ in range(3000)]
    # Same seed sequence, only the breakdown count differs -- any mean
    # difference is attributable to the effect, not draw-to-draw noise.
    assert np.mean(dirty_machine) > np.mean(clean_machine)
    assert np.mean(dirty_machine) - np.mean(clean_machine) == pytest.approx(0.08, abs=0.02)


def test_breakdown_effect_is_capped_not_unbounded():
    rng = random.Random(11)
    huge_count = [sample_execution_variance_multiplier(rng, machine_breakdown_count_at_dispatch=1000)
                  for _ in range(2000)]
    # Effective mean should sit at the 8% cap (1.08), not run away with the
    # (absurd) input count -- checked via the sample mean landing near 1.08,
    # not at some unbounded value.
    assert 1.0 < np.mean(huge_count) < 1.15


def test_leave_shifts_the_mean_up_by_a_flat_amount():
    rng_present = random.Random(20)
    rng_leave = random.Random(20)
    present = [sample_execution_variance_multiplier(rng_present, on_leave=False) for _ in range(3000)]
    on_leave = [sample_execution_variance_multiplier(rng_leave, on_leave=True) for _ in range(3000)]
    assert np.mean(on_leave) > np.mean(present)
    assert np.mean(on_leave) - np.mean(present) == pytest.approx(0.10, abs=0.02)


def test_queue_delay_widens_spread_not_the_mean():
    rng_quiet = random.Random(30)
    rng_congested = random.Random(30)
    quiet = [sample_execution_variance_multiplier(rng_quiet, queue_delay_mins=0) for _ in range(4000)]
    congested = [sample_execution_variance_multiplier(rng_congested, queue_delay_mins=10000) for _ in range(4000)]
    # The SD should widen meaningfully...
    assert np.std(congested) > np.std(quiet) * 1.2
    # ...but the MEAN should stay close to the same -- this is a spread
    # effect, not a disguised mean-shift (the structural point of Section
    # 2c: queue delay already shifts planned and actual start together,
    # so a mean-shift here would double-count the same congestion).
    assert abs(np.mean(congested) - np.mean(quiet)) < 0.03


def test_queue_delay_sd_widening_is_capped():
    rng = random.Random(31)
    absurd_queue = [sample_execution_variance_multiplier(rng, queue_delay_mins=10_000_000)
                     for _ in range(3000)]
    quiet_rng = random.Random(31)
    quiet = [sample_execution_variance_multiplier(quiet_rng, queue_delay_mins=0) for _ in range(3000)]
    # Capped at +50% SD (0.15 -> 0.225), not unbounded with an absurd input.
    assert np.std(absurd_queue) < np.std(quiet) * 1.8


def test_effects_stack_but_stay_inside_the_existing_clip_bounds():
    # Worst-case realistic stacking (breakdown 8% + leave 10% + max queue
    # SD widening) -- verify the result is still governed by the SAME
    # EXECUTION_VARIANCE_MIN/MAX clip, per the proposal's explicit check
    # that these didn't need to change.
    rng = random.Random(40)
    vals = [sample_execution_variance_multiplier(
        rng, machine_breakdown_count_at_dispatch=9, on_leave=True, queue_delay_mins=10000,
    ) for _ in range(5000)]
    assert all(EXECUTION_VARIANCE_MIN <= v <= EXECUTION_VARIANCE_MAX for v in vals)
    # And the mean should sit meaningfully above the unshifted 1.0 baseline,
    # confirming the stacked effects are actually landing (not silently
    # cancelling or clipped away entirely).
    assert np.mean(vals) > 1.10


# ---------------------------------------------------------------------------
# graph/labels.py -- label derivation
# ---------------------------------------------------------------------------

def _row(order_id, op_id, planned_start, planned_end, actual_start, actual_end):
    return {
        'production_order_id': order_id,
        'operation_id': op_id,
        'planned_start_time': pd.Timestamp(planned_start),
        'planned_end_time': pd.Timestamp(planned_end),
        'actual_start_time': pd.Timestamp(actual_start),
        'actual_end_time': pd.Timestamp(actual_end),
    }


def _merged_ops_row(order_id, op_id, seq=1):
    key = f"{order_id}_{op_id}"
    return {
        'unique_op_key': key,
        'planned_start_time': pd.Timestamp('2026-01-01 08:00'),
        'planned_end_time': pd.Timestamp('2026-01-01 09:00'),
        'operation_sequence_number': seq,
        'sam': 30,
    }


def test_operation_finishing_well_within_plan_is_not_delayed():
    # 100-minute plan, finished 5 minutes late -- 5% over, under the 20% bar.
    df = pd.DataFrame([_row('1', '1', '2026-01-01 08:00', '2026-01-01 09:40',
                             '2026-01-01 08:00', '2026-01-01 09:45')])
    op_map = {'1_1': 0}
    merged = pd.DataFrame([_merged_ops_row('1', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['delay'][0].item() == 0.0


def test_operation_finishing_well_past_plan_is_delayed():
    # 100-minute plan, finished 40 minutes late -- 40% over, clears the bar.
    df = pd.DataFrame([_row('2', '1', '2026-01-01 08:00', '2026-01-01 09:40',
                             '2026-01-01 08:00', '2026-01-01 10:20')])
    op_map = {'2_1': 0}
    merged = pd.DataFrame([_merged_ops_row('2', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['delay'][0].item() == 1.0


def test_operation_finishing_early_is_not_delayed():
    df = pd.DataFrame([_row('3', '1', '2026-01-01 08:00', '2026-01-01 09:40',
                             '2026-01-01 08:00', '2026-01-01 09:20')])
    op_map = {'3_1': 0}
    merged = pd.DataFrame([_merged_ops_row('3', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['delay'][0].item() == 0.0


def test_threshold_is_the_endorsed_twenty_percent():
    assert DELAY_LABEL_LATENESS_FRACTION == pytest.approx(0.20)


def test_missing_actual_vs_planned_data_defaults_to_not_delayed_not_a_crash():
    op_map = {'4_1': 0}
    merged = pd.DataFrame([_merged_ops_row('4', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, None)
    assert labels['delay'][0].item() == 0.0
    labels_empty, _ = extract_gnn_labels(merged, op_map, pd.DataFrame())
    assert labels_empty['delay'][0].item() == 0.0


def test_rows_missing_actual_timing_are_dropped_not_treated_as_on_time():
    # A lot with no recorded actual outcome at all (e.g. pre-migration data)
    # must not silently count as evidence of punctuality.
    df = pd.DataFrame([{
        'production_order_id': '5', 'operation_id': '1',
        'planned_start_time': pd.Timestamp('2026-01-01 08:00'),
        'planned_end_time': pd.Timestamp('2026-01-01 09:00'),
        'actual_start_time': pd.NaT,
        'actual_end_time': pd.NaT,
    }])
    agg = _aggregate_actual_vs_planned(df)
    assert agg == {}


def test_zero_length_planned_span_does_not_crash_or_divide_by_zero():
    df = pd.DataFrame([_row('6', '1', '2026-01-01 08:00', '2026-01-01 08:00',
                             '2026-01-01 08:00', '2026-01-01 08:05')])
    agg = _aggregate_actual_vs_planned(df)
    # Guarded to a minimum 1-minute planned span rather than raising or
    # producing inf/NaN.
    _, planned_duration, _, _ = agg['6_1']
    assert planned_duration >= 1.0


def test_label_does_not_depend_on_execution_status_column_at_all():
    # The core of the fix: the new aggregation never reads execution_status
    # or any notion of "now" -- structurally guaranteed here by never
    # supplying that column in the first place and confirming behaviour is
    # unaffected either way.
    late = _row('7', '1', '2026-01-01 08:00', '2026-01-01 09:00',
                '2026-01-01 08:00', '2026-01-01 09:45')
    df_no_status = pd.DataFrame([late])
    assert 'execution_status' not in df_no_status.columns
    op_map = {'7_1': 0}
    merged = pd.DataFrame([_merged_ops_row('7', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df_no_status)
    assert labels['delay'][0].item() == 1.0


def test_operation_completion_is_gated_by_its_last_finishing_lot():
    # An operation's quantity is split across ~60 PARALLEL lots (real
    # machine-level parallelism) -- it isn't "done" until the last of them
    # finishes, exactly how ga/fitness.py already treats an operation's own
    # completion. So one badly-late lot must flag the operation delayed even
    # if another lot of the same operation finished early: the early lot
    # cannot cancel out the late one, because the operation's real-world
    # completion is gated by whichever lot finishes last.
    #
    # (An earlier version of this aggregation used the MEAN of each lot's own
    # lateness instead -- measured against the real database, averaging ~60
    # roughly-independent per-lot variance draws crushed the spread by the
    # Central Limit Theorem and produced a 0.0% positive rate, a second
    # degenerate label. See PROJECT_STATUS.txt Section 0D.)
    rows = [
        _row('8', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:36'),   # this lot: +60%, and finishes LAST
        _row('8', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 08:30'),   # this lot: -30%, finishes early
    ]
    df = pd.DataFrame(rows)
    op_map = {'8_1': 0}
    merged = pd.DataFrame([_merged_ops_row('8', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    # latest planned end = 09:00, latest actual end = 09:36 (the late lot) ->
    # 36 min over a 60-min mean lot duration = 60% -- delayed, despite the
    # other lot finishing early.
    assert labels['delay'][0].item() == 1.0


def test_an_early_finishing_lot_does_not_mask_the_operation_completing_late():
    # Same shape as above from the other side: even the EARLIEST-planned-end
    # lot being on time doesn't matter if a later-finishing lot overruns.
    rows = [
        _row('10', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:00'),   # exactly on time
        _row('10', '1', '2026-01-01 09:00', '2026-01-01 10:00',
             '2026-01-01 09:00', '2026-01-01 10:40'),   # +40 min, finishes last
    ]
    df = pd.DataFrame(rows)
    op_map = {'10_1': 0}
    merged = pd.DataFrame([_merged_ops_row('10', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    # latest planned end = 10:00, latest actual end = 10:40 -> 40 min over a
    # 60-min mean lot duration = 67% -- delayed.
    assert labels['delay'][0].item() == 1.0


def test_priority_head_defaults_to_neutral_when_no_deadline_data():
    # _merged_ops_row carries no order_deadline -- Section 0H.10 re-grounded
    # Head 3 in percentile-ranked deadline slack (see tests/
    # test_priority_label.py for the full behaviour), and the "no evidence"
    # fallback for that is a neutral 0.5, not the old formula of routing
    # position alone. Confirms the Head 2 fix immediately below shares this
    # fixture without side effects.
    df = pd.DataFrame([_row('9', '1', '2026-01-01 08:00', '2026-01-01 09:40',
                             '2026-01-01 08:00', '2026-01-01 09:40')])
    op_map = {'9_1': 0}
    merged = pd.DataFrame([_merged_ops_row('9', '1', seq=2)])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['priority'][0].item() == pytest.approx(0.5)


def test_completion_head_now_reads_the_unfiltered_planned_span_not_merged_ops():
    # This is the Section 0H.4 fix itself: Head 2 used to read planned_start/
    # end straight off merged_ops (which, via graph/node_features.py's
    # agg_schedule, is silently sourced from a Done-filtered query). Now it
    # reads from df_actual_vs_planned (unfiltered) instead -- confirmed here
    # by making the two sources DISAGREE (100 min vs merged_ops's 60 min) and
    # checking the unfiltered one wins.
    df = pd.DataFrame([_row('9', '1', '2026-01-01 08:00', '2026-01-01 09:40',
                             '2026-01-01 08:00', '2026-01-01 09:40')])
    op_map = {'9_1': 0}
    merged = pd.DataFrame([_merged_ops_row('9', '1', seq=2)])  # planned 08:00-09:00 = 60 min
    labels, _ = extract_gnn_labels(merged, op_map, df)
    assert labels['completion'][0].item() == pytest.approx(100.0)


def test_completion_head_spans_all_lots_not_just_the_non_done_ones():
    # The exact failure mode measured against the real database: a
    # partially-done operation's TRUE span (across every lot) must be used,
    # not a subset. Simulated here with three lots at different times --
    # _aggregate_planned_span has no execution_status column to filter on at
    # all, so it structurally cannot reproduce that leak.
    rows = [
        _row('11', '1', '2026-01-01 08:00', '2026-01-01 08:30',
             '2026-01-01 08:00', '2026-01-01 08:30'),   # earliest lot
        _row('11', '1', '2026-01-01 08:30', '2026-01-01 09:00',
             '2026-01-01 08:30', '2026-01-01 09:00'),
        _row('11', '1', '2026-01-01 09:00', '2026-01-01 10:00',
             '2026-01-01 09:00', '2026-01-01 10:00'),   # latest lot
    ]
    df = pd.DataFrame(rows)
    op_map = {'11_1': 0}
    merged = pd.DataFrame([_merged_ops_row('11', '1')])
    labels, _ = extract_gnn_labels(merged, op_map, df)
    # true span: 08:00 -> 10:00 = 120 min, not e.g. 08:30 -> 10:00 = 90 min
    # (what excluding the earliest lot -- the "Done" one in the real bug --
    # would have produced).
    assert labels['completion'][0].item() == pytest.approx(120.0)


def test_completion_head_masks_operations_with_no_planned_span():
    # Batch-4 fix 10c: no planned span means no elapsed-SPAN label. It used
    # to receive sam x quantity -- work content, a different physical
    # quantity that omits inter-lot queueing. Masked out instead.
    op_map = {'12_1': 0}
    row = _merged_ops_row('12', '1')  # sam=30
    row['target_quantity'] = 200
    merged = pd.DataFrame([row])
    labels, meta = extract_gnn_labels(merged, op_map, None)
    assert bool(labels['completion_valid'][0]) is False
    assert meta['completion_fallback_count'] == 1
    assert meta['completion_valid_count'] == 0


def test_completion_mask_does_not_depend_on_sam_being_present():
    # Previously a missing sam produced a 0-minute completion target. With
    # masking there is no substituted value at all, so the degenerate case
    # cannot arise regardless of what sam holds.
    op_map = {'13_1': 0}
    row = _merged_ops_row('13', '1')
    row['sam'] = 0
    merged = pd.DataFrame([row])
    labels, _meta = extract_gnn_labels(merged, op_map, None)
    assert bool(labels['completion_valid'][0]) is False
