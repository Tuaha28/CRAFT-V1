"""
Tests for the evaluation metrics added for the paper: R2 + baselines on the
two regression heads, and the delay head's operating-point/top-k/calibration
reporting.

Each test is written to FALSIFY the metric rather than confirm it: known
inputs with hand-computable answers, degenerate inputs that would produce a
nan or a misleading number if the guards were removed, and a pinned check
that R2 is NOT computed for the classifier head.
"""

import numpy as np
import pytest

from models.evaluate import (
    COMPLETION_METRIC_SEMANTICS,
    DELAY_THRESHOLDS,
    DELAY_TOP_K_FRACTIONS,
    delay_operating_points,
    regression_metrics,
)


# ---------------------------------------------------------------------------
# regression_metrics
# ---------------------------------------------------------------------------

def test_perfect_prediction_scores_r2_one_and_zero_error():
    y = np.array([1.0, 2.0, 3.0, 4.0])
    m = regression_metrics(y, y)
    assert m['mae'] == pytest.approx(0.0)
    assert m['rmse'] == pytest.approx(0.0)
    assert m['r2'] == pytest.approx(1.0)


def test_mean_predictor_scores_r2_zero_and_matches_its_own_baseline():
    # R2 is DEFINED as improvement over the mean predictor, so predicting the
    # mean must score exactly 0.0 and its error must equal the reported
    # baseline. If these two ever disagree the baseline is being computed
    # from something other than the mean, making R2 uninterpretable.
    y = np.array([1.0, 2.0, 3.0, 10.0])
    m = regression_metrics(y, np.full_like(y, y.mean()))
    assert m['r2'] == pytest.approx(0.0)
    assert m['mae'] == pytest.approx(m['baseline_mae_mean_predictor'])
    assert m['rmse'] == pytest.approx(m['baseline_rmse_mean_predictor'])


def test_worse_than_mean_predictor_scores_negative_r2():
    # A model can be worse than the mean. R2 must be allowed to go negative
    # rather than being clamped, or a failing head reads as a mediocre one.
    y = np.array([1.0, 2.0, 3.0, 4.0])
    m = regression_metrics(y, np.array([100.0, -100.0, 100.0, -100.0]))
    assert m['r2'] < 0


def test_hand_computed_values():
    y = np.array([0.0, 10.0])
    p = np.array([2.0, 8.0])
    m = regression_metrics(y, p)
    assert m['mae'] == pytest.approx(2.0)            # (|2| + |2|) / 2
    assert m['rmse'] == pytest.approx(2.0)           # sqrt((4 + 4) / 2)
    # SS_res = 8, SS_tot = 50 -> 1 - 8/50
    assert m['r2'] == pytest.approx(0.84)


def test_constant_label_reports_r2_none_not_nan():
    # Zero label variance makes R2's denominator 0. sklearn returns 0.0 or
    # nan depending on version; both read as a real score in a table.
    m = regression_metrics(np.array([5.0, 5.0, 5.0]), np.array([5.0, 4.0, 6.0]))
    assert m['r2'] is None
    assert m['mae'] is not None


def test_single_sample_reports_r2_none():
    m = regression_metrics(np.array([3.0]), np.array([2.0]))
    assert m['r2'] is None
    assert m['mae'] == pytest.approx(1.0)


def test_empty_head_reports_every_field_none():
    m = regression_metrics(np.array([]), np.array([]))
    assert set(m.values()) == {None}


def test_accepts_column_vectors_like_the_model_emits():
    # The heads return shape (N, 1); a silent broadcast against a (N,) label
    # would compute an N x N error matrix and a meaningless mean.
    y = np.array([[1.0], [2.0], [3.0]])
    p = np.array([[1.5], [2.5], [3.5]])
    assert regression_metrics(y, p)['mae'] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# delay_operating_points
# ---------------------------------------------------------------------------

def _split(n_pos, n_neg, pos_prob, neg_prob):
    y = np.concatenate([np.ones(n_pos), np.zeros(n_neg)])
    p = np.concatenate([np.full(n_pos, pos_prob), np.full(n_neg, neg_prob)])
    return y, p


