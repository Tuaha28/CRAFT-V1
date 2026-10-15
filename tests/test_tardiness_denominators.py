"""
Tardiness must name its denominator, and both denominators must be available.

The reporting defect: per-window mean_tardiness_h averages over LATE orders
only, and the whole-book aggregation weighted those means by ALL committed
orders. The result was neither the whole-book mean over all orders nor the
mean among late orders -- a hybrid of no standard definition, presented as
though it were one of them.

Also pinned here: tardiness is measured against the EFFECTIVE deadline
(ga/fitness.py clamps a deadline that already passed before the origin up to
the origin), so it excludes pre-existing overdue time.
"""

import pytest

from ga.fitness import evaluate_schedule


def _score(order_deadlines, **over):
    op_keys = ['1_1', '2_1', '3_1', '4_1']
    op_details = {k: {'sam': 10.0, 'target_quantity': 60, 'eligible_lines': [1],
                      'production_order_id': k.split('_')[0]} for k in op_keys}
    gnn = {k: {'delay': 0.5, 'priority': 0.5} for k in op_keys}
    individual = list(op_keys) + [1] * len(op_keys) + [1] * len(op_keys)
    kwargs = dict(
        all_line_ids=[1], order_deadlines=order_deadlines,
        order_release_minutes={o: 0.0 for o in order_deadlines},
        machine_pools={k: {1: [1, 2, 3]} for k in op_keys},
        machine_line_map={1: 1, 2: 1, 3: 1},
        usable_machine_count=3, present_operator_count=20,
        op_sequence_map={k: i for i, k in enumerate(op_keys)},
        return_details=True)
    kwargs.update(over)
    return evaluate_schedule(individual, len(op_keys), op_details, gnn, **kwargs)[1]


def test_mean_among_late_and_mean_over_all_orders_are_different_quantities():
    # Two orders wildly late, two comfortably on time. The mean among late
    # orders must exceed the mean over all orders by exactly the late
    # fraction -- if a report quotes one as the other it overstates or
    # understates by that factor.
    det = _score({'1': 0.0, '2': 0.0, '3': 10_000_000.0, '4': 10_000_000.0})
    late_vals = [v for v in det['order_lateness_hours'].values() if v > 0]
    all_vals = list(det['order_lateness_hours'].values())
    assert len(late_vals) < len(all_vals), "fixture must have on-time orders too"
    mean_late = sum(late_vals) / len(late_vals)
    mean_all = sum(late_vals) / len(all_vals)
    assert mean_all < mean_late
    assert mean_all == pytest.approx(mean_late * len(late_vals) / len(all_vals))
    assert det['mean_tardiness_hours'] == pytest.approx(mean_late)


def test_tardiness_is_measured_against_the_clamped_effective_deadline():
    # An order whose deadline is 1,000,000 minutes in the PAST at the origin
    # is treated as due at the origin, not as already 1,000,000 late. So its
    # recorded tardiness is only what this schedule adds -- far less than the
    # pre-existing breach.
    det = _score({'1': -1_000_000.0, '2': -1_000_000.0,
                  '3': -1_000_000.0, '4': -1_000_000.0})
    worst = max(det['order_lateness_hours'].values())
    assert worst < 1_000_000 / 60.0 / 100, \
        "pre-existing overdue time must be excluded by the max(deadline, 0) clamp"


def test_an_already_overdue_order_finished_promptly_scores_near_zero():
    det = _score({'1': -500_000.0, '2': -500_000.0, '3': -500_000.0, '4': -500_000.0})
    # Every order is enormously past its ORIGINAL date, yet tardiness against
    # the effective deadline is small. This is the reading error the report
    # now warns about explicitly.
    assert max(det['order_lateness_hours'].values()) < 100.0


def test_aggregate_exposes_both_denominators_under_explicit_names():
    import inspect
    from ga import rolling_horizon
    for fn in (rolling_horizon.run_rolling_horizon,
               rolling_horizon.run_rolling_horizon_baseline):
        src = inspect.getsource(fn)
        assert "'mean_tardiness_h_late_orders_only'" in src, fn.__name__
        assert "'mean_tardiness_h_all_orders'" in src, fn.__name__


def test_order_weighted_on_time_equals_pooled_rate_for_disjoint_windows():
    # The algebraic identity the corrected report now asserts: when windows
    # are disjoint, cover every order once, and each window's rate is taken
    # over exactly its own committed orders, weighting by committed_orders
    # re-pools the same numerator and denominator.
    windows = [(300, 22.3), (300, 79.7), (300, 96.7), (11, 100.0)]
    total = sum(n for n, _ in windows)
    weighted = sum(n * pct for n, pct in windows) / total
    on_time_orders = sum(n * pct / 100.0 for n, pct in windows)
    pooled = 100.0 * on_time_orders / total
    assert weighted == pytest.approx(pooled, rel=1e-12)


def test_weighting_a_late_only_mean_by_all_orders_is_not_either_denominator():
    # Why the hybrid was removed: it equals neither standard figure.
    windows = [  # (committed_orders, late_orders, mean_tardiness_among_late)
        (300, 233, 48.08), (300, 61, 132.96), (300, 10, 60.48), (11, 0, 0.0)]
    total_orders = sum(n for n, _, _ in windows)
    total_late = sum(l for _, l, _ in windows)
    total_hours = sum(l * m for _, l, m in windows)
    hybrid = sum(n * m for n, _, m in windows) / total_orders
    mean_all = total_hours / total_orders
    mean_late = total_hours / total_late
    assert hybrid != pytest.approx(mean_all, rel=1e-6)
    assert hybrid != pytest.approx(mean_late, rel=1e-6)


def test_user_facing_reports_name_effective_deadline_tardiness():
    from pathlib import Path
    final = Path("run_final_pipeline.py").read_text(encoding="utf-8")
    standing = Path("reporting/evaluation_report.py").read_text(encoding="utf-8")
    recommendation = Path("agent/decision_engine.py").read_text(encoding="utf-8")
    assert "mean added tardiness" in final
    assert "effective deadline" in final
    assert "mean added tardiness" in standing
    assert "effective deadline" in standing
    assert "mean schedule-added tardiness" in recommendation
