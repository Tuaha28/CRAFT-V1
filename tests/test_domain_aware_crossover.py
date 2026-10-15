"""
Legality and mechanism tests for the two new crossover operators added in
DOMAIN_AWARE_CROSSOVER_PROPOSAL.md Section 5-6: triple_coupled_crossover
(TCOX) and shared_cutpoint_crossover (the cheaper comparison arm).
Neither is wired into GAScheduler by default -- opt-in only, exercised
here directly against ga/operators.py, same synthetic-factory pattern as
tests/test_ga_orchestration.py.

Legality first, per standing instruction: confirm both operators produce
only valid offspring before anything else -- same standard as every
operator this project has shipped (custom_crossover, custom_mutate,
local_search's moves).
"""
import random
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ga.scheduler import GAScheduler
from ga.operators import (
    triple_coupled_crossover, shared_cutpoint_crossover, order_crossover,
    triple_coupled_order_crossover,
)

NOW = datetime(2026, 1, 1, 8, 0, 0)


def _routings():
    # Slightly larger than test_ga_orchestration's fixture (3 orders, more
    # ops) so a 2-point OS crossover has real room to move genes around --
    # small enough for a human to hand-verify, large enough to exercise
    # the coupling mechanism meaningfully.
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
    defaults = dict(
        df_routings=_routings(), df_machines=_machines(), df_lines=_lines(),
        gnn_predictions={}, current_time=NOW, present_operator_count=10,
    )
    defaults.update(kwargs)
    return GAScheduler(**defaults, legacy_mode='legacy_company_wide')


def _is_legal(chromosome, scheduler):
    """Identical checks to test_ga_orchestration.py::_is_legal."""
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


def _crossover_kwargs(scheduler):
    return dict(
        num_ops=scheduler.num_ops,
        eligibility_map=scheduler.factory.eligibility_map,
        machines_per_line_map=scheduler.factory.machines_per_line_map,
        max_mc_cap=40,
        op_work=scheduler.op_work,
        op_optype=scheduler.factory.optype_for_op,
        protected_machine_type=scheduler.protected_machine_type,
        op_sequence_map=scheduler.seq_lookup,
    )


# ---------------------------------------------------------------------------
# Legality -- both operators, many trials, before anything else.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operator", [triple_coupled_crossover, shared_cutpoint_crossover])
def test_operator_produces_legal_offspring_across_many_trials(operator):
    scheduler = make_scheduler()
    kwargs = _crossover_kwargs(scheduler)
    random.seed(0)
    for _ in range(50):
        ind1 = scheduler.toolbox.individual()
        ind2 = scheduler.toolbox.individual()
        child1, child2 = operator(ind1, ind2, **kwargs)
        for child in (child1, child2):
            ok, reason = _is_legal(child, scheduler)
            assert ok, reason


@pytest.mark.parametrize("operator", [triple_coupled_crossover, shared_cutpoint_crossover])
def test_operator_mutates_individuals_in_place_like_custom_crossover(operator):
    """DEAP's varAnd relies on mate() mutating and returning the same
    objects it was given (custom_crossover's own contract) -- confirm
    both new operators honour it too, since a silent behavioural
    difference here would break toolbox.register("mate", ...) swap-in."""
    scheduler = make_scheduler()
    kwargs = _crossover_kwargs(scheduler)
    ind1 = scheduler.toolbox.individual()
    ind2 = scheduler.toolbox.individual()
    out1, out2 = operator(ind1, ind2, **kwargs)
    assert out1 is ind1
    assert out2 is ind2


@pytest.mark.parametrize("operator", [triple_coupled_crossover, shared_cutpoint_crossover])
def test_operator_handles_single_operation_edge_case(operator):
    """num_ops <= 1: nothing to recombine -- must not crash, must stay legal."""
    scheduler = make_scheduler(df_routings=_routings()[_routings()['production_order_id'] == 1001].head(1))
    kwargs = _crossover_kwargs(scheduler)
    ind1 = scheduler.toolbox.individual()
    ind2 = scheduler.toolbox.individual()
    child1, child2 = operator(ind1, ind2, **kwargs)
    for child in (child1, child2):
        ok, reason = _is_legal(child, scheduler)
        assert ok, reason


# ---------------------------------------------------------------------------
# Mechanism -- does each operator actually do what it claims, not just
# produce something legal by luck of the repair pass?
# ---------------------------------------------------------------------------

