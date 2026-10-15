"""
Regression tests for graph/edge_builder.py::build_disjunctive_edges --
disjunctive (contention) operation<->operation edges, the second half of a
standard job-shop disjunctive graph alongside the existing conjunctive
(precedence) edges.

POOL DEFINITION (rebuilt during the graph-module review, see chat):
operation_type ONLY, company-wide -- REPLACES the original (majority
historical line, operation_type) pooling from PROJECT_STATUS.txt 0H.15.
That pooling was measured (not assumed) to be noise: an operation's lots
touch a mean of 16.7 distinct lines, and its "majority" line captures a
mean of only 11.8% of its own lots -- because
data_generation/seed_database_fixed.py's `machine_heaps[m_type]` pools
every machine of a type across all 99 lines with no line-scoping at all.
Operation_type alone, company-wide, matches what the real dispatch heap
actually constrains. The mechanistically-correct pooling was probed for
tractability before being adopted (17,768,706 directed edges; 1,036 MB
peak RSS; 37.7s/epoch vs. 4.3s/epoch for the old pooling) -- tractable,
not the same class of problem as the rejected same-machine-instance
candidate (~37M pairs, open-ended).

No longer needs df_assignments/df_machines -- with line dropped from the
pool key entirely, only each operation's own required_machine_type
(already in df_routings) is needed.
"""
import numpy as np
import pandas as pd
import torch

from graph.edge_builder import build_disjunctive_edges


def _routings(rows):
    """rows: [(order_id, op_id, required_machine_type), ...]"""
    return pd.DataFrame(rows, columns=['production_order_id', 'operation_id', 'required_machine_type'])


def _pairs(edge_index):
    """Set of (src, dst) tuples for easy membership assertions."""
    return set(zip(edge_index[0].tolist(), edge_index[1].tolist()))


# ---------------------------------------------------------------------------
# Core pool-grouping behaviour
# ---------------------------------------------------------------------------

def test_operations_sharing_a_type_are_connected_both_directions():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'single-needle lockstitch'),
    ])
    op_map = {'1_1': 0, '2_1': 1}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    pairs = _pairs(edges)
    assert (0, 1) in pairs
    assert (1, 0) in pairs
    assert edges.shape[1] == 2


def test_operations_on_different_lines_but_the_same_type_ARE_now_connected():
    # The decisive behaviour change from the old pooling: line no longer
    # gates contention at all, because the real dispatch mechanism never
    # scoped machines to a line either. required_machine_type is the same
    # here; nothing about "line" is even represented in the input anymore.
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'single-needle lockstitch'),
    ])
    op_map = {'1_1': 0, '2_1': 1}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape[1] == 2  # (0,1) and (1,0) -- connected, unlike the old pooling


def test_operations_of_different_types_are_not_connected():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),    # -> body assembly
        (2, 1, 'overlock serger (4-thread)'),  # -> seam edge overlock
    ])
    op_map = {'1_1': 0, '2_1': 1}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape[1] == 0


def test_a_pool_of_three_gets_all_pairs_no_self_loops():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'single-needle lockstitch'),
        (3, 1, 'single-needle lockstitch'),
    ])
    op_map = {'1_1': 0, '2_1': 1, '3_1': 2}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    pairs = _pairs(edges)
    assert pairs == {(0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)}
    assert not any(s == d for s, d in pairs)  # no self-loops


def test_a_larger_pool_produces_the_full_vectorized_all_pairs_set():
    # Regression guard for the np.triu_indices vectorization specifically
    # (pool sizes now run into the thousands in production; this checks
    # correctness at a size a naive double loop can still verify against).
    n = 6
    routings = _routings([(i, 1, 'single-needle lockstitch') for i in range(n)])
    op_map = {f'{i}_1': i for i in range(n)}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    pairs = _pairs(edges)

    expected = {(i, j) for i in range(n) for j in range(n) if i != j}
    assert pairs == expected
    assert edges.shape[1] == n * (n - 1)


# ---------------------------------------------------------------------------
# Exclusions and edge cases
# ---------------------------------------------------------------------------

def test_operations_not_in_op_mapping_are_excluded():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'single-needle lockstitch'),
    ])
    op_map = {'1_1': 0}  # order 2's op deliberately omitted

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape[1] == 0


def test_unmapped_required_machine_type_is_excluded_not_a_crash():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'some-unmapped-machine-class'),
    ])
    op_map = {'1_1': 0, '2_1': 1}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape[1] == 0


def test_empty_routings_returns_empty_not_a_crash():
    empty_routings = _routings([])
    op_map = {'1_1': 0}

    edges = build_disjunctive_edges(empty_routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape == (2, 0)


def test_empty_op_mapping_returns_empty_not_a_crash():
    routings = _routings([(1, 1, 'single-needle lockstitch')])
    edges = build_disjunctive_edges(routings, {}, legacy_mode='legacy_company_wide')
    assert edges.shape == (2, 0)


def test_a_single_operation_pool_produces_no_edges():
    routings = _routings([(1, 1, 'single-needle lockstitch')])
    op_map = {'1_1': 0}

    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.shape[1] == 0


def test_edge_index_dtype_is_long():
    routings = _routings([
        (1, 1, 'single-needle lockstitch'),
        (2, 1, 'single-needle lockstitch'),
    ])
    op_map = {'1_1': 0, '2_1': 1}
    edges = build_disjunctive_edges(routings, op_map, legacy_mode='legacy_company_wide')
    assert edges.dtype == torch.long
