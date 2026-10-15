"""
Regression tests for PROJECT_STATUS.txt Section 0H.11: the urgency-tiered
staged-release sequencing heuristic.

Four things are tested:
  1. _critical_ratio -- the formula shared with sequence_critical_ratio
     (factored out so the two can't drift apart), and a regression check
     that sequence_critical_ratio's own behaviour is unchanged by that
     refactor.
  2. _tier_orders_by_critical_ratio -- percentile-rank tiering, and the
     spread-guard fallback (fewer than 2 finite ratios, or a relative IQR
     below MIN_RELATIVE_IQR_FOR_TIERING) that stops percentile ranking from
     manufacturing artificial tiers out of a genuinely uniform window.
  3. sequence_urgency_tiered's actual point: a HARD wave that
     sequence_critical_ratio's continuous blend does not enforce -- jitter
     can never move a lower-tier order ahead of a higher-tier one, even
     when the same jitter magnitude demonstrably CAN swap two close-ratio
     orders under the plain continuous sort.
  4. baseline_chromosomes() wiring -- 'UrgencyTiered' lands in the same
     table CriticalRatio already does, unmodified, since that head-to-head
     is the decisive validation this heuristic must clear.
"""
import random

import pandas as pd
import pytest

from ga.chromosome import ChromosomeFactory
from ga.heuristics import (
    _critical_ratio,
    _tier_orders_by_critical_ratio,
    sequence_critical_ratio,
    sequence_urgency_tiered,
    baseline_chromosomes,
    MIN_RELATIVE_IQR_FOR_TIERING,
)


def _grouped(order_ops):
    """order_ops: {order_id: [op_key, ...]} already in routing order."""
    return dict(order_ops)


def _op_details(work_by_key):
    return {k: {'serial_processing_mins': w} for k, w in work_by_key.items()}


# ---------------------------------------------------------------------------
# _critical_ratio
# ---------------------------------------------------------------------------

def test_ratio_is_deadline_over_total_work():
    grouped = _grouped({'A': ['A_1', 'A_2']})
    op_details = _op_details({'A_1': 30.0, 'A_2': 70.0})  # 100 total
    assert _critical_ratio('A', grouped, {'A': 500.0}, op_details) == pytest.approx(5.0)


def test_ratio_is_inf_when_deadline_missing():
    grouped = _grouped({'A': ['A_1']})
    op_details = _op_details({'A_1': 30.0})
    assert _critical_ratio('A', grouped, {}, op_details) == float('inf')


def test_ratio_is_inf_when_work_is_zero_or_negative():
    grouped = _grouped({'A': ['A_1']})
    op_details = _op_details({'A_1': 0.0})
    assert _critical_ratio('A', grouped, {'A': 100.0}, op_details) == float('inf')


def test_sequence_critical_ratio_still_sorts_ascending_by_ratio():
    # Regression check: factoring the formula out into _critical_ratio must
    # not change sequence_critical_ratio's own behaviour at all.
    grouped = _grouped({'A': ['A_1'], 'B': ['B_1'], 'C': ['C_1']})
    op_details = _op_details({'A_1': 10.0, 'B_1': 10.0, 'C_1': 10.0})
    deadlines = {'A': 300.0, 'B': 100.0, 'C': 200.0}  # ratios: A=30, B=10, C=20
    assert sequence_critical_ratio(grouped, deadlines, op_details) == ['B_1', 'C_1', 'A_1']


def test_sequence_critical_ratio_sorts_unknown_deadline_last():
    grouped = _grouped({'A': ['A_1'], 'B': ['B_1']})
    op_details = _op_details({'A_1': 10.0, 'B_1': 10.0})
    deadlines = {'A': 100.0}  # B has no deadline
    assert sequence_critical_ratio(grouped, deadlines, op_details) == ['A_1', 'B_1']


# ---------------------------------------------------------------------------
# _tier_orders_by_critical_ratio
# ---------------------------------------------------------------------------

