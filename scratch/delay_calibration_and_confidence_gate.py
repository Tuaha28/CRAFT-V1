"""
One-off diagnostic (not part of the pipeline): delay calibration + a
confidence-gate threshold analysis for RISK_BUFFER_AT_CERTAINTY, against
the already-trained Option B checkpoint. Zero new training -- reuses
models/evaluate.py's existing inference path.

CONTEXT (see chat): PROJECT_STATUS.txt Section 3 / 0H.5 found that
applying RISK_BUFFER_AT_CERTAINTY broadly, under the OLD near-constant
delay prediction, made every schedule metric worse -- a flat tax with zero
discriminative value. It was disabled with an explicit note: "restoring it
is correct the moment [the label] predicts something real." The label now
does (AP 0.159 vs. 0.112 no-skill, confirmed twice independently). This
script checks two things before anyone touches that config value again:

1. CALIBRATION -- does a predicted probability of ~0.3 actually mean
   ~30% of those operations are delayed, or is the classifier's OUTPUT
   badly miscalibrated even where its RANKING (AP/ROC-AUC) is honest?
   AP/ROC-AUC only measure ranking quality, not this.

2. CONFIDENCE-GATE THRESHOLD -- since the old failure mode was "acting on
   a broadly-applied, mostly-noisy signal," find the operating point(s)
   where precision clears a meaningful multiple of the base rate, and
   report how much of the true positive population that operating point
   still captures (recall) -- the concrete inputs a gated-buffer decision
   needs, not a modeling change.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import brier_score_loss

from models.evaluate import _load_model_and_val_predictions

data = _load_model_and_val_predictions()
y_true = data['delay_true']
y_prob = data['delay_probs']

base_rate = float(y_true.mean())
n = len(y_true)

print("="*70)
print("1. CALIBRATION")
print("="*70)
print(f"n = {n}, base rate = {base_rate:.4f}")
print(f"predicted probability range: [{y_prob.min():.4f}, {y_prob.max():.4f}]")
print(f"predicted probability mean:  {y_prob.mean():.4f}")

brier = brier_score_loss(y_true, y_prob)
brier_noskill = brier_score_loss(y_true, np.full_like(y_prob, base_rate))
print(f"\nBrier score (this model):        {brier:.4f}")
print(f"Brier score (no-skill, predict base rate always): {brier_noskill:.4f}")
print("(lower is better; this model should beat no-skill if its probabilities carry real information)")

frac_pos, mean_pred = calibration_curve(y_true, y_prob, n_bins=10, strategy='quantile')
# calibration_curve doesn't return bin counts directly -- recompute via the same quantile edges
quantile_edges = np.quantile(y_prob, np.linspace(0, 1, 11))
bin_idx = np.clip(np.digitize(y_prob, quantile_edges[1:-1]), 0, 9)
counts = np.bincount(bin_idx, minlength=10)

print("\nQuantile-binned reliability (10 bins, ~equal count each):")
print(f"{'bin':>4}{'n':>6}{'mean_pred':>12}{'actual_rate':>14}{'gap':>10}")
for i in range(len(mean_pred)):
    gap = mean_pred[i] - frac_pos[i]
    print(f"{i:>4}{counts[i]:>6}{mean_pred[i]:>12.4f}{frac_pos[i]:>14.4f}{gap:>+10.4f}")
print("(mean_pred > actual_rate = overconfident in that bucket; mean_pred < actual_rate = underconfident)")

print()
print("="*70)
print("2. CONFIDENCE-GATE THRESHOLD ANALYSIS")
print("="*70)
print(f"{'top K%':>8}{'n_flagged':>11}{'threshold':>12}{'precision':>12}{'recall':>10}{'lift_vs_base':>14}")

order = np.argsort(-y_prob)  # descending by predicted probability
y_true_sorted = y_true[order]
y_prob_sorted = y_prob[order]
total_positives = y_true.sum()

for pct in [0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]:
    k = max(1, int(round(pct * n)))
    flagged_true = y_true_sorted[:k]
    threshold = y_prob_sorted[k - 1]
    precision = flagged_true.mean()
    recall = flagged_true.sum() / total_positives
    lift = precision / base_rate
    print(f"{pct*100:>7.0f}%{k:>11}{threshold:>12.4f}{precision:>12.4f}{recall:>10.4f}{lift:>13.2f}x")

print()
print("(precision = fraction of flagged operations that are truly delayed)")
print("(recall = fraction of ALL true delays captured at this cutoff)")
print("(lift_vs_base = precision / base_rate -- how much more reliable than acting on everyone)")
print("\nDONE")
