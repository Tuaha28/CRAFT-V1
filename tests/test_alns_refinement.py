"""
Legality and mechanism tests for ga/alns.py (sequential ALNS refinement,
ALNS_REFINEMENT_PROPOSAL.md) and GAScheduler.alns_refine(). Neither is
wired into optimize() by default -- opt-in only. Legality first, per
standing instruction: same synthetic-factory pattern as
tests/test_domain_aware_crossover.py, same _is_legal check every other
operator in this project is held to.
"""
import random
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ga.scheduler import GAScheduler
from ga.alns import alns_refine, DESTROY_OPERATORS, _richer_score_line

NOW = datetime(2026, 1, 1, 8, 0, 0)


def _routings():
    rows = [
        (1001, 1, 1, 2.0, 40, 20, 'single-needle lockstitch'),
        (1001, 2, 2, 1.5, 40, 20, 'overlock serger (4-thread)'),
        (1002, 1, 1, 2.5, 30, 10, 'single-needle lockstitch'),
        (1002, 2, 2, 1.0, 30, 10, 'overlock serger (4-thread)'),
        (1003, 1, 1, 1.8, 25, 15, 'single-needle lockstitch'),
        (1003, 2, 2, 1.2, 25, 15, 'overlock serger (4-thread)'),
    ]
    return pd.DataFrame([
        {
            'production_order_id': oid, 'operation_id': op_id, 'operation_sequence_number': seq,
            'sam': sam, 'target_quantity': qty, 'order_deadline': NOW + timedelta(days=days),
            'required_machine_type': mtype,
        }
        for oid, op_id, seq, sam, qty, days, mtype in rows
    ])


def _machines():
    rows = []
    for i, asset_id in enumerate(range(101, 107)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'body assembly'})
    for i, asset_id in enumerate(range(201, 207)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'seam edge overlock'})
    return pd.DataFrame(rows)


def _lines():
    return pd.DataFrame({'line_id': [1, 2], 'is_active': [1, 1], 'target_efficiency_percentage': [85.0, 85.0]})


def make_scheduler(**kwargs):
    defaults = dict(
        df_routings=_routings(), df_machines=_machines(), df_lines=_lines(),
        gnn_predictions={}, current_time=NOW, present_operator_count=10,
    )
    defaults.update(kwargs)
    return GAScheduler(**defaults, legacy_mode='legacy_company_wide')


def _is_legal(chromosome, scheduler):
    n = scheduler.num_ops
    os_arr, la, mc = chromosome[:n], chromosome[n:2 * n], chromosome[2 * n:]
    if sorted(os_arr) != sorted(scheduler.factory.op_keys):
        return False, "operations duplicated or lost"
    last_seq_per_order = {}
    for key in os_arr:
        order_id, seq = key.rsplit('_', 1)
        seq_num = scheduler.seq_lookup[key]
        if order_id in last_seq_per_order and seq_num < last_seq_per_order[order_id]:
            return False, f"precedence violated in order {order_id}"
        last_seq_per_order[order_id] = seq_num
    for i, key in enumerate(os_arr):
        if la[i] not in scheduler.factory.eligibility_map[key]:
            return False, f"{key} assigned to ineligible line {la[i]}"
        max_mc = scheduler.factory._max_machines_for(key, la[i])
        if not (1 <= mc[i] <= max_mc):
            return False, f"{key} machine count {mc[i]} outside pool (max {max_mc})"
    return True, ""


def _start_chromosome(scheduler):
    """A real, legal starting individual -- the same kind of thing
    alns_refine is meant to be applied to (a finished construction)."""
    return scheduler.toolbox.individual()


# ---------------------------------------------------------------------------
# Legality -- many trials, multiple removal fractions.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("removal_frac", [0.02, 0.035, 0.05, 0.5])
def test_alns_refine_produces_legal_chromosome(removal_frac):
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    rng = random.Random(7)
    best, fit, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=removal_frac, rng=rng,
    )
    ok, reason = _is_legal(best, scheduler)
    assert ok, reason


def test_alns_refine_never_finishes_worse_than_it_started():
    # The core guarantee (Section 3d): `best` only ever updates on strict
    # improvement, so the STAGE's output can never regress the input,
    # even though `current` explores permissively inside the search.
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    rng = random.Random(3)
    best, fit, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=80, removal_frac=0.035, rng=rng,
    )
    assert stats['final_fitness'] <= stats['start_fitness'] + 1e-9
    assert fit <= stats['start_fitness'] + 1e-9


