"""
One-off diagnostic (not part of the pipeline): Optuna hyperparameter search
targeting completion + priority (delay's ceiling is already established as
data-limited across 4 independent investigations in PROJECT_STATUS.txt --
see chat -- so it's not this search's target metric, though its loss is
still computed and backpropagated every trial exactly as in production,
since all three heads share one encoder).

Graph is built ONCE and reused across all trials (avoids re-querying the DB
~25+ times). Each trial trains a fresh model/optimizer from scratch with
sampled hyperparameters, using the SAME parity-weighted 3-head loss
train.py uses for backprop, but only tracks/reports the combined
(completion_loss + priority_loss) as the value Optuna optimizes.

Per-trial memory RSS is logged (see chat -- this project's training runs
have twice been killed by OS-level RAM pressure at ~60-75min marks) so any
cross-trial growth is visible immediately rather than discovered by a dead
process hours in. Study saved to a SQLite file so it survives an
interruption and can be resumed/inspected afterward.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gc
import time
import psutil
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import optuna
from optuna.pruners import MedianPruner

from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN, COMPLETION_SCALE_MINUTES

N_TRIALS = 25
MAX_EPOCHS_PER_TRIAL = 40
SEED = 42
STUDY_DB = "sqlite:///scratchpad_optuna_completion_priority.db"

torch.manual_seed(SEED)
np.random.seed(SEED)

_proc = psutil.Process()

print("Building graph once (reused across all trials)...")
builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()

train_mask = hetero_graph['operation'].train_mask
val_mask = hetero_graph['operation'].val_mask
op_type_idx = hetero_graph['operation'].type_idx

criterion_delay = nn.BCEWithLogitsLoss()
criterion_priority = nn.MSELoss()


def compute_losses(model, huber_delta, mask):
    criterion_completion = nn.HuberLoss(delta=huber_delta)
    out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)
    loss_delay = criterion_delay(out['delay'][mask], labels['delay'][mask])
    loss_completion = criterion_completion(
        out['completion'][mask] / COMPLETION_SCALE_MINUTES,
        labels['completion'][mask] / COMPLETION_SCALE_MINUTES,
    )
    loss_priority = criterion_priority(out['priority'][mask], labels['priority'][mask])
    total = loss_delay + loss_completion + loss_priority  # parity, matches train.py exactly
    return total, loss_delay, loss_completion, loss_priority


def objective(trial):
    hidden_channels = trial.suggest_categorical("hidden_channels", [32, 64, 96, 128])
    dropout = trial.suggest_float("dropout", 0.1, 0.5)
    lr = trial.suggest_float("lr", 1e-4, 1e-2, log=True)
    weight_decay = trial.suggest_float("weight_decay", 1e-6, 1e-3, log=True)
    huber_delta = trial.suggest_float("huber_delta", 0.2, 0.8)

    model = MultiTaskFactoryGNN(
        metadata=hetero_graph.metadata(),
        hidden_channels=hidden_channels,
        dropout=dropout,
    )
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10)

    best_combined = float('inf')
    t0 = time.time()
    for epoch in range(1, MAX_EPOCHS_PER_TRIAL + 1):
        model.train()
        optimizer.zero_grad()
        total, _, _, _ = compute_losses(model, huber_delta, train_mask)
        total.backward()
        optimizer.step()

        model.eval()
        with torch.no_grad():
            val_total, _, val_completion, val_priority = compute_losses(model, huber_delta, val_mask)
        scheduler.step(val_total.item())

        combined = val_completion.item() + val_priority.item()
        best_combined = min(best_combined, combined)

        trial.report(combined, epoch)
        if trial.should_prune():
            del model, optimizer
            gc.collect()
            raise optuna.TrialPruned()

    elapsed = time.time() - t0
    mem_rss_mb = _proc.memory_info().rss / (1024 * 1024)
    print(f"[trial {trial.number}] done in {elapsed:.0f}s, mem_rss_mb={mem_rss_mb:.1f}, "
          f"best_combined={best_combined:.4f}, params={trial.params}")

    del model, optimizer
    gc.collect()

    return best_combined


if __name__ == "__main__":
    pruner = MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    study = optuna.create_study(
        study_name="completion_priority_hpo",
        storage=STUDY_DB,
        direction="minimize",
        pruner=pruner,
        load_if_exists=True,
    )
    study.optimize(objective, n_trials=N_TRIALS)

    print("\n" + "="*70)
    print("BEST TRIAL")
    print("="*70)
    print(f"value (val completion_loss + priority_loss): {study.best_value:.4f}")
    print(f"params: {study.best_params}")

    print("\nALL TRIALS SUMMARY:")
    for t in study.trials:
        status = t.state.name
        print(f"  trial {t.number:>2} [{status:<8}] value={t.value} params={t.params}")

    print("\nDONE")