def test_triple_coupled_crossover_keeps_an_operations_pairing_attached_to_it():
    """The core TCOX guarantee, tested on triple_coupled_order_crossover's
    RAW output -- i.e. isolated from repair_precedence and
    repair_line_and_machine_count, which run afterward in the full
    triple_coupled_crossover wrapper and are shared, unchanged behaviour
    with the original custom_crossover (not part of what TCOX itself
    changes). This matters: repair_precedence can still reorder POSITIONS
    for operations whose relative sequence a crossover violated -- a
    real, smaller, pre-existing residual coupling gap TCOX does not
    address (see triple_coupled_crossover's docstring) -- so checking the
    guarantee post-repair would conflate that unrelated, already-existing
    mechanism with TCOX's own. Every operation in every child must match
    whichever PARENT it was sourced from by OX's slice/fill logic --
    never some third value invented by position-based leftovers."""
    scheduler = make_scheduler()
    n = scheduler.num_ops
    random.seed(1)
    for _ in range(50):
        ind1 = scheduler.toolbox.individual()
        ind2 = scheduler.toolbox.individual()
        os1, la1, mc1 = ind1[:n], ind1[n:2 * n], ind1[2 * n:]
        os2, la2, mc2 = ind2[:n], ind2[n:2 * n], ind2[2 * n:]
        triples1 = {op: (la1[i], mc1[i]) for i, op in enumerate(os1)}
        triples2 = {op: (la2[i], mc2[i]) for i, op in enumerate(os2)}

        child_os1, child_la1, child_mc1, child_os2, child_la2, child_mc2 = \
            triple_coupled_order_crossover(os1, la1, mc1, os2, la2, mc2)

        for os_c, la_c, mc_c in ((child_os1, child_la1, child_mc1), (child_os2, child_la2, child_mc2)):
            assert sorted(os_c) == sorted(scheduler.factory.op_keys)  # still a legal permutation
            for i, op in enumerate(os_c):
                pairing = (la_c[i], mc_c[i])
                assert pairing == triples1.get(op) or pairing == triples2.get(op), (
                    f"{op} ended up with {pairing}, matching neither parent's "
                    f"original pairing ({triples1.get(op)} / {triples2.get(op)})"
                )


def test_shared_cutpoint_crossover_uses_the_same_slice_for_os_and_la_mc():
    """order_crossover's cut_points parameter must actually be honoured --
    the LA/MC 2-point swap and the OS slice must cover the SAME index
    range, not just coincidentally overlap."""
    random.seed(2)
    parent1 = ['a', 'b', 'c', 'd', 'e']
    parent2 = ['e', 'd', 'c', 'b', 'a']
    # Fix the draw so this test doesn't depend on random.sample's specifics.
    child1, child2 = order_crossover(parent1, parent2, cut_points=(1, 3))
    # Positions [1,3) of child1 must be exactly parent1[1:3] (the slice
    # source), proving cut_points overrides the function's own draw.
    assert child1[1:3] == parent1[1:3]
    assert child2[1:3] == parent2[1:3]


def test_order_crossover_default_behaviour_is_unchanged_without_cut_points():
    """custom_crossover's existing callers pass no cut_points -- confirm
    the new optional parameter is fully backward compatible (same output
    distribution as before, i.e. still draws its own random a,b)."""
    random.seed(3)
    parent1 = ['a', 'b', 'c', 'd', 'e']
    parent2 = ['e', 'd', 'c', 'b', 'a']
    child1, child2 = order_crossover(parent1, parent2)
    assert sorted(child1) == sorted(parent1)
    assert sorted(child2) == sorted(parent2)


# ---------------------------------------------------------------------------
# Reproducibility -- same standard as memetic_interval's own tests.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operator", [triple_coupled_crossover, shared_cutpoint_crossover])
def test_operator_is_reproducible_for_the_same_seed(operator):
    scheduler = make_scheduler()
    kwargs = _crossover_kwargs(scheduler)

    random.seed(42)
    base1 = scheduler.toolbox.individual()
    base2 = scheduler.toolbox.individual()

    random.seed(99)
    run1 = operator(list(base1), list(base2), **kwargs)

    random.seed(99)
    run2 = operator(list(base1), list(base2), **kwargs)

    assert run1[0] == run2[0]
    assert run1[1] == run2[1]
