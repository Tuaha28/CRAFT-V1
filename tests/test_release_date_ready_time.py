"""
Regression tests for GA_AUDIT.md Section 5/6 (2026-08-24): release_date as a
genuine ready-time floor in ga/fitness.py::evaluate_schedule.

Context: release_date was previously read ONLY by ga/rolling_horizon.py to
sort orders into windows -- every order's first operation defaulted to
order_ready=0.0 inside evaluate_schedule regardless of its real release
date, so a whole window's worth of orders (e.g. window 1's real 300
orders/20 days) were simulated as all becoming available simultaneously at
t=0. Measured before this fix (scratch/release_date_enforcement_check.py):
that inflated peak same-instant machine-type backlog 6-7x relative to
letting demand arrive on real release dates (80-86% peak-backlog reduction
across all 5 machine types). Fixed by threading order_release_minutes
through GAScheduler -> evaluate_schedule (and independently through
agent/decision_engine.py's exporter, which mirrors evaluate_schedule's
ready-time logic rather than calling it, so it needed the identical fix to
keep "the exported schedule is the schedule that was scored" true).
"""

from ga.fitness import evaluate_schedule


def _single_op_kwargs():
    op_details = {'1_1': {'setup_mins': 0.0, 'serial_processing_mins': 60.0}}
    gnn_predictions = {'1_1': {'delay': 0.0, 'priority': 0.5}}
    machine_pools = {'1_1': {10: ['m1']}}
    machine_line_map = {'m1': 10}
    individual = ['1_1', 10, 1]  # os, la, mc -- one operation
    return individual, op_details, gnn_predictions, machine_pools, machine_line_map


def test_first_operation_cannot_start_before_release_date():
    individual, op_details, gnn_predictions, machine_pools, machine_line_map = _single_op_kwargs()
    _fit, det = evaluate_schedule(
        individual, 1, op_details, gnn_predictions,
        all_line_ids=[10], machine_pools=machine_pools,
        machine_line_map=machine_line_map, usable_machine_count=1,
        present_operator_count=1, return_details=True,
        op_sequence_map={'1_1': 1, '2_1': 2},
        order_release_minutes={'1': 500.0},
    )
    # duration is 60 mins (setup 0 + serial 60, delay_prob=0.0 is below the
    # risk-buffer gate so buffer_mins == duration_mins exactly) -- the
    # machine must free up at release(500) + duration(60), not 0 + 60.
    assert det['final_machine_free']['m1'] == 560.0


def test_omitting_the_arg_reproduces_the_old_unfixed_behaviour():
    individual, op_details, gnn_predictions, machine_pools, machine_line_map = _single_op_kwargs()
    _fit, det = evaluate_schedule(
        individual, 1, op_details, gnn_predictions,
        all_line_ids=[10], machine_pools=machine_pools,
        machine_line_map=machine_line_map, usable_machine_count=1,
        present_operator_count=1, return_details=True,
        op_sequence_map={'1_1': 1, '2_1': 2},
        # order_release_minutes omitted entirely
    )
    assert det['final_machine_free']['m1'] == 60.0


def test_release_floor_only_binds_the_first_operation_of_an_order():
    # Two operations of the SAME order, sequential (routing order 1_1 then
    # 1_2), sharing one machine so they cannot overlap. Once the first
    # operation's own completion already exceeds the release floor, the
    # second operation's ready time must come from precedence alone, not
    # be pushed later by max()-ing against the (already-passed) floor again.
    op_details = {
        '1_1': {'setup_mins': 0.0, 'serial_processing_mins': 60.0},
        '1_2': {'setup_mins': 0.0, 'serial_processing_mins': 40.0},
    }
    gnn_predictions = {
        '1_1': {'delay': 0.0, 'priority': 0.5},
        '1_2': {'delay': 0.0, 'priority': 0.5},
    }
    machine_pools = {'1_1': {10: ['m1']}, '1_2': {10: ['m1']}}
    machine_line_map = {'m1': 10}
    individual = ['1_1', '1_2', 10, 10, 1, 1]

    _fit, det = evaluate_schedule(
        individual, 2, op_details, gnn_predictions,
        all_line_ids=[10], machine_pools=machine_pools,
        machine_line_map=machine_line_map, usable_machine_count=1,
        present_operator_count=1, return_details=True,
        op_sequence_map={'1_1': 1, '1_2': 2},
        order_release_minutes={'1': 500.0},
    )
    # op1: starts at 500 (release floor), ends 560. op2: starts at 560
    # (precedence + machine availability, both already past the release
    # floor), ends 600 -- not pushed to max(560, 500)+40 with any extra
    # penalty, and not reset back toward the floor.
    assert det['final_machine_free']['m1'] == 600.0


def test_release_floor_clamped_non_negative_for_an_already_elapsed_date():
    # A release_date in the past (negative minutes-from-now, same convention
    # GAScheduler.__init__ uses for order_deadlines_minutes) must floor to
    # 0.0, not push start times artificially negative.
    individual, op_details, gnn_predictions, machine_pools, machine_line_map = _single_op_kwargs()
    _fit, det = evaluate_schedule(
        individual, 1, op_details, gnn_predictions,
        all_line_ids=[10], machine_pools=machine_pools,
        machine_line_map=machine_line_map, usable_machine_count=1,
        present_operator_count=1, return_details=True,
        op_sequence_map={'1_1': 1, '2_1': 2},
        order_release_minutes={'1': -500.0},
    )
    assert det['final_machine_free']['m1'] == 60.0
