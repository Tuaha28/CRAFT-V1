import torch
import numpy as np
import hashlib
import json
import os
import sys
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    mean_absolute_error, root_mean_squared_error, r2_score,
    precision_recall_curve, roc_curve, average_precision_score, roc_auc_score,
    brier_score_loss, log_loss,
)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.graph_builder import FactoryGraphBuilder, SPLIT_SEED, VAL_FRACTION
from models.gnn import MultiTaskFactoryGNN
from utils.logger import get_logger
from utils.config import OUTPUT_DIR, DEVICE

logger = get_logger("gnn_evaluation", "gnn_training.log")


MANIFEST_FILENAME = "split_manifest.json"

# Operating points for the delay classifier. 0.5 alone is not a sufficient
# description of a classifier: a model can rank usefully while producing zero
# positives at the default threshold, and the reverse -- predicting positives
# freely while ranking barely above chance -- is equally possible. Reporting
# the sweep and the top-k precision separates "ranks badly" from "ranks fine
# but sits on the wrong side of 0.5", which the single 0.5 row cannot. Note
# that neither is a calibration statement -- calibration is measured
# separately by calibration_report().
DELAY_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90)
DELAY_TOP_K_FRACTIONS = (0.01, 0.02, 0.05, 0.10, 0.20)

# Head 2's target is planned_end_time.max() - planned_start_time.min() across
# an operation's lots (graph/labels.py::_aggregate_planned_span). BOTH source
# columns are planned, so every metric on this head measures how well the
# model reproduces the SCHEDULE'S OWN PLAN, not how long operations actually
# take. A high R2 here is not forecast accuracy and must never be reported as
# such. Carried in the metrics dict so the caveat travels with the number
# instead of living only in a docstring a report generator will not read.
COMPLETION_METRIC_SEMANTICS = (
    "plan reproduction (target is the PLANNED span: planned_end.max - "
    "planned_start.min); NOT forecast error against actual elapsed time"
)


class SplitManifestError(RuntimeError):
    """The recorded split does not match the graph/checkpoint being evaluated."""


def weighted_constant_references(y_true, pos_weight):
    """
    What a CONSTANT-output classifier scores under the training objective.

    Two distinct references, because they answer different questions and
    quoting one as the other misstates how much the model actually adds:

      val_optimal   -- the best constant for THIS split, found by minimising
          the weighted loss over it. A lower bound on any constant's loss
          here, but it peeks at the validation labels, so it flatters nothing
          and nobody could have deployed it.

      train_derived -- the constant a model trained under this pos_weight is
          driven toward, evaluated on the validation split. Under inverse-
          prevalence weighting (w = n_neg/n_pos) this is EXACTLY q = 0.5:
          w*p_train = 1 - p_train, so q* = w*p/(w*p + 1-p) = 0.5 identically.
          This is the deployable reference -- what you would have scored
          without a model.

    Returns None when there is nothing to score.
    """
    y_true = np.asarray(y_true, dtype=float).ravel()
    if len(y_true) == 0 or not pos_weight or pos_weight <= 0:
        return None
    w = float(pos_weight)
    p = float(y_true.mean())

    def weighted_bce_at(q):
        """Mean weighted BCE when every prediction is the constant q."""
        q = min(max(q, 1e-15), 1 - 1e-15)
        return -(w * p * np.log(q) + (1 - p) * np.log(1 - q))

    # CLOSED FORM, not a grid search. Minimising
    #   -[w*p*log q + (1-p)*log(1-q)]
    # gives q* = w*p / (w*p + 1-p) exactly. This was previously found by
    # scanning logits on a 0.0005 grid, which reported 0.4881 against a true
    # optimum of ~0.48805 -- a reported number carrying avoidable
    # discretisation error, and one whose precision the grid step silently
    # determined.
    q_star = (w * p) / (w * p + 1 - p) if (w * p + 1 - p) > 0 else 0.5
    return {
        'val_optimal_constant': float(q_star),
        'val_optimal_loss': float(weighted_bce_at(q_star)),
        'val_optimal_is_analytic': True,
        # Under inverse-prevalence weighting (w = n_neg/n_pos) we have
        # w*p_train = 1 - p_train, so the train-derived optimum is q = 0.5
        # identically -- not approximately.
        'train_derived_constant': 0.5,
        'train_derived_loss': float(weighted_bce_at(0.5)),
    }


