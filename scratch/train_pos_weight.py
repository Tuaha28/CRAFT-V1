"""
Experiment (not adopted): pos_weight on delay's BCE loss in the split-encoder
architecture. Projection/graph-encoder weights are separate, but the branches
share one trainable op_type_embedding, one optimizer, one LR scheduler, and
combined-loss checkpoint selection. Reweighting delay can therefore affect
completion/priority; this experiment does not establish parameter isolation.

pos_weight computed from the REAL train-set class balance, not guessed:
735/7001 = 10.50% positive rate on train -> pos_weight = (7001-735)/735 =
8.5252 (standard BCEWithLogitsLoss formula, neg/pos).

RECALIBRATED EXPECTATIONS (per chat, 2026-08-23): the DGP-ceiling
simulation (scratch/delay_dgp_ceiling.py, DELAY_LABEL_FIX_PROPOSAL.md)
found the theoretical best-possible AP for this label is ~0.1574 given
perfect knowledge of the true causal inputs -- and the current production
checkpoint's AP (0.1723) is already AT OR ABOVE that estimate. So this
experiment's honest bar for success is "does it hold up near the current
level without hurting completion/priority," not "does it beat 0.1723 by a
lot" -- there is little room left to gain on delay specifically, by the
project's own measurement.

Otherwise identical to models/train.py: same graph/features/split, same
completion Huber(delta=0.4)/priority MSE, same optimizer/scheduler, same
150-epoch budget. Checkpoint-resume included (same reliability practice as
scratch/train_split_encoder.py) given the multi-hour cost on this
2-core machine.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import psutil

from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN, COMPLETION_SCALE_MINUTES
from utils.logger import get_logger
from utils.config import OUTPUT_DIR, GNN_EPOCHS, GNN_LEARNING_RATE

logger = get_logger("gnn_pos_weight_trainer", "gnn_pos_weight_training.log")

CHECKPOINT_EVERY = 10
CHECKPOINT_DIR = os.path.join(OUTPUT_DIR, "checkpoints")
BEST_CHECKPOINT_PATH = os.path.join(CHECKPOINT_DIR, "gnn_pos_weight_delay_best.pth")
RESUME_CHECKPOINT_PATH = os.path.join(CHECKPOINT_DIR, "gnn_pos_weight_delay_resume.pth")


def set_seed(seed=42):
    torch.manual_seed(seed)
    np.random.seed(seed)


def main(epochs=GNN_EPOCHS, lr=GNN_LEARNING_RATE):
    set_seed(42)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    _proc = psutil.Process()

    logger.info("Building graph (identical to production split-encoder baseline)...")
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph()

    train_mask = hetero_graph['operation'].train_mask
    val_mask = hetero_graph['operation'].val_mask
    op_type_idx = hetero_graph['operation'].type_idx

    model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata())
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10)

    # pos_weight computed from the REAL train-set class balance (measured
    # this session, see module docstring) -- not a guessed constant.
    y_train_delay = labels['delay'][train_mask]
    n_pos = y_train_delay.sum().item()
    n_neg = y_train_delay.numel() - n_pos
    pos_weight_value = n_neg / n_pos
    logger.info(f"delay pos_weight computed from train set: {pos_weight_value:.4f} "
                f"({int(n_pos)} pos / {int(n_neg)} neg)")
    criterion_delay = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight_value))
    criterion_completion = nn.HuberLoss(delta=0.4)  # unchanged, matches production
    criterion_priority = nn.MSELoss()               # unchanged, matches production

    def compute_losses(out, mask):
        loss_delay = criterion_delay(out['delay'][mask], labels['delay'][mask])
        loss_completion = criterion_completion(
            out['completion'][mask] / COMPLETION_SCALE_MINUTES,
            labels['completion'][mask] / COMPLETION_SCALE_MINUTES,
        )
        loss_priority = criterion_priority(out['priority'][mask], labels['priority'][mask])
        total = loss_delay + loss_completion + loss_priority
        return total, loss_delay, loss_completion, loss_priority

    start_epoch = 1
    best_val_loss = float('inf')

    if os.path.exists(RESUME_CHECKPOINT_PATH):
        logger.info(f"Resume checkpoint found at {RESUME_CHECKPOINT_PATH} -- resuming.")
        ckpt = torch.load(RESUME_CHECKPOINT_PATH)
        model.load_state_dict(ckpt['model_state'])
        optimizer.load_state_dict(ckpt['optimizer_state'])
        scheduler.load_state_dict(ckpt['scheduler_state'])
        start_epoch = ckpt['epoch'] + 1
        best_val_loss = ckpt['best_val_loss']
        logger.info(f"Resuming from epoch {start_epoch}, best_val_loss so far = {best_val_loss:.4f}")
    else:
        logger.info("No resume checkpoint found -- starting fresh from epoch 1.")

    logger.info(f"Starting training loop (epochs {start_epoch}..{epochs})...")

    for epoch in range(start_epoch, epochs + 1):
        model.train()
        optimizer.zero_grad()

        out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)
        total_loss, loss_delay, loss_completion, loss_priority = compute_losses(out, train_mask)

        total_loss.backward()
        optimizer.step()

        model.eval()
        with torch.no_grad():
            val_out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)
            val_total_loss, val_delay, val_completion, val_priority = compute_losses(val_out, val_mask)

        scheduler.step(val_total_loss.item())

        if val_total_loss.item() < best_val_loss:
            best_val_loss = val_total_loss.item()
            torch.save(model.state_dict(), BEST_CHECKPOINT_PATH)

        mem_rss_mb = _proc.memory_info().rss / (1024 * 1024)
        logger.info(f"Epoch {epoch:03d}/{epochs} mem_rss_mb={mem_rss_mb:.1f}")

        if epoch % 10 == 0 or epoch == 1:
            logger.info(
                f"Epoch {epoch:03d}/{epochs} | "
                f"Train Loss: {total_loss.item():.4f} (delay {loss_delay.item():.4f} | "
                f"completion {loss_completion.item():.4f} | priority {loss_priority.item():.4f}) | "
                f"Val Loss: {val_total_loss.item():.4f} (delay {val_delay.item():.4f} | "
                f"completion {val_completion.item():.4f} | priority {val_priority.item():.4f})"
            )

        if epoch % CHECKPOINT_EVERY == 0:
            torch.save({
                'model_state': model.state_dict(),
                'optimizer_state': optimizer.state_dict(),
                'scheduler_state': scheduler.state_dict(),
                'epoch': epoch,
                'best_val_loss': best_val_loss,
            }, RESUME_CHECKPOINT_PATH)
            logger.info(f"Resume checkpoint written at epoch {epoch}.")

    logger.info(f"Training Complete! Best model (by val loss) saved to {BEST_CHECKPOINT_PATH}")
    return model


if __name__ == "__main__":
    print("Starting pos_weight delay experiment (split-encoder architecture, experiment not yet adopted)...")
    main()
    print("\n[OK] Training completed successfully!")
