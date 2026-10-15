"""
Regression tests for PROJECT_STATUS.txt Section 0H.8: bottleneck-aware
machine-selection priority.

Root-cause context (Section 0H.6): Tubular Sleeve Join was found to be the
LOOSEST machine type by raw capacity (not a shortage), but the most
congested in live simulation because every order's routing is rigidly
fixed and it's always the 5th/last step -- a structural funnel, not a
resource-count problem. This fix softens strict greedy machine selection
for whichever type is THIS window's single most-congested one (measured
fresh each time, never hardcoded), matching the existing weighted-top-k
style ga/operators.py::choose_line_work_aware already uses for line choice.

Two things are tested: most_congested_machine_type() (the pure,
per-window measurement, factored out of GAScheduler for testability), and
evaluate_schedule's threading of op_optype/protected_machine_type into
is_protected_type for the machine-selection call -- confirmed via a real
two-order, two-machine-type scenario.
"""

import pandas as pd
import pytest

from ga.scheduler import most_congested_machine_type
from ga.fitness import evaluate_schedule


# ---------------------------------------------------------------------------
# most_congested_machine_type
# ---------------------------------------------------------------------------

def _machines(rows):
    """rows: [(asset_id, operation_type), ...]"""
    return pd.DataFrame(rows, columns=['asset_id', 'operation_type'])


def test_empty_machine_free_returns_none_nothing_carried_forward_yet():
    machines = _machines([(1, 'A'), (2, 'B')])
    assert most_congested_machine_type(machines, {}) is None
    assert most_congested_machine_type(machines, None) is None


def test_single_type_returns_none_nothing_to_be_more_congested_than():
    machines = _machines([(1, 'A'), (2, 'A')])
    free = {1: 1000.0, 2: 2000.0}
    assert most_congested_machine_type(machines, free) is None


def test_perfect_tie_returns_none():
    machines = _machines([(1, 'A'), (2, 'B')])
    free = {1: 500.0, 2: 500.0}
    assert most_congested_machine_type(machines, free) is None


def test_clear_majority_congested_type_is_identified():
    machines = _machines([(1, 'A'), (2, 'A'), (3, 'B'), (4, 'B')])
    # type A machines booked much further ahead than type B's.
    free = {1: 5000.0, 2: 6000.0, 3: 100.0, 4: 200.0}
    assert most_congested_machine_type(machines, free) == 'a'  # normalized lowercase


def test_untouched_machines_default_to_zero_not_missing():
    # Only asset 1 (type A) appears in machine_free; every type-B machine
    # (2, 3) and the second type-A machine (4) have never been touched --
    # must read as free at 0, not be silently excluded from the mean.
    machines = _machines([(1, 'A'), (4, 'A'), (2, 'B'), (3, 'B')])
    free = {1: 10000.0}
    result = most_congested_machine_type(machines, free)
    assert result == 'a'  # (10000+0)/2=5000 vs (0+0)/2=0 for B


def test_missing_operation_type_column_returns_none_not_a_crash():
    machines = pd.DataFrame({'asset_id': [1, 2]})
    assert most_congested_machine_type(machines, {1: 100.0, 2: 0.0}) is None


def test_empty_dataframe_returns_none():
    assert most_congested_machine_type(pd.DataFrame(), {1: 100.0}) is None
    assert most_congested_machine_type(None, {1: 100.0}) is None


def test_type_names_are_case_and_whitespace_normalized():
    # Mirrors utils/machine_reserve.py's own normalization convention, so
    # this lines up with op_optype (ga/chromosome.py's optype_for_op, also
    # lowercase-normalized) when evaluate_schedule compares them.
    machines = _machines([(1, ' Tubular Sleeve Join '), (2, 'Body Assembly')])
    free = {1: 9000.0, 2: 100.0}
    assert most_congested_machine_type(machines, free) == 'tubular sleeve join'


# ---------------------------------------------------------------------------
# evaluate_schedule threading: op_optype + protected_machine_type ->
# is_protected_type at the machine-selection call
# ---------------------------------------------------------------------------

def test_protection_only_activates_for_the_matching_type():
    # Two operations, two different types, sharing nothing -- confirms
    # is_protected_type is computed PER OPERATION from its own optype, not
    # applied blanket across the whole chromosome.
    num_ops = 2
    individual = ['1_1', '2_1', 10, 20, 1, 1]  # os, la, mc (2 ops each)
    op_details = {
        '1_1': {'setup_mins': 0.0, 'serial_processing_mins': 60.0},
        '2_1': {'setup_mins': 0.0, 'serial_processing_mins': 60.0},
    }
    gnn_predictions = {
        '1_1': {'delay': 0.0, 'priority': 0.5},
        '2_1': {'delay': 0.0, 'priority': 0.5},
    }
    machine_pools = {
        '1_1': {10: ['m1', 'm2', 'm3']},
        '2_1': {20: ['n1', 'n2', 'n3']},
    }
    machine_line_map = {'m1': 10, 'm2': 10, 'm3': 10, 'n1': 20, 'n2': 20, 'n3': 20}
    op_optype = {'1_1': 'protected_type', '2_1': 'other_type'}

    # Should run without error regardless of which type is protected, and
    # stay a pure no-op (identical fitness) when nothing is protected.
    kwargs = dict(
        all_line_ids=[10, 20], machine_pools=machine_pools,
        machine_line_map=machine_line_map, usable_machine_count=6,
        present_operator_count=2, return_details=True,
        op_sequence_map={'1_1': 1, '2_1': 1},
    )
    fit_none, det_none = evaluate_schedule(
        individual, num_ops, op_details, gnn_predictions,
        op_optype=op_optype, protected_machine_type=None, **kwargs)
    fit_unmatched, det_unmatched = evaluate_schedule(
        individual, num_ops, op_details, gnn_predictions,
        op_optype=op_optype, protected_machine_type='nonexistent_type', **kwargs)
    # No machine type in this chromosome matches "nonexistent_type", so this
    # must behave identically to no protection at all.
    assert det_none['makespan_minutes'] == det_unmatched['makespan_minutes']
    assert det_none['fitness'] == pytest.approx(det_unmatched['fitness'])


def test_missing_op_optype_or_protected_type_disables_the_feature_entirely():
    # Backward compatibility: omitting either argument (both default to
    # None) must reproduce the exact pre-existing behaviour -- every caller
    # that hasn't been updated (e.g. the module's own __main__ smoke test)
    # keeps working unchanged.
    num_ops = 1
    individual = ['1_1', 10, 1]
    op_details = {'1_1': {'setup_mins': 0.0, 'serial_processing_mins': 60.0}}
    gnn_predictions = {'1_1': {'delay': 0.0, 'priority': 0.5}}
    machine_pools = {'1_1': {10: ['m1', 'm2']}}
    kwargs = dict(
        all_line_ids=[10], machine_pools=machine_pools,
        machine_line_map={'m1': 10, 'm2': 10}, usable_machine_count=2,
        present_operator_count=1, return_details=True,
        op_sequence_map={'1_1': 1},
    )
    fit_omitted, det_omitted = evaluate_schedule(individual, num_ops, op_details, gnn_predictions, **kwargs)
    fit_explicit_none, det_explicit_none = evaluate_schedule(
        individual, num_ops, op_details, gnn_predictions,
        op_optype=None, protected_machine_type=None, **kwargs)
    assert det_omitted == det_explicit_none