def test_positive_rate_is_measured_from_the_scored_split():
    # Must come from the labels passed in -- NOT from the training run's
    # pos_weight. Everything else (AP baseline, every top-k lift) is judged
    # against it, so an inherited value would misstate all of them.
    y, p = _split(25, 75, 0.9, 0.1)
    assert delay_operating_points(y, p)['positive_rate'] == pytest.approx(0.25)
    assert delay_operating_points(y, p)['n_positive'] == 25
    assert delay_operating_points(y, p)['n_negative'] == 75


def test_perfectly_separated_scores_ap_and_auc_one():
    y, p = _split(30, 70, 0.9, 0.1)
    m = delay_operating_points(y, p)
    assert m['average_precision'] == pytest.approx(1.0)
    assert m['roc_auc'] == pytest.approx(1.0)


def test_threshold_sweep_covers_every_configured_threshold():
    y, p = _split(30, 70, 0.9, 0.1)
    sweep = delay_operating_points(y, p)['threshold_sweep']
    assert [r['threshold'] for r in sweep] == [float(t) for t in DELAY_THRESHOLDS]


def test_threshold_sweep_tracks_the_threshold_not_just_the_default():
    # The whole reason the sweep exists: a head can predict everything
    # positive at a low threshold and nothing at a high one, which the 0.5
    # row alone cannot show.
    y, p = _split(30, 70, 0.9, 0.1)
    rows = {r['threshold']: r for r in delay_operating_points(y, p)['threshold_sweep']}
    assert rows[0.10]['n_predicted_positive'] == 100      # everything clears 0.1
    assert rows[0.50]['n_predicted_positive'] == 30       # only the positives
    assert rows[0.90]['n_predicted_positive'] == 30       # >= is inclusive
    assert rows[0.50]['precision'] == pytest.approx(1.0)
    assert rows[0.10]['precision'] == pytest.approx(0.30)


def test_a_head_that_ranks_well_but_never_crosses_point_five_is_still_visible():
    # THE distinction this reporting exists to make: zero positives at the
    # default threshold, yet perfect ranking. Accuracy/precision/F1 at 0.5
    # all collapse; AP, ROC-AUC and top-k must not.
    y, p = _split(20, 80, 0.40, 0.30)
    m = delay_operating_points(y, p)
    rows = {r['threshold']: r for r in m['threshold_sweep']}
    assert rows[0.50]['n_predicted_positive'] == 0
    assert rows[0.50]['f1'] == 0.0
    assert m['average_precision'] == pytest.approx(1.0)
    assert m['roc_auc'] == pytest.approx(1.0)
    assert m['top_k'][0]['precision'] == pytest.approx(1.0)


def test_top_k_precision_and_lift_against_the_measured_base_rate():
    y, p = _split(10, 90, 0.9, 0.1)
    m = delay_operating_points(y, p)
    top = {r['fraction']: r for r in m['top_k']}
    assert [r['fraction'] for r in m['top_k']] == [float(f) for f in DELAY_TOP_K_FRACTIONS]
    # Top 5% of 100 = 5 items, all drawn from the 10 positives.
    assert top[0.05]['k'] == 5
    assert top[0.05]['precision'] == pytest.approx(1.0)
    assert top[0.05]['lift'] == pytest.approx(1.0 / 0.10)     # 10x over base
    assert top[0.05]['recall'] == pytest.approx(0.5)          # 5 of 10 positives


def test_top_k_never_requests_zero_items():
    # round(4 * 0.01) == 0; k must clamp to 1 or the slice is empty and the
    # mean of an empty array is nan.
    y, p = _split(2, 2, 0.9, 0.1)
    assert all(r['k'] >= 1 for r in delay_operating_points(y, p)['top_k'])


def test_probability_spread_is_reported_as_sharpness():
    y, p = _split(20, 80, 0.73, 0.325)
    m = delay_operating_points(y, p)
    assert m['prob_min'] == pytest.approx(0.325)
    assert m['prob_max'] == pytest.approx(0.73)
    assert 0.325 <= m['prob_mean'] <= 0.73


def test_single_class_split_reports_none_rather_than_raising():
    # AP/ROC-AUC are undefined with one class present; sklearn raises.
    y = np.zeros(10)
    m = delay_operating_points(y, np.linspace(0.1, 0.9, 10))
    assert m['average_precision'] is None
    assert m['roc_auc'] is None
    assert m['positive_rate'] == 0.0
    assert m['top_k'][0]['lift'] is None      # no base rate to divide by
    assert m['top_k'][0]['recall'] is None


def test_empty_split_returns_none():
    assert delay_operating_points(np.array([]), np.array([])) is None


