"""
Tests for island-model migration and the (line, type) load-tracking fix.

Migration is easy to get subtly wrong in ways that produce a plausible-looking
run: migrating in place lets a just-arrived individual hop onward in the same
round, moving instead of copying strips an island of its own best, and a ring
that skips an island silently reduces the model to fewer populations. None of
those would crash.
"""

import random

import pytest
from deap import base, creator, tools

from ga.scheduler import GAScheduler
from ga.operators import compute_line_loads, choose_line_work_aware


# --------------------------------------------------------------------------
# Minimal individuals: fitness is the single gene, lower is better.
# --------------------------------------------------------------------------

if not hasattr(creator, "_TestFitnessMin"):
    creator.create("_TestFitnessMin", base.Fitness, weights=(-1.0,))
if not hasattr(creator, "_TestIndividual"):
    creator.create("_TestIndividual", list, fitness=creator._TestFitnessMin)


def ind(value):
    x = creator._TestIndividual([value])
    x.fitness.values = (float(value),)
    return x


def island(values):
    return [ind(v) for v in values]


def values(pop):
    return sorted(p[0] for p in pop)


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_best_is_copied_to_the_next_island():
    a = island([1, 50, 60])      # best = 1
    b = island([10, 20, 99])     # worst = 99
    GAScheduler._migrate([a, b], migrants_per_island=1)
    assert 1 in values(b), "island A's best did not arrive in island B"
    assert 99 not in values(b), "island B's worst was not displaced"


def test_migration_copies_rather_than_moves():
    # The source must KEEP its best. Moving it would make migration a net loss
    # for the sending island and could lose the global best entirely.
    a = island([1, 50, 60])
    b = island([10, 20, 99])
    GAScheduler._migrate([a, b], migrants_per_island=1)
    assert 1 in values(a), "island A lost its own best to migration"


def test_ring_closes_so_every_island_sends_and_receives():
    a, b, c = island([1, 90, 91]), island([2, 92, 93]), island([3, 94, 95])
    GAScheduler._migrate([a, b, c], migrants_per_island=1)
    assert 1 in values(b)   # a -> b
    assert 2 in values(c)   # b -> c
    assert 3 in values(a)   # c -> a, closing the ring


def test_island_sizes_are_preserved():
    islands = [island([1, 2, 3]), island([4, 5, 6]), island([7, 8, 9])]
    sizes = [len(i) for i in islands]
    GAScheduler._migrate(islands, migrants_per_island=2)
    assert [len(i) for i in islands] == sizes


def test_migrants_are_snapshotted_before_any_island_changes():
    # If migration ran in place, an individual arriving at island B this round
    # could immediately be selected as B's best and forwarded to C in the same
    # call -- a cascade, not a migration round. Island A's best (1) must reach
    # B but must NOT also appear in C.
    a, b, c = island([1, 70, 71]), island([40, 72, 73]), island([50, 74, 75])
    GAScheduler._migrate([a, b, c], migrants_per_island=1)
    assert 1 in values(b)
    assert 1 not in values(c), "a migrant hopped two islands in one round"


def test_multiple_migrants_displace_the_worst_ones():
    a = island([1, 2, 80, 81])
    b = island([10, 11, 98, 99])
    GAScheduler._migrate([a, b], migrants_per_island=2)
    assert 1 in values(b) and 2 in values(b)
    assert 98 not in values(b) and 99 not in values(b)


def test_migration_never_improves_by_inventing_individuals():
    # Total population across islands is conserved, and every value present
    # afterwards came from somewhere before.
    a, b = island([1, 5, 9]), island([2, 6, 10])
    before = set(values(a) + values(b))
    GAScheduler._migrate([a, b], migrants_per_island=1)
    after = values(a) + values(b)
    assert len(after) == 6
    assert set(after).issubset(before)


def test_single_migrant_from_a_two_member_island():
    a, b = island([1, 2]), island([3, 4])
    GAScheduler._migrate([a, b], migrants_per_island=1)
    assert len(a) == 2 and len(b) == 2
    assert 1 in values(b)


# --------------------------------------------------------------------------
# (line, type) load tracking
# --------------------------------------------------------------------------

def test_load_is_tracked_per_line_and_type_not_per_line():
    # Two operations of DIFFERENT types on the same line. Loading the line
    # with type A work must not make it look busy for type B -- that was the
    # defect that left 271 of 494 pools unused.
    os_array = ['1_1', '1_2']
    la_array = [7, 7]
    mc_array = [1, 1]
    op_work = {'1_1': 1000.0, '1_2': 0.0}
    optype = {'1_1': 'body assembly', '1_2': 'flatlock hemming'}
    pools = {'1_1': {7: 2}, '1_2': {7: 2}}

    loads = compute_line_loads(os_array, la_array, mc_array, op_work, pools, optype)
    assert loads[(7, 'body assembly')] == pytest.approx(500.0)     # 1000 / 2 machines
    assert loads[(7, 'flatlock hemming')] == pytest.approx(0.0)


def test_a_busy_type_does_not_hide_an_idle_one_on_the_same_line():
    # Line 7 is saturated with body assembly; line 8 has none. A flatlock
    # operation must be free to choose either, because line 7's flatlock
    # machines are untouched.
    loads = {(7, 'body assembly'): 99999.0}
    pools = {'op': {7: 5, 8: 5}}
    optype = {'op': 'flatlock hemming'}
    random.seed(0)
    picks = {choose_line_work_aware('op', [7, 8], loads, pools, top_k=2, op_optype=optype)
             for _ in range(30)}
    assert picks == {7, 8}, f"line 7 was excluded by unrelated load: {picks}"


def test_load_on_the_same_type_does_steer_away():
    # The converse: real load on the SAME type must push the choice elsewhere.
    loads = {(7, 'flatlock hemming'): 99999.0, (8, 'flatlock hemming'): 0.0}
    pools = {'op': {7: 5, 8: 5}}
    optype = {'op': 'flatlock hemming'}
    random.seed(0)
    picks = [choose_line_work_aware('op', [7, 8], loads, pools, top_k=1, op_optype=optype)
             for _ in range(10)]
    assert set(picks) == {8}, "strict greedy did not avoid the saturated pool"


def test_missing_optype_degrades_without_crashing():
    # Older callers may not supply the mapping; it must still return a line.
    loads = {}
    pools = {'op': {3: 1, 4: 1}}
    random.seed(0)
    assert choose_line_work_aware('op', [3, 4], loads, pools, top_k=2) in (3, 4)