def regression_metrics(y_true, y_pred, train_targets=None):
    """
    MAE/RMSE/R2 for a regression head, plus constant-predictor references.

    THE BASELINE IS NOT DEPLOYABLE AND IS NAMED ACCORDINGLY. R2's denominator
    is the variance about the VALIDATION targets' own mean, so
    validation_mean_reference is the right quantity for interpreting R2 -- but
    it is computed from the labels being scored, which no deployed model has.
    Quoting it as "the baseline a model must beat" overstates the bar the
    model actually cleared, because a real fallback would have to use a
    constant fitted on TRAINING data.

    train_targets, when supplied, adds exactly that: the error of predicting
    the training set's mean, and of predicting its median (the median is the
    MAE-optimal constant, so it is the honest constant-predictor bar for MAE
    specifically -- the mean is optimal for RMSE, not MAE).

    R2 is reported ALONGSIDE MAE/RMSE, never instead: R2 is scale-free and so
    comparable across heads, but it is a ratio against this split's own label
    variance, which means a head can post a respectable R2 while its absolute
    error is operationally useless (or vice versa on a low-variance split).
    The baseline is emitted with it because "R2 = 0.6" says nothing on its own
    -- R2 is defined as the improvement over predicting the mean, so the mean
    predictor's own error is the only thing that makes it interpretable.

    Deliberately NOT applied to the delay head: that head is a binary
    classifier whose output is a probability, and R2 against a 0/1 label is
    not a meaningful summary of it. Average precision and ROC-AUC are the
    relevant metrics there -- see delay_operating_points().

    Returns every value as None when there is nothing usable to score, so a
    fully-masked head reports "unavailable" rather than a nan that looks like
    a number.
    """
    y_true = np.asarray(y_true).ravel()
    y_pred = np.asarray(y_pred).ravel()
    if len(y_true) == 0:
        return {'mae': None, 'rmse': None, 'r2': None,
                'validation_mean_reference_mae': None,
                'validation_mean_reference_rmse': None,
                'train_mean_baseline_mae': None,
                'train_mean_baseline_rmse': None,
                'train_median_baseline_mae': None,
                'baseline_mae_mean_predictor': None,
                'baseline_rmse_mean_predictor': None}
    mean_pred = np.full_like(y_true, y_true.mean(), dtype=float)
    train_mean_mae = train_mean_rmse = train_median_mae = None
    if train_targets is not None:
        t = np.asarray(train_targets, dtype=float).ravel()
        if len(t):
            tm = np.full_like(y_true, float(t.mean()), dtype=float)
            tmed = np.full_like(y_true, float(np.median(t)), dtype=float)
            train_mean_mae = float(mean_absolute_error(y_true, tm))
            train_mean_rmse = float(root_mean_squared_error(y_true, tm))
            train_median_mae = float(mean_absolute_error(y_true, tmed))
    # R2 needs at least two points and non-zero label variance; with one
    # sample, or a constant label, the denominator is 0 and sklearn returns
    # nan or 0.0 depending on version. Report None rather than either.
    r2 = None
    if len(y_true) >= 2 and float(np.var(y_true)) > 0:
        r2 = float(r2_score(y_true, y_pred))
    val_mean_mae = float(mean_absolute_error(y_true, mean_pred))
    val_mean_rmse = float(root_mean_squared_error(y_true, mean_pred))
    return {
        'mae': float(mean_absolute_error(y_true, y_pred)),
        'rmse': float(root_mean_squared_error(y_true, y_pred)),
        'r2': r2,
        # R2's own denominator. Correct for interpreting R2; NOT deployable,
        # since it is fitted on the labels being scored.
        'validation_mean_reference_mae': val_mean_mae,
        'validation_mean_reference_rmse': val_mean_rmse,
        # Deployable constant predictors, fitted on TRAINING targets only.
        'train_mean_baseline_mae': train_mean_mae,
        'train_mean_baseline_rmse': train_mean_rmse,
        # The median is the MAE-optimal constant, so this is the honest
        # constant-predictor bar for MAE (the mean is optimal for RMSE).
        'train_median_baseline_mae': train_median_mae,
        # Retained under the old names for existing consumers; these are the
        # validation-mean reference, not a deployable baseline.
        'baseline_mae_mean_predictor': val_mean_mae,
        'baseline_rmse_mean_predictor': val_mean_rmse,
    }


def prior_corrected_probabilities(y_prob, pos_weight):
    """
    Undo the pos_weight tilt so the outputs can be read as probabilities.

    Under BCEWithLogitsLoss(pos_weight=w) the population-optimal output for a
    group whose true event probability is p is NOT p:

        minimise  -[w*p*log q + (1-p)*log(1-q)]   ->   q* = w*p / (w*p + 1-p)

    which rearranges to the exact identity

        logit(p) = logit(q) - log(w)

    So with w = 2.3855 an output of 0.5 corresponds to an underlying
    probability of 0.2954 -- essentially this dataset's own prevalence. A mean
    output near 0.5 is therefore the EXPECTED behaviour of the training
    objective, not evidence of miscalibration, and correcting by -log(w) is an
    analytic transform with no fitting and no data use whatsoever.

    Returns y_prob unchanged when no usable weight is supplied.
    """
    y_prob = np.asarray(y_prob, dtype=float).ravel()
    if not pos_weight or pos_weight <= 0:
        return y_prob
    eps = 1e-12
    clipped = np.clip(y_prob, eps, 1.0 - eps)
    logits = np.log(clipped / (1.0 - clipped)) - np.log(float(pos_weight))
    return 1.0 / (1.0 + np.exp(-logits))


def calibration_report(y_true, y_prob, n_bins=10):
    """
    Actually measure calibration, rather than inferring it from spread.

    Spread measures SHARPNESS -- how confidently a model separates cases --
    which is a different property entirely: a predictor that outputs the
    constant base rate for every case is perfectly calibrated and completely
    nondiscriminating, while a wide-spread predictor can be badly calibrated.
    Neither can be read off the other.

    Brier score and unweighted log loss are proper scoring rules (lower is
    better) and so are sensitive to calibration and discrimination together.
    The reliability table is the direct check: within each predicted-
    probability bin, does the observed frequency match the predicted mean?
    ECE is the bin-count-weighted mean absolute gap between the two.

    NOTE on any further correction: an analytic prior shift (see
    prior_corrected_probabilities) uses no data and may be applied here. But
    FITTING a calibrator -- Platt, isotonic -- on this same validation split
    and then reporting calibration on it would be measuring the fit on its own
    training data. That needs a separate calibration split, and is deliberately
    not done here.
    """
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_prob = np.asarray(y_prob, dtype=float).ravel()
    if len(y_true) == 0:
        return None
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(y_prob, edges[1:-1], right=False), 0, n_bins - 1)
    bins, ece = [], 0.0
    for b in range(n_bins):
        sel = idx == b
        n = int(sel.sum())
        if n == 0:
            continue
        pred_mean = float(y_prob[sel].mean())
        obs_freq = float(y_true[sel].mean())
        ece += n * abs(pred_mean - obs_freq)
        bins.append({'bin_lower': float(edges[b]), 'bin_upper': float(edges[b + 1]),
                     'n': n, 'predicted_mean': pred_mean, 'observed_frequency': obs_freq,
                     'gap': pred_mean - obs_freq})
    single_class = len(set(y_true.tolist())) < 2
    return {
        'brier_score': float(brier_score_loss(y_true, y_prob)) if not single_class else None,
        'log_loss_unweighted': (float(log_loss(y_true, np.clip(y_prob, 1e-12, 1 - 1e-12),
                                               labels=[0.0, 1.0]))
                                if not single_class else None),
        'expected_calibration_error': float(ece / len(y_true)),
        'mean_predicted': float(y_prob.mean()),
        'observed_base_rate': float(y_true.mean()),
        'reliability_bins': bins,
    }