# ---------------------------------------------------------------------------
# Wiring into evaluate_gnn
# ---------------------------------------------------------------------------

def _fake_predictions():
    rng = np.random.default_rng(0)
    n = 200
    y_delay = (rng.random(n) < 0.3).astype(float)
    return {
        'delay_probs': np.clip(0.45 + 0.1 * y_delay + 0.02 * rng.standard_normal(n), 0, 1),
        'delay_true': y_delay,
        'n_delay_masked': 5,
        'completion_pred': (rng.random((n, 1)) * 100),
        'completion_true': (rng.random((n, 1)) * 100),
        'n_completion_masked': 3,
        'priority_pred': rng.random((n, 1)),
        'priority_true': rng.random((n, 1)),
    }


def test_evaluate_gnn_emits_the_full_table_in_one_call(monkeypatch):
    # AP and ROC-AUC used to be reachable only via compute_delay_curves(),
    # which meant a second full graph build -- so the headline table went out
    # without them. One call must now produce everything.
    import models.evaluate as ev
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: _fake_predictions())
    m = ev.evaluate_gnn('unused')
    for key in ('delay_average_precision', 'delay_roc_auc', 'delay_positive_rate',
                'delay_threshold_sweep', 'delay_top_k', 'delay_prob_min',
                'delay_prob_max', 'delay_prob_mean', 'delay_prob_std',
                'completion_r2', 'completion_baseline_mae_mean_predictor',
                'priority_r2', 'priority_baseline_mae_mean_predictor'):
        assert m[key] is not None, key
    # Pre-existing keys must survive -- reporting/evaluation_report.py reads them.
    for key in ('delay_accuracy', 'delay_precision', 'delay_recall', 'delay_f1',
                'completion_mae_minutes', 'completion_rmse_minutes', 'priority_mae',
                'delay_labelled_operations', 'delay_masked_operations',
                'completion_labelled_operations', 'completion_masked_operations'):
        assert key in m, key


def test_no_r2_is_reported_for_the_classifier_head(monkeypatch):
    # Pinned deliberately: the delay head is a binary classifier and R2
    # against a 0/1 label is not a meaningful summary of it. If someone adds
    # delay_r2 for symmetry with the other two heads, this fails.
    import models.evaluate as ev
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: _fake_predictions())
    m = ev.evaluate_gnn('unused')
    assert not [k for k in m if k.startswith('delay') and k.endswith('r2')]


def test_completion_metrics_carry_the_plan_reproduction_caveat(monkeypatch):
    # The caveat has to travel WITH the number: a report generator reads the
    # dict, not this module's docstrings, and a high completion R2 is fidelity
    # to the plan rather than forecast accuracy.
    import models.evaluate as ev
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: _fake_predictions())
    m = ev.evaluate_gnn('unused')
    assert m['completion_metric_semantics'] == COMPLETION_METRIC_SEMANTICS
    assert 'plan reproduction' in m['completion_metric_semantics']
    assert 'NOT forecast error' in m['completion_metric_semantics']


def test_fully_masked_heads_stay_unavailable_rather_than_nan(monkeypatch):
    import models.evaluate as ev
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: {
        'delay_probs': np.array([]), 'delay_true': np.array([]), 'n_delay_masked': 2,
        'completion_pred': np.empty((0, 1)), 'completion_true': np.empty((0, 1)),
        'n_completion_masked': 2,
        'priority_pred': np.array([[.5]]), 'priority_true': np.array([[.5]])})
    m = ev.evaluate_gnn('unused')
    for key in ('delay_average_precision', 'delay_roc_auc', 'delay_positive_rate',
                'delay_threshold_sweep', 'delay_top_k', 'completion_mae_minutes',
                'completion_r2'):
        assert m[key] is None, key


def test_curve_path_and_metrics_path_agree_on_ap_and_auc(monkeypatch):
    # One definition, two consumers. These are computed for the same
    # predictions by two entry points; they must not drift.
    import models.evaluate as ev
    data = _fake_predictions()
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: data)
    m = ev.evaluate_gnn('unused')
    c = ev.compute_delay_curves('unused')
    assert c['average_precision'] == pytest.approx(m['delay_average_precision'])
    assert c['roc_auc'] == pytest.approx(m['delay_roc_auc'])
    assert c['positive_rate'] == pytest.approx(m['delay_positive_rate'])


