"""
One-off diagnostic (not part of the pipeline): fair-fight comparison for
the just-finished Option B checkpoint (10 continuous operation features:
decisive-lot breakdown + reinstated mean-based breakdown, alongside the
existing causal features). Same methodology as the historical fair-fight
investigation (PROJECT_STATUS.txt 0H.12-0H.15): GNN (graph + message
passing) vs. feature-only sklearn baselines trained on the IDENTICAL
operation continuous features + one-hot operation_type, same held-out
order-level split, so the graph structure is the only thing being tested.

REUSES the already-trained checkpoint (outputs/checkpoints/gnn_multitask_latest.pth)
rather than retraining -- training just finished, no need to repeat 150 epochs.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import torch
from sklearn.linear_model import LogisticRegression, LinearRegression
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import average_precision_score, roc_auc_score, mean_absolute_error, root_mean_squared_error, r2_score
from sklearn.dummy import DummyRegressor

from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from utils.config import OUTPUT_DIR
from utils.machine_taxonomy import NUM_OPERATION_TYPES

checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")

print("="*70)
print("1. BUILDING GRAPH (current live database, Option B features)")
print("="*70)
builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()

train_mask = hetero_graph['operation'].train_mask.numpy()
val_mask = hetero_graph['operation'].val_mask.numpy()
op_type_idx = hetero_graph['operation'].type_idx

print(f"operations: {len(train_mask)} (train {train_mask.sum()} / val {val_mask.sum()})")
print(f"operation feature dim: {hetero_graph['operation'].x.shape[1]} continuous + {NUM_OPERATION_TYPES}-way type embedding")

print()
print("="*70)
print("2. LOADING ALREADY-TRAINED CHECKPOINT (Option B, just finished), RUNNING INFERENCE")
print("="*70)
model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata())
model.load_state_dict(torch.load(checkpoint_path))
model.eval()

with torch.no_grad():
    out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)

delay_probs = torch.sigmoid(out['delay']).cpu().numpy().ravel()
completion_pred = out['completion'].cpu().numpy().ravel()
priority_pred = out['priority'].cpu().numpy().ravel()

delay_true = labels['delay'].cpu().numpy().ravel()
completion_true = labels['completion'].cpu().numpy().ravel()
priority_true = labels['priority'].cpu().numpy().ravel()

print()
print("="*70)
print("3. FEATURE-ONLY BASELINES (10 continuous + one-hot operation_type, same split)")
print("="*70)

# Build the feature-only design matrix: identical continuous features the
# GNN's operation node uses, plus a one-hot of operation_type standing in
# for the GNN's learned embedding (sklearn has no message passing / no
# embedding table, this is the natural non-graph equivalent).
X_cont = hetero_graph['operation'].x.numpy()
type_idx_np = op_type_idx.numpy()
X_type_onehot = np.eye(NUM_OPERATION_TYPES)[type_idx_np]
X = np.concatenate([X_cont, X_type_onehot], axis=1)

X_train, X_val = X[train_mask], X[val_mask]

pos_rate = float(delay_true[val_mask].mean())
print(f"positive rate (no-skill AP baseline): {pos_rate:.4f}, n={int(val_mask.sum())}")

# --- DELAY ---
y_delay_train, y_delay_val = delay_true[train_mask], delay_true[val_mask]

logreg = LogisticRegression(max_iter=1000, class_weight='balanced')
logreg.fit(X_train, y_delay_train)
logreg_prob = logreg.predict_proba(X_val)[:, 1]

rf_clf = RandomForestClassifier(n_estimators=300, random_state=42, class_weight='balanced', n_jobs=-1)
rf_clf.fit(X_train, y_delay_train)
rf_clf_prob = rf_clf.predict_proba(X_val)[:, 1]

gnn_delay_val_prob = delay_probs[val_mask]
gnn_delay_val_true = delay_true[val_mask]

print()
print("DELAY:")
def _report_delay(name, y_true, y_prob):
    ap = average_precision_score(y_true, y_prob)
    try:
        roc = roc_auc_score(y_true, y_prob)
    except ValueError:
        roc = float('nan')
    print(f"  {name:<38} ROC_AUC={roc:.4f}  AP={ap:.4f}")

_report_delay("GNN (graph + message passing, Option B)", gnn_delay_val_true, gnn_delay_val_prob)
_report_delay("LogisticRegression (features only)", y_delay_val, logreg_prob)
_report_delay("RandomForest (features only)", y_delay_val, rf_clf_prob)

# --- COMPLETION ---
y_compl_train, y_compl_val = completion_true[train_mask], completion_true[val_mask]

dummy = DummyRegressor(strategy='mean')
dummy.fit(X_train, y_compl_train)
dummy_pred = dummy.predict(X_val)

linreg = LinearRegression()
linreg.fit(X_train, y_compl_train)
linreg_pred = linreg.predict(X_val)

rf_reg = RandomForestRegressor(n_estimators=300, random_state=42, n_jobs=-1)
rf_reg.fit(X_train, y_compl_train)
rf_reg_pred = rf_reg.predict(X_val)

gnn_compl_val_pred = completion_pred[val_mask]

print()
print("COMPLETION:")
def _report_completion(name, y_true, y_pred):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = root_mean_squared_error(y_true, y_pred)
    r2 = r2_score(y_true, y_pred)
    print(f"  {name:<38} MAE={mae:.1f}  RMSE={rmse:.1f}  R2={r2:.4f}")

_report_completion("GNN (graph + message passing, Option B)", y_compl_val, gnn_compl_val_pred)
_report_completion("constant (train-set mean)", y_compl_val, dummy_pred)
_report_completion("LinearRegression (features only)", y_compl_val, linreg_pred)
_report_completion("RandomForest (features only)", y_compl_val, rf_reg_pred)

# --- PRIORITY (secondary, for completeness) ---
y_prio_train, y_prio_val = priority_true[train_mask], priority_true[val_mask]
linreg_p = LinearRegression()
linreg_p.fit(X_train, y_prio_train)
linreg_p_pred = linreg_p.predict(X_val)
rf_reg_p = RandomForestRegressor(n_estimators=300, random_state=42, n_jobs=-1)
rf_reg_p.fit(X_train, y_prio_train)
rf_reg_p_pred = rf_reg_p.predict(X_val)
gnn_prio_val_pred = priority_pred[val_mask]

print()
print("PRIORITY (secondary/FYI):")
_report_completion("GNN (graph + message passing, Option B)", y_prio_val, gnn_prio_val_pred)
_report_completion("LinearRegression (features only)", y_prio_val, linreg_p_pred)
_report_completion("RandomForest (features only)", y_prio_val, rf_reg_p_pred)

print()
print("DONE")