def test_alns_refine_is_reproducible_for_the_same_seed():
    scheduler = make_scheduler()
    start = list(_start_chromosome(scheduler))

    best_a, fit_a, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(99),
    )
    best_b, fit_b, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(99),
    )
    assert best_a == best_b
    assert fit_a == fit_b


def test_alns_refine_respects_the_evaluation_budget():
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    _, _, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=50, removal_frac=0.035, rng=random.Random(1),
    )
    assert stats['evaluations'] <= 50 + 1  # the loop's own pre-increment tolerance


def test_alns_refine_weight_history_covers_all_three_operators():
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    _, _, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=100, removal_frac=0.035, rng=random.Random(5), segment_length=10,
    )
    assert len(stats['weight_history']) > 0
    for _evals, weights in stats['weight_history']:
        assert set(weights.keys()) == set(DESTROY_OPERATORS)
        assert all(w > 0 for w in weights.values())  # floor keeps every operator selectable


def test_alns_refine_handles_a_single_operation_edge_case():
    scheduler = make_scheduler(df_routings=_routings()[_routings()['production_order_id'] == 1001].head(1))
    start = _start_chromosome(scheduler)
    best, fit, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=20, removal_frac=0.035, rng=random.Random(2),
    )
    ok, reason = _is_legal(best, scheduler)
    assert ok, reason


# ---------------------------------------------------------------------------
# GAScheduler.alns_refine() wrapper -- the actual entry point scripts use.
# ---------------------------------------------------------------------------

def test_scheduler_alns_refine_returns_a_legal_scored_individual():
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    refined, stats = scheduler.alns_refine(start, max_evals=50, removal_frac=0.035, random_seed=4)
    ok, reason = _is_legal(list(refined), scheduler)
    assert ok, reason
    assert refined.fitness.values[0] == pytest.approx(stats['final_fitness'])


def test_alns_refine_reports_a_measured_calibration_t0():
    scheduler = make_scheduler()
    start = _start_chromosome(scheduler)
    _, _, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=100, removal_frac=0.035, rng=random.Random(8),
    )
    assert stats['calibration_t0'] > 0
    assert stats['calibration_samples'] > 0


def test_alns_refine_calibration_is_reproducible_for_the_same_seed():
    scheduler = make_scheduler()
    start = list(_start_chromosome(scheduler))
    _, _, stats_a = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=80, removal_frac=0.035, rng=random.Random(21),
    )
    _, _, stats_b = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=80, removal_frac=0.035, rng=random.Random(21),
    )
    assert stats_a['calibration_t0'] == stats_b['calibration_t0']


def test_scheduler_alns_refine_is_reproducible_for_the_same_seed():
    scheduler_a = make_scheduler()
    scheduler_b = make_scheduler()
    start = _start_chromosome(scheduler_a)
    refined_a, stats_a = scheduler_a.alns_refine(list(start), max_evals=50, removal_frac=0.035, random_seed=17)
    refined_b, stats_b = scheduler_b.alns_refine(list(start), max_evals=50, removal_frac=0.035, random_seed=17)
    assert list(refined_a) == list(refined_b)
    assert refined_a.fitness.values == refined_b.fitness.values


# ---------------------------------------------------------------------------
# Richer repair-line scoring (ALNS_REPAIR_SCORING_PROPOSAL.md Section 13) --
# opt-in via repair_score_weights, None by default (unchanged behaviour,
# covered by every test above). These tests exercise the opt-in path.
# ---------------------------------------------------------------------------

class _FakeFactory:
    """Minimal stand-in exposing only what _richer_score_line reads."""
    def __init__(self, machine_ids_per_line_map):
        self.machine_ids_per_line_map = machine_ids_per_line_map


def test_richer_score_line_flips_the_pick_for_a_realistic_non_degenerate_case():
    """
    Regression test encoding the exact algebraic proof in
    ALNS_REPAIR_SCORING_PROPOSAL.md Section 12: op_work=32, line A has 2
    machines (existing load 30/machine), line B has 8 machines (existing
    load 35/machine). Plain load-only rule picks A (30 < 35). The richer
    score's marginal-load interaction term (w_impact=0 to isolate it, per
    the proof) flips the pick to B, because placing this operation on A's
    small pool would spike its per-machine load far more than on B.
    """
    op_key = "op1"
    factory = _FakeFactory({op_key: {"A": [1, 2], "B": list(range(8))}})
    eligible = ["A", "B"]
    pool_loads = {("A", "T"): 30.0, ("B", "T"): 35.0}
    op_work = {op_key: 32.0}
    delay_prob = 0.8

    plain_choice = min(eligible, key=lambda ln: pool_loads.get((ln, "T"), 0.0))
    assert plain_choice == "A"

    richer_choice = _richer_score_line(
        eligible, pool_loads, "T", op_key, delay_prob, impact_counts={},
        op_work=op_work, factory=factory, weights=(1.0, 1.0, 0.0),
    )
    assert richer_choice == "B"