def test_fewer_than_two_finite_ratios_falls_back():
    grouped = _grouped({'A': ['A_1'], 'B': ['B_1']})
    op_details = _op_details({'A_1': 10.0, 'B_1': 10.0})
    deadlines = {'A': 100.0}  # only A has a finite ratio
    tiers, ratios, fell_back = _tier_orders_by_critical_ratio(grouped, deadlines, op_details, num_tiers=4)
    assert fell_back is True
    assert tiers == {}
    assert ratios['B'] == float('inf')


def test_narrow_spread_falls_back():
    # Ratios all within a tight band (~10.0-10.15) -- relative IQR well
    # under MIN_RELATIVE_IQR_FOR_TIERING. Real-world analogue: this
    # dataset's orders all carry 21-75 days of slack (Section 0H.10), so a
    # window can easily contain nothing genuinely more urgent than anything
    # else.
    grouped = _grouped({o: [f'{o}_1'] for o in ['A', 'B', 'C', 'D']})
    op_details = _op_details({f'{o}_1': 100.0 for o in ['A', 'B', 'C', 'D']})
    deadlines = {'A': 1000.0, 'B': 1005.0, 'C': 1010.0, 'D': 1015.0}
    tiers, ratios, fell_back = _tier_orders_by_critical_ratio(grouped, deadlines, op_details, num_tiers=4)
    assert fell_back is True
    assert tiers == {}


def test_wide_spread_tiers_by_percentile_least_ratio_is_tier_zero():
    grouped = _grouped({o: [f'{o}_1'] for o in ['A', 'B', 'C', 'D']})
    op_details = _op_details({f'{o}_1': 10.0 for o in ['A', 'B', 'C', 'D']})
    deadlines = {'A': 10.0, 'B': 50.0, 'C': 100.0, 'D': 200.0}  # ratios: 1, 5, 10, 20
    tiers, ratios, fell_back = _tier_orders_by_critical_ratio(grouped, deadlines, op_details, num_tiers=4)
    assert fell_back is False
    assert tiers['A'] == 0                 # least ratio -> most urgent -> tier 0
    assert tiers['D'] == 3                 # greatest ratio -> tier num_tiers-1
    assert tiers['A'] < tiers['B'] < tiers['C'] < tiers['D']


def test_relative_iqr_threshold_is_the_documented_default():
    assert MIN_RELATIVE_IQR_FOR_TIERING == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# sequence_urgency_tiered -- the hard-wave property itself
# ---------------------------------------------------------------------------

def test_jitter_can_never_cross_a_tier_boundary_unlike_continuous_sort():
    # B and C have close ratios (5.0 vs 6.0, deliberately wide enough to
    # clear the spread guard) -- a large jitter (0.5) makes it plausible
    # for a CONTINUOUS sort to occasionally swap them, since jitter
    # perturbs the ratio directly. Tiering must never allow that swap,
    # regardless of the rng draw, because tier is the PRIMARY sort key and
    # jitter only ever touches the ratio SECONDARY key within a tier.
    grouped = _grouped({'B': ['B_1'], 'C': ['C_1']})
    op_details = _op_details({'B_1': 10.0, 'C_1': 10.0})
    deadlines = {'B': 50.0, 'C': 60.0}  # ratios: B=5.0, C=6.0

    continuous_swaps = 0
    tiered_swaps = 0
    for seed in range(200):
        continuous_seq = sequence_critical_ratio(
            grouped, deadlines, op_details, random.Random(seed), jitter=0.5)
        if continuous_seq.index('C_1') < continuous_seq.index('B_1'):
            continuous_swaps += 1

        tiered_seq = sequence_urgency_tiered(
            grouped, deadlines, op_details, random.Random(seed), jitter=0.5, num_tiers=2)
        if tiered_seq.index('C_1') < tiered_seq.index('B_1'):
            tiered_swaps += 1

    assert continuous_swaps > 0   # confirms the test setup can actually trigger a swap at all
    assert tiered_swaps == 0      # the hard-wave guarantee this heuristic exists to provide