def _precision_at_k_ci(y_true, y_prob, k, groups, n_boot=2000, seed=0):
    """
    95% CI for precision@k by CLUSTER bootstrap over production orders.

    Operations are not independent: every operation of one order shares that
    order's deadline-budget feature by construction, and the train/val split
    is itself order-level for exactly that reason. Resampling operations
    independently would treat ~8.6 correlated operations per order as 8.6
    independent observations and produce an interval several times too
    narrow. Orders are the independent unit, so orders are what gets
    resampled -- each draw takes an order and ALL of its operations.

    Returns None when no grouping is supplied, rather than silently falling
    back to an i.i.d. interval that would understate the uncertainty.
    """
    if groups is None:
        return None
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_prob = np.asarray(y_prob, dtype=float).ravel()
    groups = np.asarray(groups).ravel()
    if len(groups) != len(y_true):
        return None
    uniq = np.unique(groups)
    if len(uniq) < 2:
        return None
    members = {g: np.flatnonzero(groups == g) for g in uniq}
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        drawn = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([members[g] for g in drawn])
        kk = max(1, min(k, len(idx)))
        top = idx[np.argsort(-y_prob[idx])[:kk]]
        stats.append(float(y_true[top].mean()))
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return {'lower': float(lo), 'upper': float(hi), 'method': 'cluster bootstrap over orders',
            'n_orders': int(len(uniq)), 'n_boot': int(n_boot)}


def delay_operating_points(y_true, y_prob,
                           thresholds=DELAY_THRESHOLDS,
                           top_k_fractions=DELAY_TOP_K_FRACTIONS,
                           pos_weight=None, groups=None):
    """
    Threshold sweep, top-k precision and probability spread for the delay head.

    positive_rate is MEASURED on the split being scored -- not taken from the
    training run's pos_weight. It is the no-skill baseline that average
    precision and every top-k lift below are judged against, so reading it
    off the training split would misstate all of them whenever the two splits
    differ (they do here).

    top-k precision answers a question the threshold sweep cannot: if a
    planner can only act on the N most at-risk operations, how many of those
    are genuinely late? That is a ranking question, and it stays meaningful
    even when the probabilities are compressed into a narrow band and no
    threshold produces a sensible confusion matrix.

    prob_min/max/mean/std describe SHARPNESS -- how far the outputs spread --
    and nothing more. They are NOT evidence about calibration: a predictor
    emitting the constant base rate is perfectly calibrated and useless, and a
    wide-spread one can be badly calibrated. Calibration is measured directly
    instead, by calibration_report() below.

    A compressed band is still worth reporting, because it explains why the
    0.5 row looks the way it does -- where the band happens to sit relative to
    0.5 drives the confusion matrix far more than the head's ranking ability.
    But under pos_weight-weighted training the band's LOCATION is largely set
    by the objective itself (see prior_corrected_probabilities), so its centre
    carries little information until that tilt is removed.

    Returns None when there are no usable labels, matching the rest of this
    module's "unavailable" convention.
    """
    y_true = np.asarray(y_true).ravel()
    y_prob = np.asarray(y_prob).ravel()
    n = len(y_true)
    if n == 0:
        return None

    positive_rate = float(y_true.mean())
    sweep = []
    for t in thresholds:
        binary = (y_prob >= t).astype(float)
        sweep.append({
            'threshold': float(t),
            'n_predicted_positive': int(binary.sum()),
            'precision': float(precision_score(y_true, binary, zero_division=0)),
            'recall': float(recall_score(y_true, binary, zero_division=0)),
            'f1': float(f1_score(y_true, binary, zero_division=0)),
            'accuracy': float(accuracy_score(y_true, binary)),
        })

    order = np.argsort(-y_prob)
    total_positives = float(y_true.sum())
    top_k = []
    for frac in top_k_fractions:
        k = max(1, int(round(n * frac)))
        idx = order[:k]
        n_pos_in_k = float(y_true[idx].sum())
        precision_at_k = float(y_true[idx].mean())
        row = {
            'fraction': float(frac),
            'k': int(k),
            'threshold_at_k': float(y_prob[idx].min()),
            'precision': precision_at_k,
            # Lift is against the MEASURED base rate: "how much better than
            # picking at random from this same split".
            'lift': (precision_at_k / positive_rate) if positive_rate > 0 else None,
            # SAMPLE SIZE AND COVERAGE, stated inline. A precision computed on
            # a handful of selected operations containing a handful of
            # positives is exploratory evidence, not an operational guarantee,
            # and the top-1%/2% rows are exactly that: they capture a low
            # single-digit percentage of all positives.
            'n_positive_in_k': int(n_pos_in_k),
            'coverage_of_all_positives': (n_pos_in_k / total_positives) if total_positives > 0 else None,
            'recall': (n_pos_in_k / total_positives) if total_positives > 0 else None,
        }
        row['precision_ci95'] = _precision_at_k_ci(y_true, y_prob, k, groups)
        top_k.append(row)

    n_classes = len(set(y_true.tolist()))
    corrected = prior_corrected_probabilities(y_prob, pos_weight)
    # Ranking is invariant under the correction (it is a monotone transform of
    # the logit), so AP/ROC-AUC are deliberately reported once, from the raw
    # outputs -- recomputing them on corrected values would just restate them.
    return {
        'n_samples': int(n),
        'n_positive': int(y_true.sum()),
        'n_negative': int(n - y_true.sum()),
        'positive_rate': positive_rate,
        'average_precision': float(average_precision_score(y_true, y_prob)) if n_classes > 1 else None,
        'roc_auc': float(roc_auc_score(y_true, y_prob)) if n_classes > 1 else None,
        # SHARPNESS of the raw outputs -- not a calibration statement.
        'prob_min': float(y_prob.min()),
        'prob_max': float(y_prob.max()),
        'prob_mean': float(y_prob.mean()),
        'prob_std': float(y_prob.std()),
        'pos_weight_used': float(pos_weight) if pos_weight else None,
        # Analytic prior shift only: logit(p) = logit(q) - log(w). No fitting,
        # no data used, so this is safe to apply to the same split.
        'prob_corrected_min': float(corrected.min()),
        'prob_corrected_max': float(corrected.max()),
        'prob_corrected_mean': float(corrected.mean()),
        'prob_corrected_std': float(corrected.std()),
        'calibration_raw': calibration_report(y_true, y_prob),
        'calibration_prior_corrected': calibration_report(y_true, corrected),
        'threshold_sweep': sweep,
        'top_k': top_k,
    }