@pytest.mark.parametrize("removal_frac", [0.02, 0.035, 0.05, 0.5])
def test_alns_refine_with_richer_repair_score_produces_legal_chromosome(removal_frac):
    scheduler = make_scheduler(gnn_predictions={
        '1001_1': {'delay': 0.7}, '1001_2': {'delay': 0.3},
        '1002_1': {'delay': 0.6}, '1002_2': {'delay': 0.2},
        '1003_1': {'delay': 0.5}, '1003_2': {'delay': 0.4},
    })
    start = _start_chromosome(scheduler)
    rng = random.Random(7)
    best, fit, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=removal_frac, rng=rng,
        repair_score_weights=(1.0, 1.0, 0.3),
    )
    ok, reason = _is_legal(best, scheduler)
    assert ok, reason


def test_alns_refine_with_richer_repair_score_never_finishes_worse_than_it_started():
    scheduler = make_scheduler(gnn_predictions={
        '1001_1': {'delay': 0.7}, '1001_2': {'delay': 0.3},
        '1002_1': {'delay': 0.6}, '1002_2': {'delay': 0.2},
        '1003_1': {'delay': 0.5}, '1003_2': {'delay': 0.4},
    })
    start = _start_chromosome(scheduler)
    rng = random.Random(3)
    best, fit, stats = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=80, removal_frac=0.035, rng=rng,
        repair_score_weights=(1.0, 1.0, 0.3),
    )
    assert stats['final_fitness'] <= stats['start_fitness'] + 1e-9
    assert fit <= stats['start_fitness'] + 1e-9


def test_alns_refine_with_richer_repair_score_is_reproducible_for_the_same_seed():
    scheduler = make_scheduler(gnn_predictions={
        '1001_1': {'delay': 0.7}, '1001_2': {'delay': 0.3},
        '1002_1': {'delay': 0.6}, '1002_2': {'delay': 0.2},
        '1003_1': {'delay': 0.5}, '1003_2': {'delay': 0.4},
    })
    start = list(_start_chromosome(scheduler))

    best_a, fit_a, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(99),
        repair_score_weights=(1.0, 1.0, 0.3),
    )
    best_b, fit_b, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(99),
        repair_score_weights=(1.0, 1.0, 0.3),
    )
    assert best_a == best_b
    assert fit_a == fit_b


def test_scheduler_alns_refine_with_richer_repair_score_returns_a_legal_scored_individual():
    scheduler = make_scheduler(gnn_predictions={
        '1001_1': {'delay': 0.7}, '1001_2': {'delay': 0.3},
        '1002_1': {'delay': 0.6}, '1002_2': {'delay': 0.2},
        '1003_1': {'delay': 0.5}, '1003_2': {'delay': 0.4},
    })
    start = _start_chromosome(scheduler)
    refined, stats = scheduler.alns_refine(start, max_evals=50, removal_frac=0.035, random_seed=4,
                                            repair_score_weights=(1.0, 1.0, 0.3))
    ok, reason = _is_legal(list(refined), scheduler)
    assert ok, reason
    assert refined.fitness.values[0] == pytest.approx(stats['final_fitness'])


def test_alns_refine_default_behaviour_unchanged_when_repair_score_weights_is_none():
    """repair_score_weights=None (the default) must produce byte-identical
    output to a call that omits the parameter entirely -- the opt-in must
    have zero effect unless explicitly requested."""
    scheduler = make_scheduler()
    start = list(_start_chromosome(scheduler))

    best_default, fit_default, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(11),
    )
    best_explicit, fit_explicit, _ = alns_refine(
        list(start), scheduler.num_ops, scheduler.factory, scheduler.op_work,
        scheduler.toolbox.evaluate, scheduler.gnn_predictions,
        max_evals=60, removal_frac=0.035, rng=random.Random(11),
        repair_score_weights=None,
    )
    assert best_default == best_explicit
    assert fit_default == fit_explicit