def test_hard_wave_keeps_a_whole_tiers_operations_together():
    # Multi-operation orders: every operation of a tier-0 order must precede
    # every operation of a tier-1 order, not just the orders' first ops.
    grouped = _grouped({
        'A': ['A_1', 'A_2'], 'B': ['B_1', 'B_2'], 'C': ['C_1'], 'D': ['D_1'],
    })
    op_details = _op_details({
        'A_1': 5.0, 'A_2': 5.0, 'B_1': 5.0, 'B_2': 5.0, 'C_1': 10.0, 'D_1': 10.0,
    })
    deadlines = {'A': 10.0, 'B': 15.0, 'C': 100.0, 'D': 200.0}  # ratios: A=1, B=1.5, C=10, D=20
    seq = sequence_urgency_tiered(grouped, deadlines, op_details, num_tiers=4)
    pos = {k: i for i, k in enumerate(seq)}
    assert max(pos['A_1'], pos['A_2']) < min(pos['C_1'], pos['D_1'])
    assert max(pos['B_1'], pos['B_2']) < min(pos['C_1'], pos['D_1'])


def test_no_deadline_or_no_work_orders_sort_dead_last():
    grouped = _grouped({'A': ['A_1'], 'B': ['B_1'], 'C': ['C_1']})
    op_details = _op_details({'A_1': 10.0, 'B_1': 10.0, 'C_1': 10.0})
    deadlines = {'A': 10.0, 'B': 100.0}  # C has no deadline at all
    seq = sequence_urgency_tiered(grouped, deadlines, op_details, num_tiers=4)
    assert seq[-1] == 'C_1'


def test_fallback_output_matches_plain_critical_ratio_sort_exactly():
    grouped = _grouped({o: [f'{o}_1'] for o in ['A', 'B', 'C', 'D']})
    op_details = _op_details({f'{o}_1': 100.0 for o in ['A', 'B', 'C', 'D']})
    deadlines = {'A': 1000.0, 'B': 1005.0, 'C': 1010.0, 'D': 1015.0}  # narrow -> falls back
    tiered = sequence_urgency_tiered(grouped, deadlines, op_details, random.Random(7), jitter=0.1)
    plain = sequence_critical_ratio(grouped, deadlines, op_details, random.Random(7), jitter=0.1)
    assert tiered == plain


def test_num_tiers_is_respected():
    grouped = _grouped({o: [f'{o}_1'] for o in ['A', 'B', 'C']})
    op_details = _op_details({f'{o}_1': 10.0 for o in ['A', 'B', 'C']})
    deadlines = {'A': 10.0, 'B': 100.0, 'C': 1000.0}
    tiers, _, fell_back = _tier_orders_by_critical_ratio(grouped, deadlines, op_details, num_tiers=2)
    assert fell_back is False
    assert set(tiers.values()) <= {0, 1}


# ---------------------------------------------------------------------------
# baseline_chromosomes() wiring -- the cheap-check integration point
# ---------------------------------------------------------------------------

def _small_factory():
    df_lines = pd.DataFrame([{'line_id': 1, 'is_active': 1}])
    df_machines = pd.DataFrame([
        {'asset_id': i, 'line_id': 1, 'operation_type': 'body assembly'} for i in range(5)
    ])
    df_routings = pd.DataFrame([
        {'production_order_id': 1, 'operation_id': 1, 'operation_sequence_number': 1,
         'required_machine_type': 'single-needle lockstitch'},
        {'production_order_id': 2, 'operation_id': 1, 'operation_sequence_number': 1,
         'required_machine_type': 'single-needle lockstitch'},
    ])
    return ChromosomeFactory(df_routings, df_machines, df_lines, legacy_mode='legacy_company_wide')


def test_baseline_chromosomes_includes_urgency_tiered_as_a_valid_permutation():
    factory = _small_factory()
    op_details = {k: {'serial_processing_mins': 30.0, 'setup_mins': 5.0} for k in factory.op_keys}
    order_deadlines = {'1': 1000.0, '2': 100.0}
    seq_lookup = {k: 1 for k in factory.op_keys}

    baselines = baseline_chromosomes(factory, op_details, order_deadlines, seq_lookup)

    assert 'UrgencyTiered' in baselines
    assert 'CriticalRatio' in baselines  # the head-to-head comparison this must clear
    num_ops = len(factory.op_keys)
    os_array = baselines['UrgencyTiered'][:num_ops]
    assert sorted(os_array) == sorted(factory.op_keys)  # still a valid permutation