def _load_split_manifest():
    """
    Reads outputs/split_manifest.json, or raises.

    LABEL-AUDIT FIX 5. The previous outputs/val_order_ids.json path branched
    only on the file EXISTING, and warned-and-continued when it did not --
    so a missing manifest silently fell back to re-deriving the split, and a
    STALE manifest was never detected at all. Both produce a plausible
    metrics table computed against operations the checkpoint may well have
    trained on. There is no safe default here: without a verified split you
    do not know what the numbers mean, so this raises instead of guessing.
    """
    path = os.path.join(OUTPUT_DIR, MANIFEST_FILENAME)
    if not os.path.exists(path):
        raise SplitManifestError(
            f"{path} not found. Evaluation requires the split manifest written by "
            f"models/train.py, which records the exact held-out orders, the graph "
            f"fingerprint and the checkpoint hash they belong to. Without it there "
            f"is no way to verify that these metrics are computed on data the "
            f"checkpoint did not train on. Re-run models/train.py. "
            f"(The older outputs/val_order_ids.json is NOT accepted: it carries no "
            f"graph or checkpoint identity, which is exactly how a stale split "
            f"loaded silently before this check existed.)"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def _verify_split_manifest(manifest, path, builder, hetero_graph, checkpoint_path):
    """
    Confirms the manifest describes THIS graph and THIS checkpoint, and
    returns the verified val mask. Raises SplitManifestError on any mismatch.

    Every field is checked, not just the ones that are cheap to compare: the
    live failure this replaces had matching order IDs (a reseed regenerated
    the same 1-1029 range) and mismatched everything else, so order IDs
    alone are demonstrably not sufficient evidence of a valid split.
    """
    problems = []

    expected_nodes = int(hetero_graph['operation'].train_mask.numel())
    expected_train = int(hetero_graph['operation'].train_mask.sum())
    expected_val = int(hetero_graph['operation'].val_mask.sum())

    def check(field, expected, actual):
        if expected != actual:
            problems.append(f"  {field}: manifest={actual!r}, current={expected!r}")

    check("split_seed", SPLIT_SEED, manifest.get("split_seed"))
    check("val_fraction", VAL_FRACTION, manifest.get("val_fraction"))
    check("num_operation_nodes", expected_nodes, manifest.get("num_operation_nodes"))
    check("num_train_operations", expected_train, manifest.get("num_train_operations"))
    check("num_val_operations", expected_val, manifest.get("num_val_operations"))
    check("graph_fingerprint", builder.graph_fingerprint(), manifest.get("graph_fingerprint"))

    if os.path.exists(checkpoint_path):
        with open(checkpoint_path, "rb") as f:
            actual_sha = hashlib.sha256(f.read()).hexdigest()
        check("checkpoint_sha256", actual_sha, manifest.get("checkpoint_sha256"))

    if problems:
        raise SplitManifestError(
            "Split manifest does not match the graph/checkpoint being evaluated:\n"
            + "\n".join(problems)
            + f"\n\nManifest: {path}\nCheckpoint: {checkpoint_path}\n"
            "This means the recorded validation split was derived from different "
            "data than what is loaded now, so any metric computed against it would "
            "be measured on operations the checkpoint may have trained on. "
            "Re-run models/train.py to regenerate both together."
        )

    recorded_ids = set(manifest["production_order_ids"])
    val_mask = torch.from_numpy(
        np.isin(builder.order_ids_per_node.astype(str), list(recorded_ids))
    )
    if int(val_mask.sum()) != expected_val:
        raise SplitManifestError(
            f"Manifest recorded {len(recorded_ids)} validation orders covering "
            f"{manifest.get('num_val_operations')} operations, but those order IDs "
            f"match {int(val_mask.sum())} operation nodes in the current graph. "
            f"The order book has changed since training. Re-run models/train.py."
        )
    logger.info(
        f"Split manifest verified against {path}: {len(recorded_ids)} val orders / "
        f"{int(val_mask.sum())} operations, graph "
        f"{manifest['graph_fingerprint'][:12]}..., checkpoint "
        f"{str(manifest.get('checkpoint_sha256'))[:12]}..."
    )
    return val_mask


def _load_model_and_val_predictions(checkpoint_path=None):
    """
    Shared by evaluate_gnn() and compute_delay_curves(): build the graph,
    load the checkpoint, run inference, and return raw predictions/labels on
    the held-out validation nodes only.

    IMPORTANT: scoring on val_mask, not the whole graph — see
    graph/graph_builder.py. Evaluating on training nodes reports training
    performance, not a generalisation metric.

    val_mask comes from the verified split manifest (label-audit fix 5) --
    the exact orders a training run held out, checked against this graph's
    fingerprint and this checkpoint's hash. A missing or mismatched manifest
    raises SplitManifestError rather than falling back to a re-derived split.

    The manifest's frozen priority cut-points are fed back into the graph
    build so Head 3's labels are scored against the same slack->percentile
    curve training used (label-audit fix 1) instead of a fresh one derived
    from this rebuild's own split.

    Returns None if the checkpoint doesn't exist (callers check for that).
    """
    if checkpoint_path is None:
        checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
    logger.info(f"Loading checkpoint from {checkpoint_path}...")

    manifest, manifest_path = _load_split_manifest()

    device = torch.device(DEVICE)
    logger.info(f"Evaluating on device: {device}")
    from graph.feature_schema import load_preprocessing
    preprocessing = load_preprocessing(checkpoint_path)
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph(
        priority_cutpoints=manifest.get("priority_cutpoints"),
        preprocessing=preprocessing,
    )
    hetero_graph = hetero_graph.to(device)
    labels = {k: v.to(device) for k, v in labels.items()}

    model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata(), operation_in_dim=preprocessing['operation_schema']['count']).to(device)
    if not os.path.exists(checkpoint_path):
        logger.error(f"Checkpoint not found at {checkpoint_path}. Train model first.")
        return None

    model.load_state_dict(torch.load(checkpoint_path, map_location=device))
    model.eval()

    val_mask = _verify_split_manifest(
        manifest, manifest_path, builder, hetero_graph, checkpoint_path).to(device)

    # Head 1 metrics are computed only where a delay label actually exists
    # (label-audit fix 2). Operations with no recorded outcome, or only
    # partially-observed lots, used to enter the metrics as true negatives
    # -- inflating accuracy and depressing the positive rate against which
    # average precision is judged.
    delay_val_mask = val_mask & labels['delay_valid'].view(-1)
    n_delay_masked = int(val_mask.sum()) - int(delay_val_mask.sum())
    completion_val_mask = val_mask & labels['completion_valid'].view(-1)
    logger.info(
        f"Running inference on {int(val_mask.sum())} held-out validation operations "
        f"({int(delay_val_mask.sum())} of them usable for the delay head; "
        f"{n_delay_masked} masked out for missing/partial outcomes)..."
    )
    with torch.no_grad():
        out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, hetero_graph['operation'].type_idx)

    return {
        # The weight the delay head was TRAINED under. Needed to undo the
        # objective's tilt before any output is read as a probability.
        'delay_pos_weight': manifest.get('delay_pos_weight'),
        # Order id per scored val operation. Operations within one order are
        # correlated (shared deadline feature, order-level split), so any
        # uncertainty estimate must resample ORDERS, not operations.
        'delay_order_ids': builder.order_ids_per_node[
            delay_val_mask.cpu().numpy()].astype(str),
        # TRAINING targets, for constant-predictor baselines that a deployed
        # model could actually have used -- unlike the validation mean, which
        # is fitted on the labels being scored.
        'completion_train_true': labels['completion'][
            hetero_graph['operation'].train_mask.to(device)].cpu().numpy(),
        'priority_train_true': labels['priority'][
            hetero_graph['operation'].train_mask.to(device)].cpu().numpy(),
        'val_mask': val_mask,
        'delay_val_mask': delay_val_mask,
        'n_delay_masked': n_delay_masked,
        'delay_probs': torch.sigmoid(out['delay'][delay_val_mask]).cpu().numpy().ravel(),
        'delay_true': labels['delay'][delay_val_mask].cpu().numpy().ravel(),
        'completion_pred': out['completion'][completion_val_mask].cpu().numpy(),
        'completion_true': labels['completion'][completion_val_mask].cpu().numpy(),
        'n_completion_masked': int(val_mask.sum()) - int(completion_val_mask.sum()),
        'priority_pred': out['priority'][val_mask].cpu().numpy(),
        'priority_true': labels['priority'][val_mask].cpu().numpy(),
    }


