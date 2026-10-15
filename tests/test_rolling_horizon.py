"""
Tests for extract_committed_subschedule -- the correctness-critical piece of
the overlapping-lookahead rolling horizon.

The risk: filtering a solved chromosome down to a subset of orders must not
duplicate, drop, or reorder operations, and the L==C case must be a true
no-op so it reproduces the original non-overlapping behaviour exactly.
"""

import pytest

from ga.rolling_horizon import extract_committed_subschedule


def make_chromosome(ops, lines, mcs):
    return list(ops) + list(lines) + list(mcs)


def test_filters_to_only_the_committed_orders():
    ops = ['1_1', '2_1', '1_2', '3_1', '2_2']
    c = make_chromosome(ops, [10, 11, 10, 12, 11], [1, 2, 1, 3, 2])
    filtered, n = extract_committed_subschedule(c, len(ops), {'1', '2'})
    os_a = filtered[:n]
    assert set(k.split('_')[0] for k in os_a) == {'1', '2'}
    assert n == 4


def test_preserves_relative_order_within_each_order():
    ops = ['1_3', '2_1', '1_1', '3_1', '1_2']
    c = make_chromosome(ops, [10] * 5, [1] * 5)
    filtered, n = extract_committed_subschedule(c, len(ops), {'1'})
    os_a = filtered[:n]
    # Order 1's operations appeared as 3, 1, 2 in the source; filtering must
    # not reorder them -- only delete the other orders' positions.
    assert os_a == ['1_3', '1_1', '1_2']


def test_line_and_machine_count_travel_with_their_operation():
    ops = ['1_1', '2_1', '1_2']
    c = make_chromosome(ops, [10, 20, 30], [1, 2, 3])
    filtered, n = extract_committed_subschedule(c, len(ops), {'1'})
    os_a, la, mc = filtered[:n], filtered[n:2 * n], filtered[2 * n:]
    assert os_a == ['1_1', '1_2']
    assert la == [10, 30]
    assert mc == [1, 3]


def test_no_operation_is_duplicated_or_invented():
    ops = ['1_1', '2_1', '3_1', '1_2', '2_2']
    c = make_chromosome(ops, [10] * 5, [1] * 5)
    filtered, n = extract_committed_subschedule(c, len(ops), {'1', '2', '3'})
    assert sorted(filtered[:n]) == sorted(ops)


def test_committing_everything_is_a_true_identity():
    # L == C: the lookahead and commit sets are the same orders. This is the
    # case that must reproduce the original non-overlapping behaviour exactly.
    ops = ['1_1', '2_1', '1_2', '3_1']
    lines, mcs = [10, 11, 10, 12], [1, 2, 1, 3]
    c = make_chromosome(ops, lines, mcs)
    filtered, n = extract_committed_subschedule(c, len(ops), {'1', '2', '3'})
    assert n == len(ops)
    assert filtered == c


def test_committing_nothing_returns_empty():
    ops = ['1_1', '2_1']
    c = make_chromosome(ops, [10, 11], [1, 2])
    filtered, n = extract_committed_subschedule(c, len(ops), {'99'})
    assert n == 0
    assert filtered == [[], [], []][0] + [] + []  # all three segments empty
    assert filtered == []


def test_single_order_committed_from_a_large_lookahead():
    ops = [f'{o}_{i}' for o in range(1, 11) for i in (1, 2, 3)]
    c = make_chromosome(ops, [10] * len(ops), [1] * len(ops))
    filtered, n = extract_committed_subschedule(c, len(ops), {'7'})
    os_a = filtered[:n]
    assert os_a == ['7_1', '7_2', '7_3']


@pytest.mark.parametrize("committed", [set(), {'1'}, {'1', '2'}, {'1', '2', '3', '4'}])
def test_filtered_length_matches_the_committed_set(committed):
    ops = [f'{o}_{i}' for o in range(1, 5) for i in (1, 2)]
    c = make_chromosome(ops, [10] * len(ops), [1] * len(ops))
    filtered, n = extract_committed_subschedule(c, len(ops), committed)
    expected = sum(1 for k in ops if k.split('_')[0] in committed)
    assert n == expected
    assert len(filtered) == 3 * n


def test_committed_ids_must_be_strings_not_native_order_ids():
    # Regression test for a real bug: run_rolling_horizon originally passed
    # committed order ids straight from a pandas column (native int64), while
    # op_key.split('_')[0] is always a string (op_key is an f-string built by
    # make_key). {5, 12} never matches {'5', '12'} -- every window silently
    # committed nothing until a smoke test caught it before the real sweep.
    ops = ['5_1', '12_1', '5_2']
    c = make_chromosome(ops, [10] * 3, [1] * 3)
    int_ids = {5, 12}      # what a pandas column gives you directly
    str_ids = {'5', '12'}  # what op_key.split('_')[0] actually is
    _f_int, n_int = extract_committed_subschedule(c, len(ops), int_ids)
    _f_str, n_str = extract_committed_subschedule(c, len(ops), str_ids)
    assert n_int == 0, "native int ids matched nothing (expected) -- caller must stringify"
    assert n_str == 3, "string ids matched everything"
