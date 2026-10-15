"""
Regression tests for graph/node_features.py::build_worker_node_features --
specifically the efficiency-fallback bug found during the graph structure
audit (see chat) and fixed immediately after.

BUG: the missing-data fallback for a worker with zero Operator_Skill_Matrix
rows was the literal constant 0.8, applied BEFORE any normalization -- i.e.
mixed directly into a column whose real values (efficiency_score, from
Operator_Skill_Matrix.efficiency_rating) run 70-98 on their own raw scale.
Every Line_Supervisor and Mechanic hits this fallback by construction
(skills are only ever assigned to Operators) -- measured on the live
database: 837 of 15,066 workers (5.6%). The result wasn't just wrong-scale,
it placed those 837 workers ~83 points below the real population's own
minimum, making them look like the least efficient workers in the entire
graph in a column where no real worker is anywhere close.

FIX: normalize the real column by /100 first (matching this feature
vector's other roughly-0-1 columns), then fall back to the real
population's own mean (0.84, ~84/100) for workers with no skill-matrix
evidence -- same scale, same neighbourhood as their real peers.
"""

import pandas as pd
import pytest

from graph.node_features import build_worker_node_features


def _workers(rows):
    # A padding 'Absent' row is always appended so pd.get_dummies sees both
    # status values and produces two same-dtype (bool) columns -- a single-
    # status batch hits an unrelated, pre-existing dtype quirk in
    # build_worker_node_features (status_dummies ends up mixing a bool
    # column with a literal-0 int column when a status value never
    # appears), which is not what these tests are checking. Out of scope
    # for the efficiency-fallback fix; sidestepped here rather than fixed.
    padded = rows + [{'worker_id': '__pad__', 'status': 'Absent',
                       'efficiency_score': 80.0, 'skill_breadth': 1}]
    return pd.DataFrame(padded)


def test_real_efficiency_score_is_normalized_to_roughly_0_1():
    df = _workers([
        {'worker_id': 1, 'status': 'Present', 'efficiency_score': 84.0, 'skill_breadth': 2},
    ])
    x, mapping = build_worker_node_features(df)
    efficiency_col = x[mapping[1], 2].item()  # [Present, Absent, efficiency, skill]
    assert efficiency_col == pytest.approx(0.84)


def test_missing_efficiency_score_falls_back_to_this_builds_observed_mean():
    # A worker with NaN efficiency_score (no Operator_Skill_Matrix rows) --
    # exactly the Line_Supervisor/Mechanic case. The fallback is now DERIVED
    # from the workers actually present in this build, not the frozen 0.84
    # measured on a database that has since been reseeded.
    df = _workers([
        {'worker_id': 1, 'status': 'Present', 'efficiency_score': 70.0, 'skill_breadth': 1},
        {'worker_id': 2, 'status': 'Present', 'efficiency_score': 90.0, 'skill_breadth': 3},
        {'worker_id': 3, 'status': 'Present', 'efficiency_score': float('nan'), 'skill_breadth': 0},
    ])
    x, mapping = build_worker_node_features(df)
    # NOTE: _workers() appends a padding row at 80.0 (see its docstring), so
    # the observed population here is (0.70, 0.90, 0.80) -> mean 0.80.
    assert x[mapping[3], 2].item() == pytest.approx(0.80)
    # ...and it tracks the data, not a constant: shift the population up.
    df2 = _workers([
        {'worker_id': 1, 'status': 'Present', 'efficiency_score': 90.0, 'skill_breadth': 1},
        {'worker_id': 2, 'status': 'Present', 'efficiency_score': 98.0, 'skill_breadth': 3},
        {'worker_id': 3, 'status': 'Present', 'efficiency_score': float('nan'), 'skill_breadth': 0},
    ])
    x2, m2 = build_worker_node_features(df2)
    # observed (0.90, 0.98, 0.80 pad) -> mean 0.8933: the fallback MOVED with
    # the data, which is the whole point of deriving rather than hardcoding.
    assert x2[m2[3], 2].item() == pytest.approx(0.8933, abs=1e-4)


def test_fallback_lands_in_the_same_range_as_real_workers_not_far_below_it():
    # The core of the bug: 0.8 on the OLD unnormalized column sat ~83 points
    # below the real 70-98 range. Post-fix, the fallback must sit WITHIN the
    # real population's normalized range (0.70-0.98), not far outside it.
    df = _workers([
        {'worker_id': 1, 'status': 'Present', 'efficiency_score': 70.0, 'skill_breadth': 1},  # real min
        {'worker_id': 2, 'status': 'Present', 'efficiency_score': 98.0, 'skill_breadth': 1},  # real max
        {'worker_id': 3, 'status': 'Present', 'efficiency_score': float('nan'), 'skill_breadth': 0},  # fallback
    ])
    x, mapping = build_worker_node_features(df)
    real_min = x[mapping[1], 2].item()
    real_max = x[mapping[2], 2].item()
    fallback = x[mapping[3], 2].item()
    assert real_min <= fallback <= real_max, (
        f"fallback {fallback} must sit within the real range [{real_min}, {real_max}], "
        "not below it the way the old 0.8-on-raw-scale bug did"
    )


def test_efficiency_score_column_entirely_absent_still_uses_the_matching_scale_fallback():
    # No 'efficiency_score' column at all (e.g. an older/different caller) --
    # must not crash, and must still fall back to the same-scale constant,
    # not the old 0.8. Built directly (not via _workers()) since this case
    # is specifically about the column being absent altogether.
    df = pd.DataFrame([
        {'worker_id': 1, 'status': 'Present', 'skill_breadth': 1},
        {'worker_id': '__pad__', 'status': 'Absent', 'skill_breadth': 1},
    ])
    x, mapping = build_worker_node_features(df)
    assert x[mapping[1], 2].item() == pytest.approx(0.84)


def test_mixed_real_and_missing_workers_each_get_their_own_correct_value():
    df = _workers([
        {'worker_id': 1, 'status': 'Present', 'efficiency_score': 90.0, 'skill_breadth': 3},
        {'worker_id': 2, 'status': 'Present', 'efficiency_score': float('nan'), 'skill_breadth': 0},
    ])
    x, mapping = build_worker_node_features(df)
    assert x[mapping[1], 2].item() == pytest.approx(0.90)
    # Observed (0.90, 0.80 pad) -> derived fallback 0.85.
    assert x[mapping[2], 2].item() == pytest.approx(0.85)