# ---------------------------------------------------------------------------
# Prior correction and calibration (FIX 4)
# ---------------------------------------------------------------------------

def test_prior_correction_matches_the_weighted_bce_optimum():
    # Under BCEWithLogitsLoss(pos_weight=w) the optimal output for a group with
    # true probability p is q* = w*p/(w*p+1-p). Feeding q* back through the
    # correction must recover p exactly -- that identity is the whole basis for
    # reading corrected outputs as probabilities.
    from models.evaluate import prior_corrected_probabilities
    w = 2.3854505971769817
    for p in (0.05, 0.2854, 0.2954, 0.5, 0.9):
        q_star = w * p / (w * p + 1 - p)
        recovered = prior_corrected_probabilities(np.array([q_star]), w)[0]
        assert recovered == pytest.approx(p, rel=1e-9)


def test_raw_half_corresponds_to_the_inverse_weight_probability():
    # The specific number the old "uncalibrated" claim missed: with w=2.3855 a
    # raw output of 0.5 means p = 1/(1+w) = 0.2954, which is essentially this
    # dataset's prevalence. A mean near 0.5 is expected, not a finding.
    from models.evaluate import prior_corrected_probabilities
    w = 2.3854505971769817
    assert prior_corrected_probabilities(np.array([0.5]), w)[0] == pytest.approx(1 / (1 + w))


def test_correction_is_monotone_so_ranking_metrics_are_unchanged():
    from models.evaluate import prior_corrected_probabilities, delay_operating_points
    rng = np.random.default_rng(3)
    q = rng.uniform(0.2, 0.8, 500)
    y = (rng.random(500) < q).astype(float)
    corrected = prior_corrected_probabilities(q, 2.3854505971769817)
    assert np.all(np.diff(corrected[np.argsort(q)]) >= -1e-12)
    a = delay_operating_points(y, q, pos_weight=2.3854505971769817)
    b = delay_operating_points(y, corrected)
    assert a['average_precision'] == pytest.approx(b['average_precision'])
    assert a['roc_auc'] == pytest.approx(b['roc_auc'])


def test_correction_is_a_no_op_without_a_weight():
    from models.evaluate import prior_corrected_probabilities
    q = np.array([0.1, 0.5, 0.9])
    for w in (None, 0, 0.0):
        assert np.allclose(prior_corrected_probabilities(q, w), q)


def test_a_constant_base_rate_predictor_is_perfectly_calibrated_yet_useless():
    # THE point the removed claim got backwards: calibration and sharpness are
    # different properties. This predictor has zero spread and zero
    # discrimination, and is perfectly calibrated.
    from models.evaluate import calibration_report
    rng = np.random.default_rng(11)
    y = (rng.random(4000) < 0.3).astype(float)
    cal = calibration_report(y, np.full(4000, y.mean()))
    assert cal['expected_calibration_error'] == pytest.approx(0.0, abs=1e-12)
    assert cal['mean_predicted'] == pytest.approx(cal['observed_base_rate'])


def test_a_badly_calibrated_predictor_is_detected():
    from models.evaluate import calibration_report
    rng = np.random.default_rng(12)
    y = (rng.random(4000) < 0.3).astype(float)
    cal = calibration_report(y, np.full(4000, 0.9))     # confidently wrong
    assert cal['expected_calibration_error'] > 0.5
    assert cal['brier_score'] > 0.3


def test_calibration_report_bins_and_sums_to_ece():
    from models.evaluate import calibration_report
    rng = np.random.default_rng(13)
    q = rng.uniform(0, 1, 3000)
    y = (rng.random(3000) < q).astype(float)
    cal = calibration_report(y, q)
    manual = sum(b['n'] * abs(b['gap']) for b in cal['reliability_bins']) / 3000
    assert cal['expected_calibration_error'] == pytest.approx(manual, rel=1e-9)
    assert sum(b['n'] for b in cal['reliability_bins']) == 3000


def test_single_class_split_reports_none_scores_rather_than_raising():
    from models.evaluate import calibration_report
    cal = calibration_report(np.zeros(50), np.linspace(0.1, 0.9, 50))
    assert cal['brier_score'] is None and cal['log_loss_unweighted'] is None
    assert cal['expected_calibration_error'] > 0


