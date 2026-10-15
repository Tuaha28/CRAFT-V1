"""
Tests for local-search polishing.

The moves mutate a chromosome directly, so the risk is producing something
that looks fine and scores well but is not a legal schedule -- a duplicated
operation, a broken routing order, a line the operation cannot run on, or a
machine count larger than the pool. Every one of those would score BETTER than
a legal schedule, which is exactly why they need asserting.
"""

import random

import pytest

from ga.local_search import (
    compute_pool_loads,
    polish,
    _move_adjust_machines,
    _move_reassign_line,
    _move_swap_sequence,
)


class FakeFactory:
    """Minimal stand-in for ChromosomeFactory."""

    def __init__(self):
        # Two orders, two operations each; two machine types; three lines.
        self.optype_for_op = {
            '1_1': 'body', '1_2': 'hem',
            '2_1': 'body', '2_2': 'hem',
        }
        self.eligibility_map = {k: [10, 11, 12] for k in self.optype_for_op}
        # Real routing order, as ChromosomeFactory exposes it -- op 1 then op 2
        # within each order (see ga/operators.py::repair_precedence).
        self.op_sequence_map = {'1_1': 1, '1_2': 2, '2_1': 1, '2_2': 2}
        self.machine_ids_per_line_map = {
            k: {10: [1, 2, 3], 11: [4, 5], 12: [6, 7, 8, 9]}
            for k in self.optype_for_op
        }

    def _max_machines_for(self, op_key, line_id):
        return max(1, len(self.machine_ids_per_line_map[op_key].get(line_id, [])))


NUM_OPS = 4
OPS = ['1_1', '1_2', '2_1', '2_2']
OP_WORK = {'1_1': 100.0, '1_2': 50.0, '2_1': 300.0, '2_2': 20.0}


def chromo(lines, mcs):
    return list(OPS) + list(lines) + list(mcs)


@pytest.fixture
def factory():
    return FakeFactory()


def is_valid(cand, factory):
    """Every invariant a chromosome must satisfy to be a legal schedule."""
    os_a = cand[:NUM_OPS]
    la = cand[NUM_OPS:2 * NUM_OPS]
    mc = cand[2 * NUM_OPS:]
    if sorted(os_a) != sorted(OPS):
        return False, "operations duplicated or lost"
    seen = {}
    for k in os_a:
        order, step = k.split('_')[0], int(k.split('_')[1])
        if order in seen and step < seen[order]:
            return False, f"precedence violated in order {order}"
        seen[order] = step
    for i, k in enumerate(os_a):
        if la[i] not in factory.eligibility_map[k]:
            return False, f"{k} on ineligible line {la[i]}"
        if not (1 <= mc[i] <= factory._max_machines_for(k, la[i])):
            return False, f"{k} machine count {mc[i]} outside pool"
    return True, ""


# --------------------------------------------------------------------------
# Pool loads
# --------------------------------------------------------------------------

def test_pool_load_is_work_per_machine(factory):
    # 1_1 (100) and 2_1 (300) are both 'body' on line 10, which has 3 machines.
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
    assert loads[(10, 'body')] == pytest.approx(400.0 / 3)
    assert loads[(10, 'hem')] == pytest.approx(70.0 / 3)


def test_pools_are_separated_by_type(factory):
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
    assert (10, 'body') in loads and (10, 'hem') in loads


def test_pool_load_reflects_line_capacity(factory):
    # Same work, different pool sizes: line 11 has 2 machines, line 12 has 4.
    a = compute_pool_loads(chromo([11, 11, 11, 11], [1] * 4), NUM_OPS, factory, OP_WORK)
    b = compute_pool_loads(chromo([12, 12, 12, 12], [1] * 4), NUM_OPS, factory, OP_WORK)
    assert a[(11, 'body')] > b[(12, 'body')]


# --------------------------------------------------------------------------
# Individual moves preserve validity
# --------------------------------------------------------------------------

def test_swap_move_keeps_the_chromosome_legal(factory):
    rng = random.Random(0)
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    for _ in range(50):
        cand = _move_swap_sequence(c, NUM_OPS, factory, (10, 'body'), rng)
        if cand is None:
            continue
        ok, why = is_valid(cand, factory)
        assert ok, why


def test_swap_move_never_breaks_precedence(factory):
    # Both operations of order 1 sit on the pool; swapping them positionally
    # must be repaired back into routing order.
    rng = random.Random(1)
    f = FakeFactory()
    f.optype_for_op = {'1_1': 'body', '1_2': 'body', '2_1': 'body', '2_2': 'body'}
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    for _ in range(50):
        cand = _move_swap_sequence(c, NUM_OPS, f, (10, 'body'), rng)
        if cand is None:
            continue
        os_a = cand[:NUM_OPS]
        for order in ('1', '2'):
            steps = [int(k.split('_')[1]) for k in os_a if k.startswith(order + '_')]
            assert steps == sorted(steps), f"order {order} out of sequence: {os_a}"