def compute_delay_curves(checkpoint_path=None):
    """
    Precision-recall and ROC curve data for the delay classifier, on the same
    held-out validation nodes evaluate_gnn() scores.

    PR is the LEAD metric here, not ROC: ROC-AUC is known to look misleadingly
    reasonable under class imbalance because it's dominated by the (easy)
    true-negative rate, so a PR curve well above the no-skill (positive-rate)
    baseline is the more honest bar to clear.

    Historical note (see PROJECT_STATUS.txt Section 2.3/3): the ORIGINAL
    execution_status-derived delay label scored near the no-skill baseline on
    every run, because that label was 99.88% identical to "has this operation
    been touched" rather than a genuine risk signal — not a model failure, a
    label failure. Section 0D replaced the label with genuine plan-vs-actual
    lateness; this function's job now is to measure whatever the classifier
    actually does against that label, not to reproduce the old near-random
    result.

    Returns None if no checkpoint exists.
    """
    data = _load_model_and_val_predictions(checkpoint_path)
    if data is None:
        return None

    y_true, y_prob = data['delay_true'], data['delay_probs']
    if len(y_true) == 0:
        logger.warning('Delay curves unavailable: no usable validation labels')
        return None
    precision, recall, pr_thresholds = precision_recall_curve(y_true, y_prob)
    fpr, tpr, roc_thresholds = roc_curve(y_true, y_prob)

    # Scalars come from delay_operating_points(), the same function
    # evaluate_gnn() uses, so the AP/ROC-AUC/positive-rate reported by the
    # curve path and by the metrics table cannot drift apart. This module has
    # already been bitten once by two code paths computing one quantity
    # differently; one definition, two consumers.
    points = delay_operating_points(y_true, y_prob)
    return {
        'positive_rate': points['positive_rate'],
        'n_samples': points['n_samples'],
        'precision': precision.tolist(),
        'recall': recall.tolist(),
        'pr_thresholds': pr_thresholds.tolist(),
        'average_precision': points['average_precision'],
        'fpr': fpr.tolist(),
        'tpr': tpr.tolist(),
        'roc_thresholds': roc_thresholds.tolist(),
        # nan, not None, preserved here: downstream plotting reads this as a
        # float and formats it, where None would raise.
        'roc_auc': points['roc_auc'] if points['roc_auc'] is not None else float('nan'),
        'y_true': y_true.tolist(),
        'y_prob': y_prob.tolist(),
    }


