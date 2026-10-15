"""
One-off diagnostic (not part of the pipeline): permutation importance for
the just-finished Option B checkpoint (10 continuous operation features +
operation_type), across all three heads. Same methodology as the project's
earlier permutation-importance passes (see scratchpad_perm_importance.log,
scratchpad_joint_perm.log from 2026-08-20): shuffle one feature column at a
time across ALL operations (so the scramble also propagates to val nodes'
neighbors via message passing, not just the val nodes' own rows), rerun the
already-trained model's forward pass, score ONLY on the held-out val_mask,
and report the drop from baseline (mean +- std over repeats). Positive drop
= removing this feature's real signal hurts the model = feature matters.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import torch
from sklearn.metrics import average_precision_score, r2_score

from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from utils.config import OUTPUT_DIR

N_REPEATS = 20
SEED = 42

FEATURE_NAMES = [
    "sam_norm", "qty_norm", "machine_parallelism_norm", "deadline_budget_norm",
    "decisive_lot_breakdown_count_norm", "mean_queue_delay_norm",
    "routing_step_norm", "steps_remaining_norm", "pool_occupancy_norm",
    "mean_breakdown_count_norm",
]

checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")

builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()

model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata())
model.load_state_dict(torch.load(checkpoint_path))
model.eval()

val_mask = hetero_graph['operation'].val_mask
delay_true = labels['delay'][val_mask].cpu().numpy().ravel()
completion_true = labels['completion'][val_mask].cpu().numpy().ravel()
priority_true = labels['priority'][val_mask].cpu().numpy().ravel()

x_orig = hetero_graph['operation'].x.clone()
type_idx_orig = hetero_graph['operation'].type_idx.clone()

def run_forward(x, type_idx):
    x_dict = dict(hetero_graph.x_dict)
    x_dict['operation'] = x
    with torch.no_grad():
        out = model(x_dict, hetero_graph.edge_index_dict, type_idx)
    delay_prob = torch.sigmoid(out['delay'][val_mask]).cpu().numpy().ravel()
    compl_pred = out['completion'][val_mask].cpu().numpy().ravel()
    prio_pred = out['priority'][val_mask].cpu().numpy().ravel()
    return delay_prob, compl_pred, prio_pred

def score(delay_prob, compl_pred, prio_pred):
    return {
        'delay_ap': average_precision_score(delay_true, delay_prob),
        'compl_r2': r2_score(completion_true, compl_pred),
        'prio_r2': r2_score(priority_true, prio_pred),
    }

print(f"checkpoint: {checkpoint_path}")
baseline_scores = score(*run_forward(x_orig, type_idx_orig))
print(f"\nBASELINE (unpermuted): {baseline_scores}")

rng = np.random.RandomState(SEED)
n_ops = x_orig.shape[0]

print()
print("="*110)
print(f"{'feature':<38}{'delay_AP_drop':>18}{'compl_R2_drop':>18}{'prio_R2_drop':>18}")
print("="*110)

results = {}
for col_idx, fname in enumerate(FEATURE_NAMES):
    drops = {'delay_ap': [], 'compl_r2': [], 'prio_r2': []}
    for r in range(N_REPEATS):
        perm = rng.permutation(n_ops)
        x_perm = x_orig.clone()
        x_perm[:, col_idx] = x_orig[perm, col_idx]
        s = score(*run_forward(x_perm, type_idx_orig))
        drops['delay_ap'].append(baseline_scores['delay_ap'] - s['delay_ap'])
        drops['compl_r2'].append(baseline_scores['compl_r2'] - s['compl_r2'])
        drops['prio_r2'].append(baseline_scores['prio_r2'] - s['prio_r2'])
    results[fname] = drops
    print(f"{fname:<38}"
          f"{np.mean(drops['delay_ap']):>10.4f}+-{np.std(drops['delay_ap']):.4f}"
          f"{np.mean(drops['compl_r2']):>10.4f}+-{np.std(drops['compl_r2']):.4f}"
          f"{np.mean(drops['prio_r2']):>10.4f}+-{np.std(drops['prio_r2']):.4f}")

# operation_type (categorical index, permuted separately)
drops = {'delay_ap': [], 'compl_r2': [], 'prio_r2': []}
for r in range(N_REPEATS):
    perm = rng.permutation(n_ops)
    type_idx_perm = type_idx_orig.clone()
    type_idx_perm = type_idx_orig[perm]
    s = score(*run_forward(x_orig, type_idx_perm))
    drops['delay_ap'].append(baseline_scores['delay_ap'] - s['delay_ap'])
    drops['compl_r2'].append(baseline_scores['compl_r2'] - s['compl_r2'])
    drops['prio_r2'].append(baseline_scores['prio_r2'] - s['prio_r2'])
results['operation_type'] = drops
print(f"{'operation_type':<38}"
      f"{np.mean(drops['delay_ap']):>10.4f}+-{np.std(drops['delay_ap']):.4f}"
      f"{np.mean(drops['compl_r2']):>10.4f}+-{np.std(drops['compl_r2']):.4f}"
      f"{np.mean(drops['prio_r2']):>10.4f}+-{np.std(drops['prio_r2']):.4f}")

print()
print("(positive drop = removing this feature's info HURTS the model = feature matters)")
print("(near-zero or negative drop = model doesn't rely on this feature)")
print("DONE")
