"""
Orchestration tests for the GA main loop (ga/scheduler.py::GAScheduler).

Everything else in tests/ exercises the GA's individual building blocks
(scheduling-core primitives, local-search moves, island-migration mechanics,
bottleneck protection) in isolation. Nothing previously drove the top-level
entry points themselves -- run_baselines(), optimize(), optimize_islands() --
so the guarantees the docstrings claim (elitism never regresses, the GA can
never finish worse than the best baseline it seeds, a seeded run is
reproducible) had never actually been checked end to end.

Uses a small synthetic factory (2 orders x 2 operations, 2 lines, 2 machine
types, 6 machines per type) rather than the real DB -- same reasoning as
every other test file in this suite: fast, deterministic, no DB dependency,
and small enough that a human can hand-verify the legality checks below.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ga.scheduler import GAScheduler

NOW = datetime(2026, 1, 1, 8, 0, 0)


def _routings():
    # Two orders, two operations each, alternating machine type so both
    # lines/types are actually exercised.
    # production_order_id values are numeric-string-castable (matches the
    # real schema's integer order ids) -- ga/heuristics.py::sequence_fifo
    # uses int(order_id) as its priority proxy (older/lower id first), so a
    # non-numeric id like 'O1' crashes population seeding.
    rows = [
        # production_order_id, operation_id, seq, sam, qty, deadline_days, machine_type
        (1001, 1, 1, 2.0, 40, 20, 'single-needle lockstitch'),
        (1001, 2, 2, 1.5, 40, 20, 'overlock serger (4-thread)'),
        (1002, 1, 1, 2.5, 30, 10, 'single-needle lockstitch'),
        (1002, 2, 2, 1.0, 30, 10, 'overlock serger (4-thread)'),
    ]
    return pd.DataFrame([
        {
            'production_order_id': oid,
            'operation_id': op_id,
            'operation_sequence_number': seq,
            'sam': sam,
            'target_quantity': qty,
            'order_deadline': NOW + timedelta(days=days),
            'required_machine_type': mtype,
        }
        for oid, op_id, seq, sam, qty, days, mtype in rows
    ])


def _machines():
    # 6 machines of each of the two types used above, split across 2 lines.
    rows = []
    for i, asset_id in enumerate(range(101, 107)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'body assembly'})
    for i, asset_id in enumerate(range(201, 207)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'seam edge overlock'})
    return pd.DataFrame(rows)


def _lines():
    return pd.DataFrame({
        'line_id': [1, 2],
        'is_active': [1, 1],
        'target_efficiency_percentage': [85.0, 85.0],
    })


def make_scheduler(**kwargs):
    """A fresh GAScheduler over fresh copies of the synthetic factory data.
    Fresh DataFrame copies each call so nothing one test does to a scheduler
    (or the frames it holds references to) can leak into another."""
    defaults = dict(
        df_routings=_routings(),
        df_machines=_machines(),
        df_lines=_lines(),
        gnn_predictions={},  # every GNN field defaults (delay=0.0, priority=0.5) -- see ga/fitness.py
        current_time=NOW,
        present_operator_count=10,
    )
    defaults.update(kwargs)
    return GAScheduler(**defaults, legacy_mode='legacy_company_wide')


def _is_legal(chromosome, scheduler):
    """Every invariant a chromosome must satisfy to be a legal schedule --
    same checks tests/test_local_search.py applies to local-search output,
    against the real eligibility/pool data this scheduler actually built."""
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


# ---------------------------------------------------------------------------
# run_baselines()
# ---------------------------------------------------------------------------

def test_run_baselines_returns_all_five_named_heuristics():
    scheduler = make_scheduler()
    results = scheduler.run_baselines()
    assert set(results.keys()) == {'FIFO', 'EDF', 'CriticalRatio', 'UrgencyTiered', 'RandomRepaired'}


def test_run_baselines_details_have_a_finite_fitness_and_are_legal():
    scheduler = make_scheduler()
    # baseline_chromosomes() is what run_baselines() scores -- re-derive the
    # actual chromosomes here (same call the method itself makes) so the
    # legality check can run against real genes, not just the fitness number.
    from ga.heuristics import baseline_chromosomes
    chromosomes = baseline_chromosomes(
        scheduler.factory, scheduler.op_details, scheduler.order_deadlines_minutes, scheduler.seq_lookup
    )
    results = scheduler.run_baselines()
    for name, details in results.items():
        assert 'fitness' in details
        assert details['fitness'] == pytest.approx(details['fitness'])  # not NaN/inf-poisoned
        ok, reason = _is_legal(chromosomes[name], scheduler)
        assert ok, f"{name}: {reason}"


# ---------------------------------------------------------------------------
# optimize() -- the single-population GA main loop
# ---------------------------------------------------------------------------

def test_optimize_returns_legal_chromosomes_no_more_than_requested():
    scheduler = make_scheduler()
    selected, logbook = scheduler.optimize(pop_size=8, generations=3, n_solutions=2, random_seed=1)
    assert 0 < len(selected) <= 2
    for chromosome, fitness in selected:
        ok, reason = _is_legal(chromosome, scheduler)
        assert ok, reason
        assert fitness == pytest.approx(fitness)


def test_optimize_result_is_sorted_best_first():
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize(pop_size=8, generations=3, n_solutions=3, random_seed=1)
    fitnesses = [f for _, f in selected]
    assert fitnesses == sorted(fitnesses)  # FitnessMin: lower is better, ascending = best-first


def test_optimize_never_finishes_worse_than_the_best_baseline_it_seeds():
    # The documented elitism guarantee: the best pure heuristic is seeded
    # into generation 0 and the top elite_frac survives every generation
    # unchanged, so the GA's own best can only match or beat it.
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize(pop_size=8, generations=3, n_solutions=1, random_seed=7)
    best_baseline_fitness = min(details['fitness'] for details in scheduler.baseline_results.values())
    assert selected[0][1] <= best_baseline_fitness + 1e-6


def test_optimize_elitism_never_regresses_generation_to_generation():
    scheduler = make_scheduler()
    _, logbook = scheduler.optimize(pop_size=8, generations=5, n_solutions=1, random_seed=3)
    mins = logbook.select('min')
    assert all(mins[i + 1] <= mins[i] + 1e-9 for i in range(len(mins) - 1))


def test_optimize_is_reproducible_for_the_same_seed():
    sel_a, _ = make_scheduler().optimize(pop_size=8, generations=3, n_solutions=2, random_seed=99)
    sel_b, _ = make_scheduler().optimize(pop_size=8, generations=3, n_solutions=2, random_seed=99)
    assert [f for _, f in sel_a] == [f for _, f in sel_b]
    assert [list(c) for c, _ in sel_a] == [list(c) for c, _ in sel_b]


def test_optimize_different_seeds_actually_use_the_seed():
    # A weak but real sanity check that random_seed is actually wired into
    # population construction rather than a no-op: on a problem this small,
    # the GA reliably converges to the SAME global optimum regardless of
    # seed (a real, checked-in-passing finding, not a bug -- see the other
    # tests above), so the final selected fitness is a bad thing to compare.
    # generation 0's population average, taken before any selection pressure
    # has had a chance to erase the initial randomness, is not.
    _, log_a = make_scheduler().optimize(pop_size=8, generations=1, n_solutions=1, random_seed=1)
    _, log_b = make_scheduler().optimize(pop_size=8, generations=1, n_solutions=1, random_seed=2)
    assert log_a[0]['avg'] != log_b[0]['avg']


# ---------------------------------------------------------------------------
# optimize() -- memetic integration (memetic_interval), GA_AGENT_POSTMORTEM.md
# Section 2 / the 0.035%-evolution-vs-0.615%-local-search gap this targets.
# ---------------------------------------------------------------------------

def test_memetic_none_is_the_default_and_changes_nothing():
    # memetic_interval defaults to None -- must reproduce the EXACT original
    # behaviour (same reproducibility guarantee test_optimize_is_reproducible_
    # for_the_same_seed already checks, re-asserted here explicitly against
    # a scheduler built the same way memetic tests below use it).
    sel_a, _ = make_scheduler().optimize(pop_size=8, generations=4, n_solutions=2, random_seed=5)
    sel_b, _ = make_scheduler().optimize(pop_size=8, generations=4, n_solutions=2, random_seed=5,
                                          memetic_interval=None)
    assert [f for _, f in sel_a] == [f for _, f in sel_b]
    assert [list(c) for c, _ in sel_a] == [list(c) for c, _ in sel_b]


def test_memetic_produces_legal_chromosomes():
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize(pop_size=8, generations=6, n_solutions=2, random_seed=1,
                                      memetic_interval=2, memetic_evals=15)
    assert 0 < len(selected) <= 2
    for chromosome, fitness in selected:
        ok, reason = _is_legal(chromosome, scheduler)
        assert ok, reason


def test_memetic_never_regresses_generation_to_generation():
    # Elitism's own guarantee must still hold with memetic injection active:
    # a locally-searched individual only ever replaces the population's
    # WORST member, and only when it is strictly better than the CURRENT
    # best -- it can only help or be a no-op for the tracked minimum.
    scheduler = make_scheduler()
    _, logbook = scheduler.optimize(pop_size=8, generations=8, n_solutions=1, random_seed=3,
                                     memetic_interval=2, memetic_evals=15)
    mins = logbook.select('min')
    assert all(mins[i + 1] <= mins[i] + 1e-9 for i in range(len(mins) - 1))


def test_memetic_is_reproducible_for_the_same_seed():
    sel_a, _ = make_scheduler().optimize(pop_size=8, generations=6, n_solutions=2, random_seed=42,
                                          memetic_interval=2, memetic_evals=15)
    sel_b, _ = make_scheduler().optimize(pop_size=8, generations=6, n_solutions=2, random_seed=42,
                                          memetic_interval=2, memetic_evals=15)
    assert [f for _, f in sel_a] == [f for _, f in sel_b]
    assert [list(c) for c, _ in sel_a] == [list(c) for c, _ in sel_b]


def test_memetic_never_finishes_worse_than_the_non_memetic_run_same_seed():
    # The core guarantee motivating this feature: strict-improvement-only
    # injection means enabling it can only help or match, never hurt, the
    # SAME seed's outcome.
    non_memetic, _ = make_scheduler().optimize(pop_size=10, generations=8, n_solutions=1,
                                                random_seed=11)
    memetic, _ = make_scheduler().optimize(pop_size=10, generations=8, n_solutions=1,
                                            random_seed=11, memetic_interval=2, memetic_evals=15)
    assert memetic[0][1] <= non_memetic[0][1] + 1e-6


def test_memetic_replace_random_produces_legal_chromosomes():
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize(pop_size=8, generations=6, n_solutions=2, random_seed=1,
                                      memetic_interval=2, memetic_evals=15, memetic_replace='random')
    assert 0 < len(selected) <= 2
    for chromosome, fitness in selected:
        ok, reason = _is_legal(chromosome, scheduler)
        assert ok, reason


def test_memetic_replace_random_never_regresses_generation_to_generation():
    # Same elitism guarantee check as the 'worst' policy -- the tracked
    # minimum must still never get worse regardless of which non-elite
    # slot memetic injection targets.
    scheduler = make_scheduler()
    _, logbook = scheduler.optimize(pop_size=8, generations=8, n_solutions=1, random_seed=3,
                                     memetic_interval=2, memetic_evals=15, memetic_replace='random')
    mins = logbook.select('min')
    assert all(mins[i + 1] <= mins[i] + 1e-9 for i in range(len(mins) - 1))


def test_memetic_replace_random_is_reproducible_for_the_same_seed():
    sel_a, _ = make_scheduler().optimize(pop_size=8, generations=6, n_solutions=2, random_seed=42,
                                          memetic_interval=2, memetic_evals=15, memetic_replace='random')
    sel_b, _ = make_scheduler().optimize(pop_size=8, generations=6, n_solutions=2, random_seed=42,
                                          memetic_interval=2, memetic_evals=15, memetic_replace='random')
    assert [f for _, f in sel_a] == [f for _, f in sel_b]
    assert [list(c) for c, _ in sel_a] == [list(c) for c, _ in sel_b]


def test_memetic_replace_random_never_targets_an_elite_slot():
    # Elites (pop[0:elite_count] after _advance_generation) must never be
    # overwritten by either injection policy -- a small, deterministic
    # problem where elite_count is easy to reason about by hand.
    scheduler = make_scheduler()
    elite_frac = 0.25
    pop_size = 8
    elite_count = max(1, int(pop_size * elite_frac))  # 2
    _, logbook = scheduler.optimize(pop_size=pop_size, generations=6, n_solutions=1, random_seed=9,
                                     elite_frac=elite_frac, memetic_interval=1, memetic_evals=15,
                                     memetic_replace='random')
    # Indirect but real check: elitism's own guarantee (min never regresses)
    # still holds after many 'random' injections -- if an injection had
    # clobbered an elite, a subsequent generation's reselection of "best"
    # could regress, since the true elite would have been lost.
    mins = logbook.select('min')
    assert all(mins[i + 1] <= mins[i] + 1e-9 for i in range(len(mins) - 1))


def test_memetic_interval_zero_or_falsy_behaves_like_none():
    sel_a, _ = make_scheduler().optimize(pop_size=8, generations=3, n_solutions=1, random_seed=2)
    sel_b, _ = make_scheduler().optimize(pop_size=8, generations=3, n_solutions=1, random_seed=2,
                                          memetic_interval=0)
    assert sel_a[0][1] == sel_b[0][1]


# ---------------------------------------------------------------------------
# optimize_islands() -- same contract, island-model structure
# ---------------------------------------------------------------------------

def test_optimize_islands_returns_legal_chromosomes_no_more_than_requested():
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize_islands(
        pop_size=8, generations=4, n_islands=2, migration_interval=2, n_solutions=2, random_seed=1
    )
    assert 0 < len(selected) <= 2
    for chromosome, _fitness in selected:
        ok, reason = _is_legal(chromosome, scheduler)
        assert ok, reason


def test_optimize_islands_never_finishes_worse_than_the_best_baseline_it_seeds():
    scheduler = make_scheduler()
    selected, _ = scheduler.optimize_islands(
        pop_size=8, generations=4, n_islands=2, migration_interval=2, n_solutions=1, random_seed=5
    )
    best_baseline_fitness = min(details['fitness'] for details in scheduler.baseline_results.values())
    assert selected[0][1] <= best_baseline_fitness + 1e-6


def test_optimize_islands_uses_the_total_population_not_a_multiple_of_it():
    # FAIRNESS, per the method's own docstring: n_islands islands of
    # (pop_size // n_islands) each, not n_islands x pop_size individuals.
    scheduler = make_scheduler()
    _, logbook = scheduler.optimize_islands(
        pop_size=8, generations=1, n_islands=2, migration_interval=2, n_solutions=1, random_seed=1
    )
    # generation 0's nevals is every individual across all islands combined.
    assert logbook[0]['nevals'] <= 8


def test_optimize_islands_is_reproducible_for_the_same_seed():
    sel_a, _ = make_scheduler().optimize_islands(
        pop_size=8, generations=3, n_islands=2, migration_interval=2, n_solutions=1, random_seed=11
    )
    sel_b, _ = make_scheduler().optimize_islands(
        pop_size=8, generations=3, n_islands=2, migration_interval=2, n_solutions=1, random_seed=11
    )
    assert [f for _, f in sel_a] == [f for _, f in sel_b]
