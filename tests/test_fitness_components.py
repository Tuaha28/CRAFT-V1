"""
The fitness breakdown must reconstruct the fitness the GA actually optimised.

A breakdown that does not sum back to `fitness` is worse than no breakdown:
it looks authoritative in a results table while describing a different
objective than the one the search minimised. These tests pin the identity.
"""

import random

import pytest

from ga.fitness import (
    ALPHA, GAMMA, DELTA, EPSILON, ZETA, SERVICE_LEVEL_WEIGHT,
    evaluate_schedule,
)


def _problem(num_ops=10, seed=7):
    rng = random.Random(seed)
    op_keys = [f"{100 + (i // 5)}_{i}" for i in range(num_ops)]
    op_details = {
        k: {'sam': 20.0 + 5 * i, 'target_quantity': 50 + 10 * i,
            'eligible_lines': [1, 2], 'production_order_id': k.split('_')[0]}
        for i, k in enumerate(op_keys)
    }
    gnn = {k: {'delay': rng.random(), 'priority': rng.random()} for k in op_keys}
    individual = (list(op_keys)
                  + [rng.randint(1, 2) for _ in op_keys]
                  + [rng.randint(1, 4) for _ in op_keys])
    orders = sorted({k.split('_')[0] for k in op_keys})
    return {
        'individual': individual,
        'num_ops': num_ops,
        'op_details': op_details,
        'gnn_predictions': gnn,
        'all_line_ids': [1, 2],
        'order_deadlines': {o: 5000.0 for o in orders},
        'order_release_minutes': {o: 0.0 for o in orders},
        'machine_pools': {k: {1: [1, 2, 3], 2: [4, 5, 6]} for k in op_keys},
        'machine_line_map': {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2},
        'usable_machine_count': 6,
        'present_operator_count': 40,
        'op_sequence_map': {k: i for i, k in enumerate(op_keys)},
    }


def _score(**overrides):
    p = _problem()
    p.update(overrides)
    individual = p.pop('individual')
    num_ops = p.pop('num_ops')
    op_details = p.pop('op_details')
    gnn = p.pop('gnn_predictions')
    return evaluate_schedule(individual, num_ops, op_details, gnn,
                             return_details=True, **p)


def test_components_sum_to_the_fitness_the_ga_minimised():
    (fit,), det = _score()
    total = sum(det['fitness_components'].values())
    assert total == pytest.approx(det['fitness'], rel=1e-9)
    assert total == pytest.approx(fit, rel=1e-9)


def test_every_weighted_term_is_present():
    _, det = _score()
    assert set(det['fitness_components']) == {
        'makespan', 'delay_penalty', 'priority_reward',
        'load_balance', 'lateness', 'idle_labour'}


def test_priority_reward_is_negative_because_it_is_subtracted():
    # It is a REWARD in a minimisation objective: it must lower the score.
    # Reporting it unsigned would make the breakdown fail to sum, and would
    # read in a table as though priority were a penalty.
    _, det = _score()
    assert det['fitness_components']['priority_reward'] <= 0


def test_lateness_split_sums_to_the_lateness_term():
    _, det = _score()
    split = det['fitness_lateness_split']
    assert (split['avg_lateness_hours_term'] + split['service_level_term']
            == pytest.approx(det['fitness_components']['lateness'], rel=1e-9))


def test_service_level_term_tracks_the_fraction_of_late_orders():
    # Deadline in the past for every order -> every order late -> the service
    # level term must be exactly its full weight.
    _, det = _score(order_deadlines={o: 0.0 for o in ('100', '101')})
    assert det['orders_late'] == det['orders_with_deadline']
    assert det['fitness_lateness_split']['service_level_term'] == pytest.approx(
        SERVICE_LEVEL_WEIGHT)


def test_breakdown_still_sums_when_nothing_is_late():
    # Deadline far enough out that no order is tardy -- the lateness term
    # goes to zero and the identity must still hold.
    _, det = _score(order_deadlines={o: 10_000_000.0 for o in ('100', '101')})
    assert det['orders_late'] == 0
    assert det['fitness_components']['lateness'] == pytest.approx(0.0)
    assert sum(det['fitness_components'].values()) == pytest.approx(det['fitness'], rel=1e-9)


def test_makespan_term_uses_scored_makespan_not_base_makespan():
    # These two differ whenever the committed subset is smaller than the
    # lookahead. The objective is built from scored_makespan; a breakdown
    # built from makespan_minutes would silently disagree with the fitness.
    _, det = _score()
    assert det['fitness_components']['makespan'] == pytest.approx(
        ALPHA * det['scored_makespan_minutes'] / 60.0, rel=1e-9)


def test_load_balance_term_matches_the_reported_stdev():
    _, det = _score()
    assert det['fitness_components']['load_balance'] == pytest.approx(
        EPSILON * det['load_balance_stdev_minutes'] / 60.0, rel=1e-9)
