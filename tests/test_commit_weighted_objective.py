"""
Regression tests for the commit-weighted objective fix (scored_order_ids in
ga/fitness.py::evaluate_schedule).

Context (see PROJECT_STATUS.txt Section 0B): the rolling-horizon GA is shown
a lookahead wider than what it commits each round, so machine contention is
realistic. Before this fix, the fitness function also *scored* makespan and
deadline-lateness across the whole lookahead, as if every order in it were
being locked in now -- pulling the GA toward orders that get re-planned from
scratch next round, at the expense of the orders actually being committed.
Two independent sweeps (widening the lookahead; shrinking the commit chunk)
both made on-time rate, makespan, throughput and labour utilisation worse as
the scored-but-uncommitted fraction grew, which is what motivated this fix.

Scenario used throughout: two single-operation orders sharing one machine,
so they serialize in os_array order.
    A_0: 60 min of work,  deadline  30 min -> finishes at t=60, LATE by 30min
    B_0: 120 min of work, deadline 1000 min -> finishes at t=205 (after A,
         plus the 25min changeover for switching orders on the shared
         machine), on time
os_array = [A_0, B_0], so A is scheduled first and B waits for the shared
machine -- the whole-lookahead makespan is always B's completion (205 min),
regardless of which orders are scored.
"""

import pytest

from ga.fitness import evaluate_schedule


def _scenario():
    num_ops = 2
    os_array = ['A_0', 'B_0']
    la_array = [1, 1]
    mc_array = [1, 1]
    individual = os_array + la_array + mc_array

    op_details = {
        'A_0': {'setup_mins': 0.0, 'serial_processing_mins': 60.0},
        'B_0': {'setup_mins': 0.0, 'serial_processing_mins': 120.0},
    }
    gnn_predictions = {
        'A_0': {'delay': 0.0, 'priority': 0.5},
        'B_0': {'delay': 0.0, 'priority': 0.5},
    }
    machine_pools = {
        'A_0': {1: ['m1']},
        'B_0': {1: ['m1']},
    }
    machine_line_map = {'m1': 1}
    order_deadlines = {'A': 30.0, 'B': 1000.0}

    kwargs = dict(
        all_line_ids=[1],
        order_deadlines=order_deadlines,
        machine_pools=machine_pools,
        machine_line_map=machine_line_map,
        usable_machine_count=1,
        present_operator_count=1,
        return_details=True,
        op_sequence_map={'A_0': 1, 'B_0': 1},
    )
    return individual, num_ops, op_details, gnn_predictions, kwargs


def test_scored_order_ids_omitted_matches_explicit_none():
    """Backward compatibility: not passing the parameter at all must be
    identical to passing scored_order_ids=None, for every caller that hasn't
    opted in (baseline GAScheduler.__init__ callers, the module's own
    __main__ smoke test, etc.)."""
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _f1, det_omitted = evaluate_schedule(individual, num_ops, op_details, gnn_predictions, **kwargs)
    _f2, det_none = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                       scored_order_ids=None, **kwargs)
    assert det_omitted == det_none


def test_whole_lookahead_scoring_sees_both_orders():
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _fit, det = evaluate_schedule(individual, num_ops, op_details, gnn_predictions, **kwargs)

    assert det['orders_with_deadline'] == 2
    assert det['orders_late'] == 1  # only A
    assert det['on_time_rate'] == 0.5
    assert det['makespan_minutes'] == 205.0
    assert det['scored_makespan_minutes'] == 205.0  # == makespan when unrestricted


def test_scoring_only_the_committed_order_ignores_the_provisional_one():
    """When only A is committed this round, B (still simulated for realistic
    machine contention, but not committed) must not appear in the lateness
    stats or drive the makespan term at all."""
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _fit, det = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                   scored_order_ids={'A'}, **kwargs)

    assert det['orders_with_deadline'] == 1
    assert det['orders_late'] == 1
    assert det['on_time_rate'] == 0.0
    assert 'B' not in det['order_lateness_hours']
    assert det['order_lateness_hours']['A'] == pytest.approx(0.5)

    # The whole-lookahead makespan (load-balance/idle-labour scope) is
    # unaffected by which orders are scored...
    assert det['makespan_minutes'] == 205.0
    # ...but the makespan actually driving the ALPHA fitness term reflects
    # only the committed order's completion (A finishes at t=60), not the
    # provisional order still being re-planned next round (B, at t=205).
    assert det['scored_makespan_minutes'] == 60.0


def test_scoring_only_the_on_time_order_flips_the_verdict():
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _fit, det = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                   scored_order_ids={'B'}, **kwargs)

    assert det['orders_with_deadline'] == 1
    assert det['orders_late'] == 0
    assert det['on_time_rate'] == 1.0
    assert 'A' not in det['order_lateness_hours']
    # B is the last order to finish either way, so restricting to {B} doesn't
    # change the scored makespan in this particular layout.
    assert det['scored_makespan_minutes'] == 205.0


def test_fitness_score_is_lower_when_scoring_only_the_on_time_order():
    """The whole point of the fix: an identical chromosome must score BETTER
    when judged only on the order(s) actually being committed and one of
    them is on time, than when a late provisional order drags the whole-
    lookahead score down too. Lower fitness = better (DEAP minimizes)."""
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    fit_whole, _ = evaluate_schedule(individual, num_ops, op_details, gnn_predictions, **kwargs)
    fit_b_only, _ = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                       scored_order_ids={'B'}, **kwargs)
    assert fit_b_only[0] < fit_whole[0]


def test_scored_order_ids_with_no_matching_order_falls_back_to_base_makespan():
    """Defensive: a scored set that (should never happen, but) matches
    nothing in this chromosome must not crash or silently zero the makespan
    term -- it falls back to the whole-lookahead makespan."""
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _fit, det = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                   scored_order_ids={'NOPE'}, **kwargs)
    assert det['orders_with_deadline'] == 0
    assert det['scored_makespan_minutes'] == det['makespan_minutes'] == 205.0


def test_load_balance_and_idle_labour_scope_stays_whole_lookahead():
    """load_balance_penalty and labour utilisation reflect everything
    simulated (real machine/labour usage), not just the scored subset --
    restricting those too would reintroduce blindness to real resource
    commitments, per the docstring in ga/fitness.py."""
    individual, num_ops, op_details, gnn_predictions, kwargs = _scenario()
    _f1, det_whole = evaluate_schedule(individual, num_ops, op_details, gnn_predictions, **kwargs)
    _f2, det_a_only = evaluate_schedule(individual, num_ops, op_details, gnn_predictions,
                                         scored_order_ids={'A'}, **kwargs)
    assert det_whole['labor_used_minutes'] == det_a_only['labor_used_minutes']
    assert det_whole['load_balance_stdev_minutes'] == det_a_only['load_balance_stdev_minutes']
    assert det_whole['labor_utilization_vs_ceiling'] == det_a_only['labor_utilization_vs_ceiling']