def evaluate_gnn(checkpoint_path=None):
    data = _load_model_and_val_predictions(checkpoint_path)
    if data is None:
        return None

    pred_delay_probs = data['delay_probs']
    pred_delay_binary = (pred_delay_probs >= 0.5).astype(float)
    true_delay = data['delay_true']

    pred_completion = data['completion_pred']
    true_completion = data['completion_true']

    pred_priority = data['priority_pred']
    true_priority = data['priority_true']

    # Task 1: Delay Classification Metrics
    acc = accuracy_score(true_delay, pred_delay_binary) if len(true_delay) else None
    prec = precision_score(true_delay, pred_delay_binary, zero_division=0) if len(true_delay) else None
    rec = recall_score(true_delay, pred_delay_binary, zero_division=0) if len(true_delay) else None
    f1 = f1_score(true_delay, pred_delay_binary, zero_division=0) if len(true_delay) else None
    
    # Task 2: Completion Time Regression Metrics.
    # PLAN REPRODUCTION, not forecast error -- see COMPLETION_METRIC_SEMANTICS.
    completion = regression_metrics(true_completion, pred_completion,
                                    train_targets=data.get('completion_train_true'))

    # Task 3: Priority Metrics
    # Named mae_prio, not mse_prio (its old name) -- this is
    # mean_absolute_error, not MSE. The dict key was always correct
    # (priority_mae); only the local variable name was misleading.
    priority = regression_metrics(true_priority, pred_priority,
                                  train_targets=data.get('priority_train_true'))

    # Ranking/threshold behaviour for the delay head. Computed here, in the
    # main entry point, rather than only in compute_delay_curves(): AP and
    # ROC-AUC are the two metrics that actually describe this head, and
    # requiring a second function call (and a second full graph build) to see
    # them meant the headline table routinely went out without them.
    delay_points = delay_operating_points(true_delay, pred_delay_probs,
                                          pos_weight=data.get('delay_pos_weight'),
                                          groups=data.get('delay_order_ids'))
    constant_refs = weighted_constant_references(true_delay, data.get('delay_pos_weight'))

    metrics = {
        'delay_accuracy': acc,
        'delay_precision': prec,
        'delay_recall': rec,
        'delay_f1': f1,
        'delay_average_precision': delay_points['average_precision'] if delay_points else None,
        'delay_roc_auc': delay_points['roc_auc'] if delay_points else None,
        # MEASURED on this split, not inherited from the training run's
        # pos_weight -- it is the no-skill baseline for AP and for every
        # top-k lift, so it has to come from the data being scored.
        'delay_positive_rate': delay_points['positive_rate'] if delay_points else None,
        'delay_val_positives': delay_points['n_positive'] if delay_points else None,
        'delay_val_negatives': delay_points['n_negative'] if delay_points else None,
        'delay_prob_min': delay_points['prob_min'] if delay_points else None,
        'delay_prob_max': delay_points['prob_max'] if delay_points else None,
        'delay_prob_mean': delay_points['prob_mean'] if delay_points else None,
        'delay_prob_std': delay_points['prob_std'] if delay_points else None,
        'delay_pos_weight_used': delay_points['pos_weight_used'] if delay_points else None,
        'delay_prob_corrected_min': delay_points['prob_corrected_min'] if delay_points else None,
        'delay_prob_corrected_max': delay_points['prob_corrected_max'] if delay_points else None,
        'delay_prob_corrected_mean': delay_points['prob_corrected_mean'] if delay_points else None,
        'delay_prob_corrected_std': delay_points['prob_corrected_std'] if delay_points else None,
        'delay_calibration_raw': delay_points['calibration_raw'] if delay_points else None,
        'delay_calibration_prior_corrected': (
            delay_points['calibration_prior_corrected'] if delay_points else None),
        # Both constant-classifier references, each named for what it is.
        'delay_constant_reference_val_optimal': (
            constant_refs['val_optimal_loss'] if constant_refs else None),
        'delay_constant_reference_val_optimal_q': (
            constant_refs['val_optimal_constant'] if constant_refs else None),
        'delay_constant_reference_train_derived': (
            constant_refs['train_derived_loss'] if constant_refs else None),
        'delay_constant_reference_train_derived_q': (
            constant_refs['train_derived_constant'] if constant_refs else None),
        'delay_threshold_sweep': delay_points['threshold_sweep'] if delay_points else None,
        'delay_top_k': delay_points['top_k'] if delay_points else None,
        'completion_mae_minutes': completion['mae'],
        'completion_rmse_minutes': completion['rmse'],
        'completion_r2': completion['r2'],
        'completion_validation_mean_reference_mae': completion['validation_mean_reference_mae'],
        'completion_validation_mean_reference_rmse': completion['validation_mean_reference_rmse'],
        'completion_train_mean_baseline_mae': completion['train_mean_baseline_mae'],
        'completion_train_mean_baseline_rmse': completion['train_mean_baseline_rmse'],
        'completion_train_median_baseline_mae': completion['train_median_baseline_mae'],
        'completion_baseline_mae_mean_predictor': completion['baseline_mae_mean_predictor'],
        'completion_baseline_rmse_mean_predictor': completion['baseline_rmse_mean_predictor'],
        'completion_metric_semantics': COMPLETION_METRIC_SEMANTICS,
        'priority_mae': priority['mae'],
        'priority_rmse': priority['rmse'],
        'priority_r2': priority['r2'],
        'priority_validation_mean_reference_mae': priority['validation_mean_reference_mae'],
        'priority_train_mean_baseline_mae': priority['train_mean_baseline_mae'],
        'priority_train_median_baseline_mae': priority['train_median_baseline_mae'],
        'priority_baseline_mae_mean_predictor': priority['baseline_mae_mean_predictor'],
        'completion_labelled_operations': int(len(true_completion)),
        'completion_masked_operations': int(data['n_completion_masked']),
        # Denominator transparency (label-audit fix 2): the delay metrics
        # above are computed over a SMALLER population than the other two
        # heads, because operations with no usable outcome are masked out
        # rather than counted as on-time. Reported so the difference is
        # visible in the numbers rather than implied.
        'delay_labelled_operations': int(len(true_delay)),
        'delay_masked_operations': int(data['n_delay_masked']),
    }

    _log_metrics(metrics)
    return metrics