def test_line_move_respects_eligibility_and_reclamps_machines(factory):
    rng = random.Random(2)
    # Start on line 12 (4 machines) with MC 4; moving to line 11 (2 machines)
    # must clamp MC down to 2, not carry 4 across.
    c = chromo([12, 12, 12, 12], [4, 4, 4, 4])
    loads = {(10, 'body'): 999.0, (11, 'body'): 0.0, (12, 'body'): 500.0}
    for _ in range(30):
        cand = _move_reassign_line(c, NUM_OPS, factory, (12, 'body'), loads, rng)
        if cand is None:
            continue
        ok, why = is_valid(cand, factory)
        assert ok, why


def test_line_move_targets_the_least_loaded_alternative(factory):
    rng = random.Random(3)
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    loads = {(10, 'body'): 900.0, (11, 'body'): 800.0, (12, 'body'): 1.0}
    cand = _move_reassign_line(c, NUM_OPS, factory, (10, 'body'), loads, rng)
    assert cand is not None
    moved = [i for i in range(NUM_OPS) if cand[NUM_OPS + i] != c[NUM_OPS + i]]
    assert moved, "no operation was moved"
    assert cand[NUM_OPS + moved[0]] == 12, "did not pick the least-loaded line"


def test_machine_move_stays_within_the_pool(factory):
    rng = random.Random(4)
    c = chromo([11, 11, 11, 11], [2, 2, 2, 2])   # line 11 has exactly 2 machines
    for _ in range(50):
        cand = _move_adjust_machines(c, NUM_OPS, factory, (11, 'body'), rng)
        if cand is None:
            continue
        ok, why = is_valid(cand, factory)
        assert ok, why
        assert all(1 <= v <= 2 for v in cand[2 * NUM_OPS:])


def test_machine_move_returns_none_when_no_room(factory):
    # A pool of one machine allows neither an increase nor a decrease.
    f = FakeFactory()
    f.machine_ids_per_line_map = {k: {10: [1]} for k in f.optype_for_op}
    f.eligibility_map = {k: [10] for k in f.optype_for_op}
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    assert _move_adjust_machines(c, NUM_OPS, f, (10, 'body'), random.Random(5)) is None


def test_moves_on_an_empty_pool_return_none(factory):
    rng = random.Random(6)
    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    empty = (11, 'body')   # nothing assigned there
    assert _move_swap_sequence(c, NUM_OPS, factory, empty, rng) is None
    assert _move_reassign_line(c, NUM_OPS, factory, empty, {}, rng) is None
    assert _move_adjust_machines(c, NUM_OPS, factory, empty, rng) is None


# --------------------------------------------------------------------------
# The hill climber
# --------------------------------------------------------------------------

def test_polish_never_returns_a_worse_schedule(factory):
    # Fitness rises with the busiest pool's load, so spreading work helps.
    def evaluate(c):
        loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
        return (max(loads.values()) if loads else 0.0,)

    c = chromo([10, 10, 10, 10], [1, 1, 1, 1])
    before = evaluate(c)[0]
    best, fit, stats = polish(c, NUM_OPS, factory, OP_WORK, evaluate,
                              max_evals=80, rng=random.Random(7))
    assert fit <= before + 1e-9
    assert stats['final_fitness'] == pytest.approx(fit)


def test_polish_output_is_always_a_legal_schedule(factory):
    def evaluate(c):
        loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
        return (max(loads.values()) if loads else 0.0,)

    for seed in range(5):
        best, _fit, _s = polish(chromo([10, 10, 10, 10], [1, 1, 1, 1]),
                                NUM_OPS, factory, OP_WORK, evaluate,
                                max_evals=60, rng=random.Random(seed))
        ok, why = is_valid(best, factory)
        assert ok, why


def test_polish_actually_improves_a_bad_start(factory):
    # Everything piled on one line; moving work to the other two must help.
    def evaluate(c):
        loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
        return (max(loads.values()) if loads else 0.0,)

    c = chromo([11, 11, 11, 11], [1, 1, 1, 1])   # line 11 = only 2 machines
    _best, fit, stats = polish(c, NUM_OPS, factory, OP_WORK, evaluate,
                               max_evals=120, rng=random.Random(8))
    assert fit < stats['start_fitness'], "no improvement from a clearly bad start"
    assert stats['improvement_pct'] > 0


def test_polish_respects_its_evaluation_budget(factory):
    calls = {'n': 0}

    def evaluate(c):
        calls['n'] += 1
        loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
        return (max(loads.values()) if loads else 0.0,)

    polish(chromo([11, 11, 11, 11], [1] * 4), NUM_OPS, factory, OP_WORK,
           evaluate, max_evals=25, rng=random.Random(9))
    assert calls['n'] <= 25


def test_polish_reports_which_move_types_were_accepted(factory):
    def evaluate(c):
        loads = compute_pool_loads(c, NUM_OPS, factory, OP_WORK)
        return (max(loads.values()) if loads else 0.0,)

    _b, _f, stats = polish(chromo([11, 11, 11, 11], [1] * 4), NUM_OPS, factory,
                           OP_WORK, evaluate, max_evals=120, rng=random.Random(10))
    assert set(stats['accepted']) == {'swap', 'line', 'machines'}
    assert sum(stats['accepted'].values()) >= 1
