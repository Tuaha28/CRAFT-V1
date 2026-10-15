"""
One-off diagnostic: real AP/ROC-AUC/R2 metrics for the split-encoder
checkpoint (outputs/checkpoints/gnn_split_encoder_best.pth), same
methodology/metrics as models/evaluate.py, for a direct comparison
against the Option B (shared-encoder) checkpoint already measured.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from sklearn.metrics import average_precision_score, roc_auc_score, mean_absolute_error, root_mean_squared_error, r2_score

from graph.graph_builder import FactoryGraphBuilder
from scratch.gnn_split_encoder import SplitEncoderFactoryGNN
from utils.config import OUTPUT_DIR

checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_split_encoder_best.pth")
print(f"Loading checkpoint: {checkpoint_path}")

builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()

model = SplitEncoderFactoryGNN(metadata=hetero_graph.metadata())
model.load_state_dict(torch.load(checkpoint_path))
model.eval()

val_mask = hetero_graph['operation'].val_mask
op_type_idx = hetero_graph['operation'].type_idx

with torch.no_grad():
    out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)

delay_probs = torch.sigmoid(out['delay'][val_mask]).cpu().numpy().ravel()
delay_true = labels['delay'][val_mask].cpu().numpy().ravel()
completion_pred = out['completion'][val_mask].cpu().numpy().ravel()
completion_true = labels['completion'][val_mask].cpu().numpy().ravel()
priority_pred = out['priority'][val_mask].cpu().numpy().ravel()
priority_true = labels['priority'][val_mask].cpu().numpy().ravel()

pos_rate = float(delay_true.mean())
ap = average_precision_score(delay_true, delay_probs)
roc = roc_auc_score(delay_true, delay_probs)
compl_mae = mean_absolute_error(completion_true, completion_pred)
compl_rmse = root_mean_squared_error(completion_true, completion_pred)
compl_r2 = r2_score(completion_true, completion_pred)
prio_r2 = r2_score(priority_true, priority_pred)

print("\n" + "="*70)
print("SPLIT-ENCODER CHECKPOINT -- REAL METRICS")
print("="*70)
print(f"n_val = {len(delay_true)}, positive_rate = {pos_rate:.4f}")
print(f"DELAY:      AP={ap:.4f}  ROC_AUC={roc:.4f}")
print(f"COMPLETION: MAE={compl_mae:.1f}  RMSE={compl_rmse:.1f}  R2={compl_r2:.4f}")
print(f"PRIORITY:   R2={prio_r2:.4f}")
print("\nDONE")