def _log_metrics(metrics):
    """Scalar summary, then the two tables, which do not fit one line each."""
    logger.info("--- EVALUATION METRICS SUMMARY ---")
    for k, v in metrics.items():
        if k in ('delay_threshold_sweep', 'delay_top_k',
                 'delay_calibration_raw', 'delay_calibration_prior_corrected'):
            continue
        logger.info(f"{k:<42}: {v if v is not None else 'unavailable (no usable labels)'}")

    sweep = metrics.get('delay_threshold_sweep')
    if sweep:
        logger.info("--- DELAY HEAD: OPERATING THRESHOLDS ---")
        logger.info(f"{'thresh':>7}{'n_pred_pos':>12}{'precision':>11}"
                    f"{'recall':>9}{'f1':>8}{'accuracy':>10}")
        for row in sweep:
            logger.info(f"{row['threshold']:>7.2f}{row['n_predicted_positive']:>12}"
                        f"{row['precision']:>11.4f}{row['recall']:>9.4f}"
                        f"{row['f1']:>8.4f}{row['accuracy']:>10.4f}")

    top_k = metrics.get('delay_top_k')
    if top_k:
        base = metrics.get('delay_positive_rate')
        logger.info(f"--- DELAY HEAD: PRECISION AT TOP-K (base rate {base:.4f}) ---")
        logger.info(f"{'top-k':>8}{'n':>7}{'thresh':>9}{'precision':>11}{'lift':>9}{'recall':>9}")
        for row in top_k:
            lift = f"{row['lift']:.3f}x" if row['lift'] is not None else "n/a"
            rec = f"{row['recall']:.4f}" if row['recall'] is not None else "n/a"
            logger.info(f"{100*row['fraction']:>7.0f}%{row['k']:>7}"
                        f"{row['threshold_at_k']:>9.3f}{row['precision']:>11.4f}"
                        f"{lift:>9}{rec:>9}")

