"""
Tests for the database-layer functions that don't require a live DB.

Deliberately scoped, per instruction: the 13 fetch_*() functions in
database/queries.py are thin pd.read_sql() wrappers around real SQL against
a live MySQL DB. Every real database-layer bug this project has actually hit
(the execution_status filter that leaked the delay label into
fetch_operation_assignments, the Done-filter that corrupted the completion
label, a query against a table that had already been renamed) was SEMANTIC
-- the SQL ran fine and returned data that was wrong for its purpose, not a
malformed query. Mocking the SQL layer verifies the query STRING matches
intent, which would not have caught any of those; a live-DB integration
suite would catch them but at the cost of the whole suite's dependency-free,
20-90s run, which is why the tests actually get run in the first place. So:

  1. apply_planning_horizon() -- pure DataFrame logic, no DB access at all,
     tested directly like everything else in this suite.
  2. A source-inspection guard on the three label/feature-path queries that
     must stay unfiltered on execution_status (fetch_operation_assignments,
     fetch_dispatch_context, fetch_actual_vs_planned_timing) -- this is the
     actual repeated failure mode in this codebase (see each function's own
     docstring in database/queries.py), so it's the one thing about the SQL
     text worth asserting on directly, without needing a live DB to run it.
"""
import inspect
import re

import pandas as pd
import pytest

from database.queries import (
    apply_planning_horizon,
    fetch_operation_assignments,
    fetch_dispatch_context,
    fetch_actual_vs_planned_timing,
)


# ---------------------------------------------------------------------------
# apply_planning_horizon
# ---------------------------------------------------------------------------

def _routings(rows):
    """rows: [(order_id, release_date, operation_id), ...]"""
    return pd.DataFrame([
        {'production_order_id': oid, 'release_date': rel, 'operation_id': op_id}
        for oid, rel, op_id in rows
    ])


def test_none_horizon_returns_everything_unchanged():
    df = _routings([(1, '2026-01-01', 1), (2, '2026-01-05', 1), (2, '2026-01-05', 2)])
    out = apply_planning_horizon(df, horizon_orders=None)
    pd.testing.assert_frame_equal(out, df)


def test_empty_input_returns_empty_regardless_of_horizon():
    df = _routings([])
    out = apply_planning_horizon(df, horizon_orders=5)
    assert out.empty


def test_selects_the_earliest_released_n_orders():
    df = _routings([
        (1, '2026-01-10', 1),
        (2, '2026-01-01', 1),
        (3, '2026-01-05', 1),
    ])
    out = apply_planning_horizon(df, horizon_orders=2)
    assert set(out['production_order_id']) == {2, 3}  # earliest two by release_date


def test_keeps_every_operation_of_a_selected_order_not_just_one_row():
    df = _routings([
        (1, '2026-01-01', 1),
        (1, '2026-01-01', 2),
        (1, '2026-01-01', 3),
        (2, '2026-01-10', 1),
    ])
    out = apply_planning_horizon(df, horizon_orders=1)
    assert len(out) == 3
    assert set(out['production_order_id']) == {1}


def test_an_order_with_operations_at_different_release_dates_stays_whole():
    # Anomalous but real-world-possible data: an order's own operation rows
    # disagree on release_date. The order is windowed as a UNIT by its
    # earliest release_date -- selecting it must keep ALL its operations,
    # not just the ones whose row happens to carry the earliest date.
    df = _routings([
        (1, '2026-01-01', 1),
        (1, '2026-01-03', 2),  # same order, later release_date on this row
        (2, '2026-01-20', 1),
    ])
    out = apply_planning_horizon(df, horizon_orders=1)
    assert set(out['production_order_id']) == {1}
    assert len(out) == 2  # both of order 1's operations, not just the row with the min date


def test_ties_on_release_date_break_deterministically_by_order_id():
    df = _routings([
        (3, '2026-01-01', 1),
        (1, '2026-01-01', 1),
        (2, '2026-01-01', 1),
    ])
    out = apply_planning_horizon(df, horizon_orders=2)
    # Stable tiebreak on production_order_id -- lowest ids win the tie.
    assert set(out['production_order_id']) == {1, 2}


def test_horizon_larger_than_the_order_book_returns_everything():
    df = _routings([(1, '2026-01-01', 1), (2, '2026-01-02', 1)])
    out = apply_planning_horizon(df, horizon_orders=1000)
    assert set(out['production_order_id']) == {1, 2}


def test_horizon_of_zero_returns_nothing():
    df = _routings([(1, '2026-01-01', 1), (2, '2026-01-02', 1)])
    out = apply_planning_horizon(df, horizon_orders=0)
    assert out.empty


# ---------------------------------------------------------------------------
# Label/feature-path queries must stay unfiltered on execution_status
#
# This is the actual repeated failure mode in this codebase (documented in
# each function's own docstring): a WHERE clause keyed to execution_status,
# or to any live "now" pointer, silently reintroduces "has this operation
# started" as a confound into a query that must be blind to it. Guarded via
# source inspection of the literal query string rather than a live query, so
# it runs with no DB and catches a regression the moment the SQL changes.
# ---------------------------------------------------------------------------

_FORBIDDEN_GATES = ('execution_status', 'now()', 'curdate()', 'current_timestamp')


def _query_literal(fn) -> str:
    """Extracts the triple-quoted SQL literal assigned to `query` inside fn's
    source. Brittle-by-design: if the query is ever built some other way,
    this should fail loudly rather than silently stop checking anything."""
    source = inspect.getsource(fn)
    match = re.search(r'query\s*=\s*"""(.*?)"""', source, re.DOTALL)
    assert match, f"{fn.__name__}: could not find a `query = \"\"\"...\"\"\"` literal to inspect"
    return match.group(1).lower()


@pytest.mark.parametrize("fn", [
    fetch_operation_assignments,
    fetch_dispatch_context,
    fetch_actual_vs_planned_timing,
])
def test_label_and_leak_free_feature_queries_stay_unfiltered_on_execution_status(fn):
    sql = _query_literal(fn)
    for gate in _FORBIDDEN_GATES:
        assert gate not in sql, (
            f"{fn.__name__}'s query now contains '{gate}' -- this is exactly the leak "
            f"documented in its own docstring (an execution_status/now-gated WHERE clause "
            f"reintroduces 'has this operation started' as a confound). See "
            f"PROJECT_STATUS.txt Section 0D/0E and database/queries.py."
        )
