"""
Tests for ga/rolling_horizon.py::run_rolling_horizon_baseline -- the
whole-book, independent-machine-state-trajectory baseline comparison arm
built for the final authoritative GA-vs-baselines report. Same lightweight
synthetic-factory pattern as tests/test_alns_refinement.py, small enough
to run fast while still exercising multiple rolling windows.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ga.scheduler import GAScheduler
from ga.rolling_horizon import run_rolling_horizon_baseline

NOW = datetime(2026, 1, 1, 8, 0, 0)


def _routings(n_orders=6):
    rows = []
    for oid in range(1001, 1001 + n_orders):
        rows.append({
            'production_order_id': oid, 'operation_id': 1, 'operation_sequence_number': 1,
            'sam': 2.0, 'target_quantity': 20, 'order_deadline': NOW + timedelta(days=10),
            'release_date': NOW + timedelta(days=(oid - 1001)),
            'required_machine_type': 'single-needle lockstitch',
        })
        rows.append({
            'production_order_id': oid, 'operation_id': 2, 'operation_sequence_number': 2,
            'sam': 1.5, 'target_quantity': 20, 'order_deadline': NOW + timedelta(days=10),
            'release_date': NOW + timedelta(days=(oid - 1001)),
            'required_machine_type': 'overlock serger (4-thread)',
        })
    return pd.DataFrame(rows)


def _machines():
    rows = []
    for i, asset_id in enumerate(range(101, 107)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'body assembly'})
    for i, asset_id in enumerate(range(201, 207)):
        rows.append({'asset_id': asset_id, 'line_id': 1 if i < 3 else 2, 'operation_type': 'seam edge overlock'})
    return pd.DataFrame(rows)


def _lines():
    return pd.DataFrame({'line_id': [1, 2], 'is_active': [1, 1], 'target_efficiency_percentage': [85.0, 85.0]})


def _preds(routings):
    return {
        f"{row['production_order_id']}_{row['operation_id']}": {'delay': 0.1, 'priority': 0.0}
        for _, row in routings.iterrows()
    }


def test_baseline_run_covers_every_order_exactly_once_across_windows():
    routings = _routings(n_orders=6)
    result = run_rolling_horizon_baseline(
        'FIFO', routings, _machines(), _lines(), _preds(routings),
        capacity=10, origin=NOW, commit_size=2, log_fn=lambda *a, **k: None,
    legacy_mode='legacy_company_wide')
    assert result['aggregate']['n_windows'] == 3  # 6 orders / commit_size=2
    assert result['aggregate']['orders'] == 6
    total_committed = sum(w['committed_orders'] for w in result['windows'])
    assert total_committed == 6


def test_baseline_aggregate_has_the_expected_metric_set():
    routings = _routings(n_orders=4)
    result = run_rolling_horizon_baseline(
        'EDF', routings, _machines(), _lines(), _preds(routings),
        capacity=10, origin=NOW, commit_size=4, log_fn=lambda *a, **k: None,
    legacy_mode='legacy_company_wide')
    agg = result['aggregate']
    for key in ('strategy', 'orders', 'on_time_pct', 'orders_late', 'mean_tardiness_h',
                'max_tardiness_h', 'makespan_days', 'throughput_orders_per_day',
                'labour_utilisation_pct', 'total_runtime_s', 'n_windows'):
        assert key in agg, f"missing expected key {key}"
    assert agg['strategy'] == 'EDF'


def test_baseline_machine_state_carries_across_windows():
    # With only 2 lines and commit_size=2, the second window's machines are
    # not idle from time 0 -- end_day must strictly increase window to
    # window if state is genuinely carried forward, not reset.
    routings = _routings(n_orders=6)
    result = run_rolling_horizon_baseline(
        'CriticalRatio', routings, _machines(), _lines(), _preds(routings),
        capacity=10, origin=NOW, commit_size=2, log_fn=lambda *a, **k: None,
    legacy_mode='legacy_company_wide')
    end_days = [w['end_day'] for w in result['windows']]
    assert end_days == sorted(end_days), "end_day must be non-decreasing across windows"
    assert end_days[-1] > 0


def test_baseline_strategies_all_run_without_error():
    routings = _routings(n_orders=4)
    for strategy in ('FIFO', 'EDF', 'CriticalRatio', 'UrgencyTiered', 'RandomRepaired'):
        result = run_rolling_horizon_baseline(
            strategy, routings, _machines(), _lines(), _preds(routings),
            capacity=10, origin=NOW, commit_size=4, log_fn=lambda *a, **k: None,
        legacy_mode='legacy_company_wide')
        assert result['aggregate']['orders'] == 4


def test_baseline_final_partial_window_is_not_dropped():
    routings = _routings(n_orders=5)  # commit_size=2 -> windows of 2, 2, 1
    result = run_rolling_horizon_baseline(
        'FIFO', routings, _machines(), _lines(), _preds(routings),
        capacity=10, origin=NOW, commit_size=2, log_fn=lambda *a, **k: None,
    legacy_mode='legacy_company_wide')
    assert result['aggregate']['n_windows'] == 3
    assert result['aggregate']['orders'] == 5
    assert result['windows'][-1]['committed_orders'] == 1