def test_evaluate_gnn_emits_calibration_and_corrected_probabilities(monkeypatch):
    import models.evaluate as ev
    data = _fake_predictions()
    data['delay_pos_weight'] = 2.3854505971769817
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: data)
    m = ev.evaluate_gnn('unused')
    for key in ('delay_pos_weight_used', 'delay_prob_corrected_mean',
                'delay_calibration_raw', 'delay_calibration_prior_corrected'):
        assert m[key] is not None, key
    # Correcting a mean near 0.5 must pull it toward the base rate.
    assert m['delay_prob_corrected_mean'] < m['delay_prob_mean']


# ---------------------------------------------------------------------------
# Labelling corrections (FIX 5)
# ---------------------------------------------------------------------------

def test_train_derived_constant_is_exactly_half_under_inverse_prevalence_weight():
    # Under w = n_neg/n_pos we have w*p = 1 - p, so the optimal constant is
    # q = 0.5 identically -- and when w is derived from THE SAME split being
    # scored, the val-optimal constant therefore equals 0.5 too. An earlier
    # version of this test asserted they DIFFER on such input, which
    # contradicted the identity the code documents; the grid search masked it
    # by never landing exactly on logit 0.
    from models.evaluate import weighted_constant_references
    n_pos, n_neg = 449, 1124
    w_same_split = n_neg / n_pos
    y = np.concatenate([np.ones(n_pos), np.zeros(n_neg)])
    refs = weighted_constant_references(y, w_same_split)
    assert refs['train_derived_constant'] == 0.5
    assert refs['val_optimal_constant'] == pytest.approx(0.5, abs=1e-12)
    assert refs['val_optimal_loss'] == pytest.approx(refs['train_derived_loss'], rel=1e-12)


def test_the_two_constants_diverge_when_the_weight_comes_from_another_split():
    # THE real case: pos_weight is fitted on TRAIN and the model is scored on
    # VAL, whose prevalence differs (0.2954 train vs 0.2854 val here). Then
    # the val-optimal constant is genuinely not 0.5 and its loss is strictly
    # lower -- which is why both must be reported under distinct names.
    from models.evaluate import weighted_constant_references
    w_from_train = 2.3854505971769817
    y = np.concatenate([np.ones(449), np.zeros(1124)])
    refs = weighted_constant_references(y, w_from_train)
    assert refs['val_optimal_constant'] != pytest.approx(0.5, abs=1e-3)
    assert refs['val_optimal_constant'] == pytest.approx(0.487943, abs=1e-6)
    assert refs['val_optimal_loss'] < refs['train_derived_loss']


def test_both_constant_references_are_reported_not_just_one(monkeypatch):
    import models.evaluate as ev
    data = _fake_predictions()
    data['delay_pos_weight'] = 2.3854505971769817
    monkeypatch.setattr(ev, '_load_model_and_val_predictions', lambda *a, **k: data)
    m = ev.evaluate_gnn('unused')
    for key in ('delay_constant_reference_val_optimal',
                'delay_constant_reference_val_optimal_q',
                'delay_constant_reference_train_derived',
                'delay_constant_reference_train_derived_q'):
        assert m[key] is not None, key


def test_top_k_reports_coverage_and_absolute_positive_count():
    # Precision at top-2% rests on a handful of positives. The row must carry
    # its own sample size and the share of ALL positives it covers, so it
    # cannot be read as an operational guarantee.
    y, p = _split(100, 900, 0.9, 0.1)
    m = delay_operating_points(y, p)
    top = {r['fraction']: r for r in m['top_k']}
    r = top[0.02]
    assert r['k'] == 20
    assert r['n_positive_in_k'] == 20
    assert r['coverage_of_all_positives'] == pytest.approx(20 / 100)


def test_precision_ci_requires_grouping_rather_than_assuming_independence():
    # No groups -> no interval. Silently returning an i.i.d. interval would
    # understate uncertainty, since operations within an order are correlated.
    y, p = _split(50, 450, 0.8, 0.2)
    m = delay_operating_points(y, p)
    assert all(r['precision_ci95'] is None for r in m['top_k'])


def test_cluster_bootstrap_ci_brackets_the_point_estimate():
    rng = np.random.default_rng(5)
    n_orders, per_order = 120, 8
    groups = np.repeat(np.arange(n_orders), per_order)
    q = rng.uniform(0.2, 0.8, n_orders * per_order)
    y = (rng.random(n_orders * per_order) < q).astype(float)
    m = delay_operating_points(y, q, groups=groups)
    for r in m['top_k']:
        ci = r['precision_ci95']
        assert ci is not None and ci['n_orders'] == n_orders
        assert ci['lower'] <= ci['upper']
        assert 'orders' in ci['method']