def _print_summary(results):
    def show(label, key, fmt="{}"):
        v = results.get(key)
        print(f"  {label:<38}"
              + ("unavailable (no usable labels)" if v is None else fmt.format(v)))

    print("\n" + "=" * 72)
    print("DELAY HEAD (binary classifier)")
    print("=" * 72)
    show("val operations scored", 'delay_labelled_operations')
    show("masked (no/partial outcome)", 'delay_masked_operations')
    show("positives", 'delay_val_positives')
    show("negatives", 'delay_val_negatives')
    show("MEASURED positive rate", 'delay_positive_rate', "{:.4f}")
    show("average precision (AP)", 'delay_average_precision', "{:.4f}")
    show("  no-skill AP baseline", 'delay_positive_rate', "{:.4f}")
    show("ROC-AUC", 'delay_roc_auc', "{:.4f}")
    print("  constant-classifier references (weighted loss, LABELLED):")
    show("  val-optimal constant q", 'delay_constant_reference_val_optimal_q', "{:.6f}")
    show("  its weighted loss", 'delay_constant_reference_val_optimal', "{:.6f}")
    show("  train-derived constant q", 'delay_constant_reference_train_derived_q', "{:.6f}")
    show("  its weighted loss on val", 'delay_constant_reference_train_derived', "{:.6f}")
    print("  The val-optimal constant is fitted on the labels being scored and is")
    print("  not deployable; the train-derived one (q=0.5 exactly, under inverse-")
    print("  prevalence weighting) is what a model-free fallback would have scored.")
    show("accuracy @0.5", 'delay_accuracy', "{:.4f}")
    show("precision @0.5", 'delay_precision', "{:.4f}")
    show("recall @0.5", 'delay_recall', "{:.4f}")
    show("F1 @0.5", 'delay_f1', "{:.4f}")
    print("  predicted-probability SHARPNESS (spread only -- says nothing about")
    print("  calibration; a constant base-rate predictor is perfectly calibrated):")
    show("  min", 'delay_prob_min', "{:.4f}")
    show("  max", 'delay_prob_max', "{:.4f}")
    show("  mean", 'delay_prob_mean', "{:.4f}")
    show("  std", 'delay_prob_std', "{:.4f}")
    w = results.get('delay_pos_weight_used')
    if w:
        print("")
        print(f"prior-corrected probabilities  logit(p) = logit(q) - log(w), w={w:.4f}")
        print(f"  Analytic transform, no fitting. Under weighted BCE the optimal")
        print(f"  output for true probability p is w*p/(w*p+1-p), so a raw 0.5")
        print(f"  means p={1/(1+w):.4f} -- a mean near 0.5 is EXPECTED here, not")
        print(f"  a miscalibration finding.")
        show("  corrected min", 'delay_prob_corrected_min', "{:.4f}")
        show("  corrected max", 'delay_prob_corrected_max', "{:.4f}")
        show("  corrected mean", 'delay_prob_corrected_mean', "{:.4f}")
        show("  corrected std", 'delay_prob_corrected_std', "{:.4f}")

    for label, key in (("RAW", 'delay_calibration_raw'),
                       ("PRIOR-CORRECTED", 'delay_calibration_prior_corrected')):
        cal = results.get(key)
        if not cal:
            continue
        print("")
        print(f"CALIBRATION ({label}) -- measured, not inferred from spread:")
        print(f"    Brier score                 "
              + ("n/a" if cal['brier_score'] is None else f"{cal['brier_score']:.6f}"))
        print(f"    log loss (unweighted)       "
              + ("n/a" if cal['log_loss_unweighted'] is None else f"{cal['log_loss_unweighted']:.6f}"))
        print(f"    expected calibration error  {cal['expected_calibration_error']:.6f}")
        print(f"    mean predicted / observed   {cal['mean_predicted']:.4f} / {cal['observed_base_rate']:.4f}")
        print(f"    reliability: {'bin':>12}{'n':>7}{'pred':>9}{'observed':>10}{'gap':>9}")
        for b in cal['reliability_bins']:
            print(f"                 {b['bin_lower']:.1f}-{b['bin_upper']:.1f}{b['n']:>9}"
                  f"{b['predicted_mean']:>9.4f}{b['observed_frequency']:>10.4f}{b['gap']:>9.4f}")
    # No R2 for this head on purpose -- it is a classifier; see
    # regression_metrics()'s docstring.

    sweep = results.get('delay_threshold_sweep')
    if sweep:
        print("\n  operating thresholds:")
        print(f"  {'thresh':>7}{'n_pred_pos':>12}{'precision':>11}"
              f"{'recall':>9}{'f1':>8}{'accuracy':>10}")
        for r in sweep:
            print(f"  {r['threshold']:>7.2f}{r['n_predicted_positive']:>12}"
                  f"{r['precision']:>11.4f}{r['recall']:>9.4f}"
                  f"{r['f1']:>8.4f}{r['accuracy']:>10.4f}")

    top_k = results.get('delay_top_k')
    if top_k:
        print(f"\n  precision at top-k (lift vs measured base rate "
              f"{results['delay_positive_rate']:.4f}):")
        print("  EXPLORATORY at small k: precision over a handful of selected")
        print("  operations containing a handful of positives is not an operational")
        print("  guarantee. Coverage (share of ALL positives caught) is shown so the")
        print("  sample size behind each row is visible; the CI is a cluster")
        print("  bootstrap over ORDERS, since operations within an order are not")
        print("  independent.")
        print(f"  {'top-k':>8}{'n':>7}{'pos':>6}{'thresh':>9}{'precision':>11}"
              f"{'lift':>9}{'coverage':>10}{'95% CI (orders)':>22}")
        for r in top_k:
            lift = f"{r['lift']:.3f}x" if r['lift'] is not None else "n/a"
            cov = f"{100*r['coverage_of_all_positives']:.2f}%" if r['coverage_of_all_positives'] is not None else "n/a"
            ci = r.get('precision_ci95')
            ci_s = f"[{ci['lower']:.4f}, {ci['upper']:.4f}]" if ci else "n/a (no grouping)"
            print(f"  {100*r['fraction']:>7.0f}%{r['k']:>7}{r['n_positive_in_k']:>6}"
                  f"{r['threshold_at_k']:>9.3f}{r['precision']:>11.4f}{lift:>9}"
                  f"{cov:>10}{ci_s:>22}")

    print("\n" + "=" * 72)
    print("COMPLETION HEAD (regression)")
    print("=" * 72)
    print(f"  MEASURES: {results.get('completion_metric_semantics')}")
    print("  A high R2 here is fidelity to the PLAN, not forecast accuracy.")
    show("val operations scored", 'completion_labelled_operations')
    show("masked (no planned span)", 'completion_masked_operations')
    show("MAE (minutes)", 'completion_mae_minutes', "{:.4f}")
    show("RMSE (minutes)", 'completion_rmse_minutes', "{:.4f}")
    show("R2", 'completion_r2', "{:.4f}")
    show("validation-mean reference MAE", 'completion_validation_mean_reference_mae', "{:.4f}")
    show("validation-mean reference RMSE", 'completion_validation_mean_reference_rmse', "{:.4f}")
    print("    ^ R2's own denominator; fitted on the scored labels, NOT deployable.")
    show("train-mean baseline MAE", 'completion_train_mean_baseline_mae', "{:.4f}")
    show("train-mean baseline RMSE", 'completion_train_mean_baseline_rmse', "{:.4f}")
    show("train-median baseline MAE", 'completion_train_median_baseline_mae', "{:.4f}")
    print("    ^ deployable constants fitted on TRAINING targets only;")
    print("      the median is the MAE-optimal constant, the mean is RMSE-optimal.")

    print("\n" + "=" * 72)
    print("PRIORITY HEAD (regression)")
    print("=" * 72)
    show("MAE", 'priority_mae', "{:.4f}")
    show("RMSE", 'priority_rmse', "{:.4f}")
    show("R2", 'priority_r2', "{:.4f}")
    show("validation-mean reference MAE", 'priority_validation_mean_reference_mae', "{:.4f}")
    print("    ^ R2's own denominator; fitted on the scored labels, NOT deployable.")
    show("train-mean baseline MAE", 'priority_train_mean_baseline_mae', "{:.4f}")
    show("train-median baseline MAE", 'priority_train_median_baseline_mae', "{:.4f}")


if __name__ == "__main__":
    print("Evaluating GNN Model Performance...")
    results = evaluate_gnn()
    if results:
        _print_summary(results)