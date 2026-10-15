"""
The delay fitness term supplies no schedule-discriminating signal.

ga/fitness.py accumulates  total_delay_penalty += delay_prob * 50.0  once per
operation, then divides by the operation count. That is the mean predicted
delay probability times a constant: it depends on WHICH operations are in the
problem, and on nothing the GA decides -- not sequence, not line assignment,
not machine count, not timing. For a fixed operation set it is identical for
every candidate chromosome, so it cannot change any tournament outcome.

The second channel by which the delay prediction could influence a schedule --
the risk buffer, which pads durations for risky operations -- is currently
inert too: RISK_BUFFER_AT_CERTAINTY is 0.0 (ga/schedule_utils.py), set there
deliberately after a multiseed ablation. duration * (1 + p*0.0) == duration.

Taken together the GNN delay head has no effect on GA scheduling at present.
These tests pin that so it is a measured, documented property rather than a
surprise, and so re-enabling the buffer visibly breaks the invariance test.
"""

import random

import pytest

from ga.fitness import GAMMA, evaluate_schedule
from ga.schedule_utils import (RISK_BUFFER_AT_CERTAINTY,
                               RISK_BUFFER_GATE_THRESHOLD, apply_risk_buffer)


OP_KEYS = ['1_1', '1_2', '2_1', '2_2', '3_1', '3_2']


def _problem():
    op_details = {k: {'sam': 15.0 + 3 * i, 'target_quantity': 40 + 5 * i,
                      'eligible_lines': [1, 2], 'production_order_id': k.split('_')[0]}
                  for i, k in enumerate(OP_KEYS)}
    rng = random.Random(4)
    gnn = {k: {'delay': rng.random(), 'priority': rng.random()} for k in OP_KEYS}
    orders = sorted({k.split('_')[0] for k in OP_KEYS})
    return op_details, gnn, dict(
        all_line_ids=[1, 2],
        order_deadlines={o: 20000.0 for o in orders},
        order_release_minutes={o: 0.0 for o in orders},
        machine_pools={k: {1: [1, 2, 3], 2: [4, 5, 6]} for k in OP_KEYS},
        machine_line_map={1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2},
        usable_machine_count=6, present_operator_count=30,
        op_sequence_map={k: i for i, k in enumerate(OP_KEYS)},
        return_details=True)


# Each order's own operations must stay in routing order or the schedule is
# rejected as precedence-violating and never scored. These three differ in how
# the ORDERS interleave, plus lines and machine counts -- genuinely different
# schedules, all feasible.
SEQ_A = ['1_1', '1_2', '2_1', '2_2', '3_1', '3_2']
SEQ_B = ['3_1', '3_2', '2_1', '2_2', '1_1', '1_2']
SEQ_C = ['1_1', '2_1', '3_1', '1_2', '2_2', '3_2']


def _score(order_seq, lines, counts):
    op_details, gnn, kw = _problem()
    individual = list(order_seq) + list(lines) + list(counts)
    return evaluate_schedule(individual, len(OP_KEYS), op_details, gnn, **kw)[1]


def test_delay_term_is_identical_across_completely_different_schedules():
    # Same operation set, wildly different sequence / lines / machine counts.
    # Every other component moves; the delay term must not.
    a = _score(SEQ_A, [1] * 6, [1] * 6)
    b = _score(SEQ_B, [2] * 6, [3] * 6)
    c = _score(SEQ_C, [1, 2, 1, 2, 1, 2], [2, 1, 3, 1, 2, 3])
    for x in (a, b, c):
        assert 'fitness_components' in x, f"schedule rejected: {x.get('invalid')}"
    terms = [x['fitness_components']['delay_penalty'] for x in (a, b, c)]
    assert terms[0] == pytest.approx(terms[1], rel=1e-12)
    assert terms[0] == pytest.approx(terms[2], rel=1e-12)
    # ...while the schedules genuinely differ somewhere else.
    assert len({round(x['fitness'], 9) for x in (a, b, c)}) > 1


def test_delay_term_equals_gamma_times_mean_delay_probability_times_fifty():
    op_details, gnn, _ = _problem()
    expected = GAMMA * (sum(v['delay'] * 50.0 for v in gnn.values()) / len(OP_KEYS))
    det = _score(SEQ_A, [1] * 6, [2] * 6)
    assert det['fitness_components']['delay_penalty'] == pytest.approx(expected, rel=1e-12)


def test_risk_buffer_is_currently_a_no_op_at_any_probability():
    # The other channel through which a delay prediction could reach the
    # schedule. At RISK_BUFFER_AT_CERTAINTY = 0.0 it returns the duration
    # unchanged even for a certain-delay operation above the gate.
    assert RISK_BUFFER_AT_CERTAINTY == 0.0
    for p in (0.0, RISK_BUFFER_GATE_THRESHOLD, 0.5, 1.0):
        assert apply_risk_buffer(120.0, p) == pytest.approx(120.0)


def test_changing_every_delay_prediction_does_not_change_the_schedule():
    # The decisive end-to-end check: hold the operation set and the chromosome
    # fixed, replace every delay probability, and confirm nothing about the
    # SCHEDULE moves -- makespan, lateness and load balance are untouched.
    # Only the constant delay term shifts, by the same amount for every
    # candidate, which cannot reorder any tournament.
    op_details, gnn, kw = _problem()
    individual = list(SEQ_A) + [1, 2, 1, 2, 1, 2] + [2] * 6
    low = {k: {'delay': 0.01, 'priority': v['priority']} for k, v in gnn.items()}
    high = {k: {'delay': 0.99, 'priority': v['priority']} for k, v in gnn.items()}
    a = evaluate_schedule(individual, len(OP_KEYS), op_details, low, **kw)[1]
    b = evaluate_schedule(individual, len(OP_KEYS), op_details, high, **kw)[1]
    for key in ('makespan', 'load_balance', 'lateness', 'idle_labour'):
        assert a['fitness_components'][key] == pytest.approx(
            b['fitness_components'][key], rel=1e-12), key
    assert a['makespan_minutes'] == pytest.approx(b['makespan_minutes'], rel=1e-12)
    # The delay term itself is the only thing that moved.
    assert a['fitness_components']['delay_penalty'] != pytest.approx(
        b['fitness_components']['delay_penalty'])


def test_removing_the_delay_term_would_not_change_any_ranking():
    # Directly demonstrates the interpretability claim: subtracting the term
    # from every candidate preserves the complete ordering.
    scores = [_score(SEQ_A, [1] * 6, [1] * 6),
              _score(SEQ_B, [2] * 6, [3] * 6),
              _score(SEQ_C, [1, 2, 1, 2, 1, 2], [2] * 6)]
    for s_ in scores:
        assert 'fitness_components' in s_, f"schedule rejected: {s_.get('invalid')}"
    with_term = [s['fitness'] for s in scores]
    without = [s['fitness'] - s['fitness_components']['delay_penalty'] for s in scores]
    assert sorted(range(len(with_term)), key=lambda i: with_term[i]) == \
           sorted(range(len(without)), key=lambda i: without[i])