def test_cluster_bootstrap_is_wider_than_ignoring_the_grouping():
    # THE reason clustering matters: with every operation of an order sharing
    # its label, treating operations as independent multiplies the effective
    # sample size by the cluster size and shrinks the interval accordingly.
    rng = np.random.default_rng(6)
    n_orders, per_order = 80, 10
    order_pos = rng.random(n_orders) < 0.3
    groups = np.repeat(np.arange(n_orders), per_order)
    y = np.repeat(order_pos.astype(float), per_order)       # fully correlated
    q = rng.uniform(0.3, 0.7, n_orders * per_order)
    clustered = delay_operating_points(y, q, groups=groups)['top_k'][2]['precision_ci95']
    iid = delay_operating_points(y, q, groups=np.arange(len(y)))['top_k'][2]['precision_ci95']
    assert (clustered['upper'] - clustered['lower']) > (iid['upper'] - iid['lower'])


def test_regression_reports_deployable_train_baselines_separately():
    # The validation mean is R2's denominator but is fitted on the labels
    # being scored. A deployable constant must come from TRAINING targets.
    y = np.array([1.0, 2.0, 3.0, 10.0])
    train = np.array([5.0, 5.0, 6.0, 40.0])
    m = regression_metrics(y, np.array([1.0, 2.0, 3.0, 10.0]), train_targets=train)
    assert m['validation_mean_reference_mae'] == pytest.approx(
        np.abs(y - y.mean()).mean())
    assert m['train_mean_baseline_mae'] == pytest.approx(np.abs(y - train.mean()).mean())
    assert m['train_median_baseline_mae'] == pytest.approx(
        np.abs(y - np.median(train)).mean())
    # They are genuinely different numbers -- the point of separating them.
    assert m['train_mean_baseline_mae'] != pytest.approx(m['validation_mean_reference_mae'])


def test_train_baselines_are_none_when_no_train_targets_supplied():
    m = regression_metrics(np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    assert m['train_mean_baseline_mae'] is None
    assert m['train_median_baseline_mae'] is None
    assert m['validation_mean_reference_mae'] is not None


def test_val_optimal_constant_is_analytic_not_grid_searched():
    # Was found by scanning logits on a 0.0005 grid, reporting 0.4881 against
    # a true optimum of 0.487943 -- discretisation error in a reported number,
    # with its precision silently set by the grid step. Now closed-form.
    from models.evaluate import weighted_constant_references
    w = 2.3854505971769817
    y = np.concatenate([np.ones(449), np.zeros(1124)])
    r = weighted_constant_references(y, w)
    p = y.mean()
    assert r['val_optimal_constant'] == pytest.approx((w * p) / (w * p + 1 - p), rel=1e-12)
    assert r['val_optimal_is_analytic'] is True
    # It must genuinely be the minimum: perturbing either way costs more.
    import math

    def loss_at(q):
        return -(w * p * math.log(q) + (1 - p) * math.log(1 - q))
    q = r['val_optimal_constant']
    assert r['val_optimal_loss'] == pytest.approx(loss_at(q), rel=1e-12)
    for eps in (1e-3, 1e-2):
        assert loss_at(q + eps) > r['val_optimal_loss']
        assert loss_at(q - eps) > r['val_optimal_loss']


def test_analytic_loss_agrees_with_torchs_weighted_bce():
    # The closed form must reproduce what the real loss function computes,
    # not merely be self-consistent.
    import math
    import torch
    import torch.nn as nn
    from models.evaluate import weighted_constant_references
    w = 2.3854505971769817
    y = np.concatenate([np.ones(449), np.zeros(1124)])
    r = weighted_constant_references(y, w)
    t = torch.tensor(y, dtype=torch.float)
    crit = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([w]))
    q = r['val_optimal_constant']
    logit = math.log(q / (1 - q))
    assert crit(torch.full_like(t, logit), t).item() == pytest.approx(
        r['val_optimal_loss'], rel=1e-6)
    assert crit(torch.zeros_like(t), t).item() == pytest.approx(
        r['train_derived_loss'], rel=1e-6)
