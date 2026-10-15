"""
One-off diagnostic (not part of the pipeline): identify which epoch the
saved checkpoint actually corresponds to, by recomputing the EXACT same
per-head losses train.py uses (BCEWithLogits delay, Huber(delta=0.4) on
completion/1000, MSE priority) against the checkpoint's own forward pass,
and comparing the total against the logged per-10-epoch val loss trajectory.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.nn as nn
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN, COMPLETION_SCALE_MINUTES
from utils.config import OUTPUT_DIR

checkpoint_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
print(f"Loading checkpoint: {checkpoint_path}")

builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()

model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata())
model.load_state_dict(torch.load(checkpoint_path))
model.eval()

val_mask = hetero_graph['operation'].val_mask
op_type_idx = hetero_graph['operation'].type_idx

criterion_delay = nn.BCEWithLogitsLoss()
criterion_completion = nn.HuberLoss(delta=0.4)
criterion_priority = nn.MSELoss()

with torch.no_grad():
    out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)

    loss_delay = criterion_delay(out['delay'][val_mask], labels['delay'][val_mask])
    loss_completion = criterion_completion(
        out['completion'][val_mask] / COMPLETION_SCALE_MINUTES,
        labels['completion'][val_mask] / COMPLETION_SCALE_MINUTES,
    )
    loss_priority = criterion_priority(out['priority'][val_mask], labels['priority'][val_mask])
    total = loss_delay + loss_completion + loss_priority

print("\n--- RECOMPUTED VAL LOSS (same recipe as train.py's compute_losses) ---")
print(f"delay:      {loss_delay.item():.4f}")
print(f"completion: {loss_completion.item():.4f}")
print(f"priority:   {loss_priority.item():.4f}")
print(f"TOTAL:      {total.item():.4f}")

print("\n--- FOR COMPARISON, LOGGED VAL TOTAL LOSS PER EPOCH (this run) ---")
logged = {
    120: 0.4223, 130: 0.4167, 140: 0.4140, 150: 0.4157,
}
for ep, v in logged.items():
    print(f"epoch {ep:>3}: {v:.4f}")
