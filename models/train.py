import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import hashlib
import json
import os
import sys
import psutil

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.graph_builder import FactoryGraphBuilder, SPLIT_SEED, VAL_FRACTION
from models.gnn import MultiTaskFactoryGNN, COMPLETION_SCALE_MINUTES
from utils.logger import get_logger
from utils.config import OUTPUT_DIR, GNN_EPOCHS, GNN_LEARNING_RATE, DEVICE

logger = get_logger("gnn_trainer", "gnn_training.log")

def set_seed(seed=42):
    torch.manual_seed(seed)
    np.random.seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

def train_gnn(epochs=GNN_EPOCHS, lr=GNN_LEARNING_RATE):
    set_seed(42)
    logger.info("Initializing Training Pipeline...")
    
    # 1. Build Graph Data
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph()

    # DEVICE PLACEMENT (audit #8). utils/config.py has defined DEVICE since
    # the first commit and NOTHING ever read it -- there was no .to(device)
    # call anywhere in models/, graph/, ga/ or agent/, so training and
    # inference ran on CPU regardless of hardware. Moved here, once, for the
    # whole graph and every label tensor, so model and data cannot end up on
    # different devices.
    device = torch.device(DEVICE)
    logger.info(f"Training on device: {device}")
    hetero_graph = hetero_graph.to(device)
    labels = {k: v.to(device) for k, v in labels.items()}

    # IMPORTANT: without this split, the model was being trained AND
    # evaluated (in evaluate.py) on identical data — every "accuracy" or
    # "MAE" reported was training-set performance, not a measure of how well
    # the model generalizes to operations it hasn't seen. train_mask/val_mask
    # come from FactoryGraphBuilder.build_graph() (graph/graph_builder.py).
    train_mask = hetero_graph['operation'].train_mask
    val_mask = hetero_graph['operation'].val_mask
    # Head 1 validity (label-audit fix 2): operations with no recorded
    # actual outcome, or only a partially-observed set of lots, have NO
    # delay label -- they used to sit at the label array's 0.0 default and
    # train as true negatives. Intersected with each split below so they
    # contribute to neither the loss nor any reported metric.
    delay_valid = labels['delay_valid'].view(-1)
    train_mask = train_mask.to(device)
    val_mask = val_mask.to(device)
    delay_train_mask = train_mask & delay_valid
    delay_val_mask = val_mask & delay_valid
    # Head 2 validity (Batch-4 fix 10c): operations with no planned span have
    # no elapsed-span label. They used to receive a work-content substitute,
    # a different physical quantity; now they are excluded from this head's
    # loss the same way missing outcomes are excluded from the delay head.
    completion_valid = labels.get('completion_valid')
    if completion_valid is None:
        completion_valid = torch.ones_like(delay_valid)
    else:
        completion_valid = completion_valid.view(-1).to(device)
    completion_train_mask = train_mask & completion_valid
    completion_val_mask = val_mask & completion_valid
    logger.info(
        f"Delay-head usable labels: {int(delay_train_mask.sum())}/{int(train_mask.sum())} train, "
        f"{int(delay_val_mask.sum())}/{int(val_mask.sum())} val "
        f"({int((~delay_valid).sum())} operations masked out entirely)."
    )
    # operation_type index for the GNN's learned embedding -- not part of
    # x_dict (see graph/graph_builder.py), passed into every forward() call
    # explicitly.
    op_type_idx = hetero_graph['operation'].type_idx

    # 2. Initialize Model and Optimizer
    model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata(), operation_in_dim=builder.feature_schema['count']).to(device)
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10)
    
    # 3. Loss Functions
    #
    # pos_weight DERIVED AT RUNTIME from the training split's own corrected,
    # masked labels (label-audit fix 4). Plain BCEWithLogitsLoss on this
    # class balance optimises toward predicting all-negative -- which is
    # exactly what the previous run produced (precision/recall/F1 all
    # 0.0000 at 88.8% accuracy, a trivial always-negative classifier).
    # pos_weight = negatives/positives restores the positive class's
    # contribution to the gradient.
    #
    # Computed from the TRAIN split only (never val) and from labels after
    # per-head validity masking. The operation-level target is
    # max(actual_end) - max(planned_end), deliberately not max(actual-planned)
    # across lots. The ratio may be below one when positives are the majority;
    # BCEWithLogitsLoss supports that and the saved value remains part of the
    # probability-calibration contract.
    # A previous experiment (DELAY_LABEL_FIX_PROPOSAL.md) measured
    # pos_weight=8.5252 as marginally worse on AP, but that ran against the
    # old biased labels without masking -- it does not carry over to the
    # corrected label and is not evidence either way here. Whether this
    # helps is a measurement to make after the retrain.
    n_pos = int(labels['delay'][delay_train_mask].sum())
    n_neg = int(delay_train_mask.sum()) - n_pos
    if n_pos > 0 and n_neg > 0:
        pos_weight_value = n_neg / n_pos
        criterion_delay = nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor([pos_weight_value], dtype=torch.float, device=device)
        )
    else:
        # One class entirely absent from the training split -- a weight is
        # undefined and any value would be arbitrary. Logged rather than
        # silently substituted.
        pos_weight_value = None
        criterion_delay = nn.BCEWithLogitsLoss()
    logger.info(
        f"Delay head class balance (train, corrected+masked labels): "
        f"{n_pos} positive / {n_neg} negative "
        f"({100.0 * n_pos / max(1, n_pos + n_neg):.2f}% positive) -> "
        f"pos_weight={pos_weight_value if pos_weight_value is None else f'{pos_weight_value:.4f}'}"
    )
    # delta RE-DERIVED (model-module audit, see chat) -- 1.0 was PyTorch's
    # own library default, inherited unchanged since the first commit, from
    # BEFORE completion's target was rescaled by /1000 (0H.13/0H.14). On
    # the rescaled label Huber actually trains against, measured directly
    # against the live graph: mean=0.884, std=0.687 (n=8,745). At delta=1.0,
    # that std sits WELL inside the quadratic region -- and the already-
    # trained model's own achieved residuals (MAE=0.295, RMSE=0.425
    # rescaled, from the last full run) confirm it: typical post-training
    # error never approaches 1.0, so Huber has been behaving as plain MSE
    # for this problem the entire time, never engaging its robust/linear
    # region at all. delta=0.4 chosen to actually put a boundary inside the
    # real error range: close to the achieved RMSE (0.425, the natural
    # "typical residual" scale for a reasonably-fit model) and to ~0.6x the
    # label's own std (0.687) -- both land in the same 0.3-0.45
    # neighbourhood, giving convergent support for it. This is a reasoned
    # choice grounded in the real distribution, not a single provably-
    # optimal value -- measure, don't assume the direction of the effect.
    criterion_completion = nn.HuberLoss(delta=0.4)
    criterion_priority = nn.MSELoss()
    
    logger.info("Starting Training Loop...")
    
    best_val_loss = float('inf')
    checkpoint_dir = os.path.join(OUTPUT_DIR, "checkpoints")
    os.makedirs(checkpoint_dir, exist_ok=True)
    checkpoint_path = os.path.join(checkpoint_dir, "gnn_multitask_latest.pth")

    # Memory-per-epoch logging (see chat -- background retrains were being
    # killed by the OS, fully dead not suspended, twice in a row at a
    # consistent ~60-75min mark: the classic signature of unbounded memory
    # growth). Code-reviewed the training loop line by line first for the
    # obvious cause (a loss/output tensor accumulated across epochs without
    # .item()/.detach()) -- found none: every per-epoch tensor here is a
    # local reassigned fresh each iteration, and every place a loss crosses
    # the loop boundary already calls .item(). This logs actual RSS so the
    # question is settled with real per-epoch data instead of inferred from
    # the code alone -- if a leak exists somewhere less obvious (PyG/PyTorch
    # internals, OS-level, etc.), a monotonic climb will show up here
    # directly; if memory is flat, that rules out a leak as this failure's
    # cause entirely.
    _proc = psutil.Process()

    def compute_losses(out, mask, delay_mask, completion_mask=None):
        # Message passing runs over the whole graph (transductive setting);
        # only the loss is restricted to the nodes in `mask`.
        #
        # delay_mask is `mask` further intersected with label validity
        # (label-audit fix 2) -- Heads 2 and 3 have a defined target for
        # every operation, Head 1 does not. An empty delay_mask would make
        # BCE return nan and poison the total, so it contributes zero
        # instead; that can only happen if this split has no usable delay
        # label at all, which the startup log above would already show.
        if int(delay_mask.sum()) == 0:
            loss_delay = torch.zeros((), dtype=torch.float, device=device)
        else:
            loss_delay = criterion_delay(out['delay'][delay_mask], labels['delay'][delay_mask])
        # Both sides divided by the SAME scale the model itself uses
        # internally (models/gnn.py::COMPLETION_SCALE_MINUTES) -- keeps
        # this head's loss/gradient on a comparable scale to delay/
        # priority's without an arbitrary down-weight. Replaces the
        # previous `0.001 * loss_completion`: measured (PROJECT_STATUS.txt
        # Section 0H.13) to crush this head's gradient into the shared
        # encoder to ~1/1000th of the other two heads', every epoch, which
        # was never the intent -- the 0.001 was chosen to balance the
        # combined LOSS VALUE, and did that, but as a side effect also
        # suppressed the GRADIENT by the same factor. out['completion'] is
        # still raw minutes (the model rescales back up internally before
        # returning it), so this line, not the model's public output, is
        # the only place that changed.
        cmask = mask if completion_mask is None else completion_mask
        if int(cmask.sum()) == 0:
            loss_completion = torch.zeros((), dtype=torch.float, device=device)
        else:
            loss_completion = criterion_completion(
                out['completion'][cmask] / COMPLETION_SCALE_MINUTES,
                labels['completion'][cmask] / COMPLETION_SCALE_MINUTES,
            )
        loss_priority = criterion_priority(out['priority'][mask], labels['priority'][mask])
        total = loss_delay + loss_completion + loss_priority  # parity -- all weight 1.0
        return total, loss_delay, loss_completion, loss_priority
    
    for epoch in range(1, epochs + 1):
        model.train()
        optimizer.zero_grad()
        
        out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)
        total_loss, loss_delay, loss_completion, loss_priority = compute_losses(
            out, train_mask, delay_train_mask, completion_train_mask)

        total_loss.backward()
        optimizer.step()

        # Held-out validation pass (no grad, no dropout)
        model.eval()
        with torch.no_grad():
            val_out = model(hetero_graph.x_dict, hetero_graph.edge_index_dict, op_type_idx)
            val_total_loss, val_delay, val_completion, val_priority = compute_losses(
                val_out, val_mask, delay_val_mask, completion_val_mask)
        
        # Pass Python float to scheduler to prevent PyTorch tensor warning
        scheduler.step(val_total_loss.item())
        
        if val_total_loss.item() < best_val_loss:
            best_val_loss = val_total_loss.item()
            torch.save(model.state_dict(), checkpoint_path)
            from graph.feature_schema import save_preprocessing
            save_preprocessing(
                checkpoint_path, builder.feature_schema, builder.breakdown_scale,
                pos_weight_value,
            )

        # Every epoch, deliberately not throttled to the %10 cadence below --
        # a leak needs fine granularity to be provable, not just visible in
        # hindsight after the fact.
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
            
    logger.info(f"Training Complete! Best model (by val loss) saved to {checkpoint_path}")

    # PIPELINE_METHODOLOGY_TRACE.md Stage 7 fix: persist the ACTUAL
    # production_order_ids in this run's val split, not just the boolean
    # mask -- the mask is only meaningful attached to the exact same graph
    # node ordering it was computed against, which is not guaranteed to
    # survive a later, independently-rebuilt graph (a live DB can change
    # between a training run and a later evaluation run). models/evaluate.py
    # reloads this file and re-derives its OWN val_mask by matching order
    # IDs, rather than re-deriving the split via _build_split_masks -- so a
    # training run's reported val metrics and a later evaluate.py run
    # against the same checkpoint score the identical held-out orders.
    # SUPERSEDED by the split manifest below (label-audit fix 5). The old
    # outputs/val_order_ids.json recorded split_seed and val_fraction but
    # nothing that could actually detect a stale file: models/evaluate.py
    # branched only on the file EXISTING, never verified the two fields it
    # did record, and stored no graph or checkpoint identity at all. A
    # reseed that regenerates the same production_order_id range therefore
    # matched every recorded ID and loaded silently against a checkpoint
    # trained on a different split.
    val_order_ids = sorted(
        {str(oid) for oid in builder.order_ids_per_node[val_mask.cpu().numpy()].tolist()}
    )
    with open(checkpoint_path, "rb") as f:
        checkpoint_sha256 = hashlib.sha256(f.read()).hexdigest()

    manifest = {
        "split_seed": SPLIT_SEED,
        "val_fraction": VAL_FRACTION,
        "num_operation_nodes": int(train_mask.numel()),
        "num_train_operations": int(train_mask.sum()),
        "num_val_operations": int(val_mask.sum()),
        "graph_fingerprint": builder.graph_fingerprint(),
        "checkpoint_file": os.path.basename(checkpoint_path),
        "checkpoint_sha256": checkpoint_sha256,
        "num_val_orders": len(val_order_ids),
        "production_order_ids": val_order_ids,
        # Head 3's frozen slack->percentile curve (label-audit fix 1), so
        # evaluation and inference score priority against exactly the curve
        # this training run used instead of re-deriving one.
        "priority_cutpoints": builder.label_meta.get('priority_cutpoints'),
        # Train-derived feature normaliser (audit #13), so evaluation and
        # inference scale on the training run's value instead of refitting.
        "breakdown_scale": getattr(builder, "breakdown_scale", None),
        "priority_cutpoints_source": builder.label_meta.get('priority_cutpoints_source'),
        # Head 1 label-validity bookkeeping (label-audit fix 2).
        "delay_valid_count": builder.label_meta.get('delay_valid_count'),
        "delay_masked_no_outcome": builder.label_meta.get('delay_masked_no_outcome'),
        "delay_masked_partial": builder.label_meta.get('delay_masked_partial'),
        "completion_fallback_count": builder.label_meta.get('completion_fallback_count'),
        "delay_pos_weight": pos_weight_value,
    }
    manifest_path = os.path.join(OUTPUT_DIR, "split_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info(
        f"Split manifest written to {manifest_path} "
        f"({len(val_order_ids)} val orders, graph {manifest['graph_fingerprint'][:12]}..., "
        f"checkpoint {checkpoint_sha256[:12]}...)"
    )

    return model

if __name__ == "__main__":
    print("Starting Multi-Task GNN Training...")
    trained_model = train_gnn()
    print("\n[OK] Training completed successfully!")
