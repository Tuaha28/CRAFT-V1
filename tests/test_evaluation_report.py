"""
Tests for the standing evaluation report.

Uses fabricated data throughout -- no GNN inference, no database -- so this
suite stays fast and runs independent of any live pipeline state. What it
checks: the report is generated from EXACTLY the data handed to it (no
silent re-derivation), plot failures degrade gracefully instead of crashing
the caller, and both output files always get written.
"""

import json
import os

import pytest

from reporting.evaluation_report import generate_evaluation_report
from utils.config import OUTPUT_DIR


FAKE_CURVES = {
    'positive_rate': 0.34,
    'n_samples': 1000,
    'precision': [0.34, 0.34, 0.34, 1.0],
    'recall': [1.0, 0.6, 0.3, 0.0],
    'pr_thresholds': [0.1, 0.4, 0.7],
    'average_precision': 0.35,
    'fpr': [0.0, 0.5, 1.0],
    'tpr': [0.0, 0.5, 1.0],
    'roc_thresholds': [1.0, 0.5, 0.0],
    'roc_auc': 0.501,
    'y_true': [0, 1, 0, 1],
    'y_prob': [0.3, 0.4, 0.35, 0.33],
}

FAKE_GNN_METRICS = {
    'delay_accuracy': 0.81, 'delay_precision': 0.62, 'delay_recall': 0.55, 'delay_f1': 0.58,
    'completion_mae_minutes': 42.1, 'completion_rmse_minutes': 60.3, 'priority_mae': 0.09,
}

FAKE_LOGBOOK = [
    {'gen': 0, 'nevals': 50, 'avg': 700.0, 'min': 650.0},
    {'gen': 1, 'nevals': 40, 'avg': 690.0, 'min': 649.0},
    {'gen': 2, 'nevals': 38, 'avg': 685.0, 'min': 649.0},
]

FAKE_WINDOWS = [
    {'window': 1, 'on_time_pct': 100.0, 'local_search_gain_pct': 0.12},
    {'window': 2, 'on_time_pct': 80.7, 'local_search_gain_pct': 0.0},
]

FAKE_AGGREGATE = {
    'commit_size': 300, 'lookahead_size': 600,
    'on_time_pct': 72.8, 'orders_late': 343,
    'mean_tardiness_h': 299.7, 'max_tardiness_h': 872.1,
    'makespan_days': 70.9, 'throughput_orders_per_day': 17.8,
    'labour_utilisation_pct': 37.0,
    'labour_minutes_by_shift': {'Morning': 12000.0, 'Evening': 11000.0, 'Night': 9000.0},
    'labour_minutes_by_sourcing_tier': {'batch': 8000.0, 'factory': 20000.0, 'anywhere': 4000.0},
}


def fake_rolling_result():
    return {
        'aggregate': dict(FAKE_AGGREGATE),
        'representative_logbook': list(FAKE_LOGBOOK),
        'windows': [dict(w) for w in FAKE_WINDOWS],
        'export_summaries': [],
    }


def test_report_writes_json_and_text(tmp_path, monkeypatch):
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))
    report = generate_evaluation_report(fake_rolling_result(), delay_curves=FAKE_CURVES, gnn_metrics=FAKE_GNN_METRICS,
                                        write_plots=False)
    assert os.path.exists(tmp_path / 'evaluation_report.json')
    assert os.path.exists(tmp_path / 'evaluation_report.txt')
    assert report['standard_metrics']['on_time_pct'] == 72.8


def test_report_uses_the_data_it_was_given_not_recomputed_values(tmp_path, monkeypatch):
    # The whole point of this module: the numbers in the report must be
    # exactly what was passed in, not silently re-derived.
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))
    report = generate_evaluation_report(fake_rolling_result(), delay_curves=FAKE_CURVES, gnn_metrics=FAKE_GNN_METRICS,
                                        write_plots=False)
    with open(tmp_path / 'evaluation_report.json') as f:
        on_disk = json.load(f)
    assert on_disk['standard_metrics']['makespan_days'] == 70.9
    assert on_disk['standard_metrics']['throughput_orders_per_day'] == 17.8
    assert on_disk['delay_classifier_curves']['average_precision'] == 0.35
    assert on_disk['ga_convergence'] == FAKE_LOGBOOK
    assert on_disk['windows'] == FAKE_WINDOWS


def test_plots_are_written_when_matplotlib_is_available(tmp_path, monkeypatch):
    pytest.importorskip('matplotlib')
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))
    report = generate_evaluation_report(fake_rolling_result(), delay_curves=FAKE_CURVES, gnn_metrics=FAKE_GNN_METRICS,
                                        write_plots=True)
    for key in ('pr_curve', 'roc_curve', 'ga_convergence', 'local_search_gain',
               'labour_by_shift', 'labour_by_sourcing'):
        assert key in report['plots'], f"missing plot: {key}"
        assert os.path.exists(report['plots'][key])


def test_missing_labour_breakdown_does_not_crash(tmp_path, monkeypatch):
    # Schedule stages run with export=False (e.g. inside a sweep) have no
    # labour-by-shift/sourcing data. The report must still generate.
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))
    result = fake_rolling_result()
    del result['aggregate']['labour_minutes_by_shift']
    del result['aggregate']['labour_minutes_by_sourcing_tier']
    report = generate_evaluation_report(result, delay_curves=FAKE_CURVES,
                                        gnn_metrics=FAKE_GNN_METRICS, write_plots=True)
    assert 'labour_by_shift' not in report['plots']
    assert 'labour_by_sourcing' not in report['plots']


def test_missing_delay_curves_does_not_crash(tmp_path, monkeypatch):
    # A missing/failed checkpoint gives compute_delay_curves()/evaluate_gnn()
    # -> None. Mocked at the source rather than exercising the real path: the
    # real versions build a graph and hit the live database, which is not a
    # unit test's business and would silently make this test both slow and
    # environment-dependent.
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))
    monkeypatch.setattr('models.evaluate.compute_delay_curves', lambda checkpoint_path=None: None)
    monkeypatch.setattr('models.evaluate.evaluate_gnn', lambda checkpoint_path=None: None)
    report = generate_evaluation_report(fake_rolling_result(), delay_curves=None,
                                        gnn_metrics=None,
                                        checkpoint_path='/nonexistent/path.pth',
                                        write_plots=True)
    assert report['delay_classifier_curves'] is None
    assert report['gnn_metrics'] is None
    assert 'pr_curve' not in report['plots']
    assert os.path.exists(tmp_path / 'evaluation_report.json')


def test_plot_failure_degrades_to_json_and_text_only(tmp_path, monkeypatch):
    monkeypatch.setattr('reporting.evaluation_report.OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr('reporting.evaluation_report.PLOTS_DIR', str(tmp_path / 'plots'))

    def boom(_curves):
        raise RuntimeError("simulated plotting backend failure")
    monkeypatch.setattr('reporting.evaluation_report._plot_pr_curve', boom)

    report = generate_evaluation_report(fake_rolling_result(), delay_curves=FAKE_CURVES, gnn_metrics=FAKE_GNN_METRICS,
                                        write_plots=True)
    assert os.path.exists(tmp_path / 'evaluation_report.json')
    assert os.path.exists(tmp_path / 'evaluation_report.txt')
    assert report['plots'] == {}, "a plotting failure must not leave partial plot paths behind"
