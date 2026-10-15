"""Leak-free GNN v3 training path: train/validation only, delay objective only."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import random
import time

import psutil

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from graph.graph_builder_v3 import FactoryGraphBuilderV3
from models.gnn import MultiTaskFactoryGNN, node_input_dimensions_from_graph
from utils.config import (
    DEVICE,
    GNN_DROPOUT,
    GNN_EPOCHS,
    GNN_HIDDEN_CHANNELS,
    GNN_LEARNING_RATE,
)

V3_NODE_INPUT_WIDTHS = {"machine": 1, "worker": 4, "operation": 7}
V3_ENABLED_HEADS = ("delay",)
V3_CHECKPOINT_SELECTION = "minimum_validation_delay_bce"
V3_EARLY_STOPPING = None
V3_DELAY_POS_WEIGHT = 1.0


@dataclass(frozen=True)
class V3TrainingConfig:
    seed: int = 42
    max_epochs: int = GNN_EPOCHS
    learning_rate: float = GNN_LEARNING_RATE
    weight_decay: float = 1e-4
    hidden_channels: int = GNN_HIDDEN_CHANNELS
    dropout: float = GNN_DROPOUT
    device: str = str(DEVICE)


def set_v3_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def assert_v3_training_contract(train_bundle, validation_bundle, builder) -> dict:
    for name, bundle in (
        ("train", train_bundle),
        ("validation", validation_bundle),
    ):
        dimensions = node_input_dimensions_from_graph(bundle.graph)
        if dimensions != V3_NODE_INPUT_WIDTHS:
            raise AssertionError(
                f"{name} graph dimensions {dimensions} != {V3_NODE_INPUT_WIDTHS}"
            )
        if bundle.graph["operation"].x.shape[1] != 7:
            raise AssertionError("v3 operation schema width must be 7")
        if not bool(bundle.graph["operation"].delay_valid_mask.any()):
            raise AssertionError(f"{name} graph has no valid delay labels")
    if builder.label_access.test_accessed:
        raise RuntimeError("Test labels were accessed during v3 training setup")
    if builder.label_access._checkpoint_sha256 is not None:
        raise RuntimeError("Training setup must not seal a final checkpoint")
    requests = [name for name, _ in builder.label_access.requests]
    if requests != ["train", "validation"]:
        raise RuntimeError(f"Unexpected v3 label access sequence: {requests}")
    return V3_NODE_INPUT_WIDTHS.copy()


def _delay_metrics(delay_logits, labels, valid_mask, bins: int = 10) -> dict:
    y_true = labels["delay"][valid_mask].detach().cpu().numpy().ravel()
    probabilities = torch.sigmoid(delay_logits[valid_mask]).detach().cpu().numpy().ravel()
    if len(y_true) == 0:
        raise RuntimeError("No valid delay labels for metrics")
    metrics = {
        "validation_ap": float(average_precision_score(y_true, probabilities)),
        "validation_roc_auc": float(roc_auc_score(y_true, probabilities)),
        "validation_brier_score": float(brier_score_loss(y_true, probabilities)),
    }
    ece = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        mask = (
            (probabilities >= lower) & (probabilities < upper)
            if index < bins - 1
            else (probabilities >= lower) & (probabilities <= upper)
        )
        if mask.any():
            ece += float(mask.mean() * abs(probabilities[mask].mean() - y_true[mask].mean()))
    metrics["validation_ece_10"] = float(ece)
    return metrics


def _delay_loss(model_output, labels, valid_mask, criterion):
    if set(model_output) != {"delay"}:
        raise AssertionError("v3 objective permits only the delay head")
    if int(valid_mask.sum()) == 0:
        raise RuntimeError("No valid delay labels for loss")
    return criterion(
        model_output["delay"][valid_mask],
        labels["delay"][valid_mask],
    )


def prepare_v3_training(config: V3TrainingConfig = V3TrainingConfig()):
    """Build only train/validation graphs and instantiate from their widths."""
    set_v3_seed(config.seed)
    builder = FactoryGraphBuilderV3(seed=config.seed)
    train, validation = builder.build_train_validation()
    dimensions = assert_v3_training_contract(train, validation, builder)

    device = torch.device(config.device)
    train_graph = train.graph.to(device)
    validation_graph = validation.graph.to(device)
    train_labels = {name: value.to(device) for name, value in train.labels.items()}
    validation_labels = {
        name: value.to(device) for name, value in validation.labels.items()
    }
    model = MultiTaskFactoryGNN.from_graph(
        train_graph,
        hidden_channels=config.hidden_channels,
        dropout=config.dropout,
    ).to(device)
    if model.input_dimensions != dimensions:
        raise AssertionError("Model dimensions differ from v3 graph contract")
    optimizer = optim.Adam(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=0.5, patience=10
    )
    train_valid = train_graph["operation"].delay_valid_mask
    positives = int(train_labels["delay"][train_valid].sum().item())
    negatives = int(train_valid.sum().item()) - positives
    if positives + negatives <= 0:
        raise RuntimeError("No valid training delay labels")
    # Deliberately unweighted: raw sigmoid output is the deployment risk
    # probability under the observed high-prevalence STRESS regime.
    criterion = nn.BCEWithLogitsLoss()
    return {
        "config": config,
        "builder": builder,
        "train": train,
        "validation": validation,
        "train_graph": train_graph,
        "validation_graph": validation_graph,
        "train_labels": train_labels,
        "validation_labels": validation_labels,
        "model": model,
        "optimizer": optimizer,
        "scheduler": scheduler,
        "criterion": criterion,
        "delay_pos_weight": V3_DELAY_POS_WEIGHT,
        "train_delay_positives": positives,
        "train_delay_negatives": negatives,
        "train_delay_prevalence": positives / (positives + negatives),
    }


def smoke_train_validation_step_v3(
    config: V3TrainingConfig = V3TrainingConfig(),
) -> dict:
    """One real train step plus one validation forward; never opens test labels."""
    state = prepare_v3_training(config)
    model = state["model"]
    train_graph = state["train_graph"]
    validation_graph = state["validation_graph"]

    model.train()
    state["optimizer"].zero_grad()
    train_output = model(
        train_graph.x_dict,
        train_graph.edge_index_dict,
        train_graph["operation"].type_idx,
        enabled_heads=V3_ENABLED_HEADS,
    )
    train_loss = _delay_loss(
        train_output,
        state["train_labels"],
        train_graph["operation"].delay_valid_mask,
        state["criterion"],
    )
    if not torch.isfinite(train_loss):
        raise RuntimeError("Non-finite v3 training loss")
    train_loss.backward()
    state["optimizer"].step()

    model.eval()
    with torch.no_grad():
        validation_output = model(
            validation_graph.x_dict,
            validation_graph.edge_index_dict,
            validation_graph["operation"].type_idx,
            enabled_heads=V3_ENABLED_HEADS,
        )
        validation_loss = _delay_loss(
            validation_output,
            state["validation_labels"],
            validation_graph["operation"].delay_valid_mask,
            state["criterion"],
        )
    if not torch.isfinite(validation_loss):
        raise RuntimeError("Non-finite v3 validation loss")
    if state["builder"].label_access.test_accessed:
        raise RuntimeError("Test labels opened during v3 smoke run")
    return {
        "objective": "delay_only",
        "enabled_heads": list(V3_ENABLED_HEADS),
        "train_loss": float(train_loss.detach().cpu()),
        "validation_loss": float(validation_loss.detach().cpu()),
        "delay_pos_weight": state["delay_pos_weight"],
        "train_delay_prevalence": state["train_delay_prevalence"],
        "validation_metrics": _delay_metrics(
            validation_output["delay"],
            state["validation_labels"],
            validation_graph["operation"].delay_valid_mask,
        ),
        "input_dimensions": state["model"].input_dimensions,
        "label_requests": [
            name for name, _ in state["builder"].label_access.requests
        ],
        "test_labels_opened": state["builder"].label_access.test_accessed,
    }


def train_gnn_v3(config: V3TrainingConfig = V3TrainingConfig()):
    """Full deterministic v3 trainer; train/validation labels only."""
    state = prepare_v3_training(config)
    best_validation_loss = float("inf")
    best_epoch = None
    history = []
    run_started = time.perf_counter()
    output_dir = Path("outputs/gnn_v3_leakfree")
    checkpoint_path = output_dir / "checkpoints/gnn_v3_delay_only.pth"
    history_path = output_dir / "training_history.json"
    for epoch in range(1, config.max_epochs + 1):
        epoch_started = time.perf_counter()
        learning_rate = state["optimizer"].param_groups[0]["lr"]
        state["model"].train()
        state["optimizer"].zero_grad()
        output = state["model"](
            state["train_graph"].x_dict,
            state["train_graph"].edge_index_dict,
            state["train_graph"]["operation"].type_idx,
            enabled_heads=V3_ENABLED_HEADS,
        )
        train_loss = _delay_loss(
            output, state["train_labels"],
            state["train_graph"]["operation"].delay_valid_mask,
            state["criterion"],
        )
        if not torch.isfinite(train_loss):
            raise RuntimeError(f"Non-finite training loss at epoch {epoch}")
        train_loss.backward()
        state["optimizer"].step()

        state["model"].eval()
        with torch.no_grad():
            validation_output = state["model"](
                state["validation_graph"].x_dict,
                state["validation_graph"].edge_index_dict,
                state["validation_graph"]["operation"].type_idx,
                enabled_heads=V3_ENABLED_HEADS,
            )
            validation_loss = _delay_loss(
                validation_output, state["validation_labels"],
                state["validation_graph"]["operation"].delay_valid_mask,
                state["criterion"],
            )
        if not torch.isfinite(validation_loss):
            raise RuntimeError(f"Non-finite validation loss at epoch {epoch}")
        validation_value = float(validation_loss.cpu())
        metrics = _delay_metrics(
            validation_output["delay"], state["validation_labels"],
            state["validation_graph"]["operation"].delay_valid_mask,
        )
        state["scheduler"].step(validation_value)
        record = {
            "epoch": epoch,
            "train_delay_bce": float(train_loss.detach().cpu()),
            "validation_delay_bce": validation_value,
            "learning_rate": learning_rate,
            "epoch_runtime_seconds": time.perf_counter() - epoch_started,
            "rss_bytes": psutil.Process().memory_info().rss,
            **metrics,
        }
        history.append(record)
        output_dir.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        if validation_value < best_validation_loss:
            best_validation_loss = validation_value
            best_epoch = epoch
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({
                "model_state_dict": state["model"].state_dict(),
                "best_epoch": best_epoch,
                "validation_delay_bce": best_validation_loss,
                "input_dimensions": state["model"].input_dimensions,
                "enabled_heads": V3_ENABLED_HEADS,
                "delay_pos_weight": V3_DELAY_POS_WEIGHT,
                "training_config": asdict(config),
            }, checkpoint_path)
    if state["builder"].label_access.test_accessed:
        raise RuntimeError("Test labels opened during v3 training")
    summary = {
        "checkpoint_path": str(checkpoint_path),
        "best_epoch": best_epoch,
        "best_validation_delay_bce": best_validation_loss,
        "history_path": str(history_path),
        "runtime_seconds": time.perf_counter() - run_started,
        "delay_pos_weight": V3_DELAY_POS_WEIGHT,
        "test_labels_opened": False,
    }
    (output_dir / "training_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return {**summary, "history": history}
