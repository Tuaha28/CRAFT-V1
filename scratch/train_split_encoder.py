"""
Training script for the split-encoder architecture EXPERIMENT
(scratch/gnn_split_encoder.py) -- controlled comparison against the
Option B baseline (models/train.py's train_gnn(), the checkpoint just
evaluated). Identical graph, features, labels, split, loss functions/
weights, optimizer config, and epoch budget as production train.py --
the ONLY variable under test is the split encoder.

RELIABILITY, added on top of production train.py's own per-epoch RSS
logging (see chat -- the last diagnostic job that ran on this project
today died from what looks like a system sleep/idle event, not the
historical OOM pattern, so logging alone would not have saved it):
periodic checkpoint-resume. A resumable checkpoint (model + optimizer +
scheduler state + epoch number + best_val_loss) is written every
CHECKPOINT_EVERY epochs, separate from the best-val-loss checkpoint. If
this script is re-run and a resumable checkpoint already exists, it picks
up from that epoch instead of restarting -- at most CHECKPOINT_EVERY
epochs of lost progress from any interruption (OOM, sleep-kill, or
anything else), not the whole run, given this experiment costs ~2x a
single-encoder run (~5-6h estimated, extrapolated from Option B's
150-epoch/2h51m run).
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import psutil

from graph.graph_builder import FactoryGraphBuilder
from models.gnn import COMPLETION_SCALE_MINUTES
from scratch.gnn_split_encoder import SplitEncoderFactoryGNN
from utils.logger import get_logger
from utils.config import OUTPUT_DIR, GNN_EPOCHS, GNN_LEARNING_RATE

CHECKPOINT_EVERY = 10
CHECKPOINT_DIR = os.path.join(OUTPUT_DIR, "checkpoints")


def set_seed(seed):
    torch.manual_seed(seed)
    np.random.seed(seed)


def main(epochs=GNN_EPOCHS, lr=GNN_LEARNING_RATE, seed=42):
    # Seed-suffixed logger/checkpoints -- a second seed run must not
    # interleave its log lines with the first run's (same-named logger
    # would append to the same file) or silently overwrite its checkpoint
    # (see chat: this run exists specifically to check whether seed 42's
    # uniform win across all three heads replicates under a different seed).
    logger = get_logger("gnn_split_encoder_trainer", f"gnn_split_encoder_training_seed{seed}.log")
    best_checkpoint_path = os.path.join(CHECKPOINT_DIR, f"gnn_split_encoder_best_seed{seed}.pth")
    resume_checkpoint_path = os.path.join(CHECKPOINT_DIR, f"gnn_split_encoder_resume_seed{seed}.pth")

    set_seed(seed)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    _proc = psutil.Process()

    logger.info("Building graph (identical to Option B baseline)...")
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph()

    train_mask = hetero_graph['operation'].train_mask
    val_mask = hetero_graph['operation'].val_mask
    op_type_idx = hetero_graph['operation'].type_idx

    model = SplitEncoderFactoryGNN(metadata=hetero_graph.metadata())
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10)

    criterion_delay = nn.BCEWithLogitsLoss()
    criterion_completion = nn.HuberLoss(delta=0.4)  # matches models/train.py exactly
    criterion_priority = nn.MSELoss()

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

    if os.path.exists(resume_checkpoint_path):
        logger.info(f"Resume checkpoint found at {resume_checkpoint_path} -- resuming.")
        ckpt = torch.load(resume_checkpoint_path)
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
            torch.save(model.state_dict(), best_checkpoint_path)

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
            }, resume_checkpoint_path)
            logger.info(f"Resume checkpoint written at epoch {epoch}.")

    logger.info(f"Training Complete! Best model (by val loss) saved to {best_checkpoint_path}")
    return model


if __name__ == "__main__":
    seed_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    print(f"Starting Split-Encoder GNN Training (experiment, not production), seed={seed_arg}...")
    main(seed=seed_arg)
    print("\n[OK] Training completed successfully!")
