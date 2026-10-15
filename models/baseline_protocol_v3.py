"""Validation-only, leak-free delay-risk baseline protocol for GNN v3."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

import joblib
import numpy as np
import torch
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, average_precision_score, brier_score_loss, confusion_matrix, f1_score, log_loss, precision_score, recall_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

from graph.graph_builder_v3 import FactoryGraphBuilderV3
from models.gnn import MultiTaskFactoryGNN
from models.train_v3 import V3TrainingConfig, set_v3_seed


OUTPUT_DIR = Path("outputs/gnn_v3_leakfree")
CHECKPOINT = OUTPUT_DIR / "checkpoints/gnn_v3_delay_only.pth"
EXPECTED_CHECKPOINT_SHA256 = "6a00a92d5f58b39d367fa6a6f02e9599b0bfd7e5b83055e75762d5ecb99c5b1a"
THRESHOLD = 0.5


@dataclass(frozen=True)
class LogisticCandidate:
    c: float


@dataclass(frozen=True)
class RandomForestCandidate:
    n_estimators: int
    max_depth: int | None
    min_samples_leaf: int
    max_features: str | float


LOGISTIC_CANDIDATES = tuple(LogisticCandidate(c) for c in (0.01, 0.1, 1.0, 10.0))
RANDOM_FOREST_CANDIDATES = (
    RandomForestCandidate(300, None, 1, "sqrt"),
    RandomForestCandidate(300, 16, 1, "sqrt"),
    RandomForestCandidate(300, None, 10, "sqrt"),
    RandomForestCandidate(300, 16, 10, "sqrt"),
)


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _ece(y_true: np.ndarray, probability: np.ndarray, bins: int = 10) -> float:
    value = 0.0
    for i in range(bins):
        lower, upper = i / bins, (i + 1) / bins
        mask = ((probability >= lower) & (probability < upper)) if i < bins - 1 else ((probability >= lower) & (probability <= upper))
        if mask.any():
            value += float(mask.mean() * abs(probability[mask].mean() - y_true[mask].mean()))
    return value


def delay_metric_report(y_true: np.ndarray, probability: np.ndarray, *, train_prevalence: float) -> dict:
    prediction = (probability >= THRESHOLD).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, prediction, labels=(0, 1)).ravel()
    prevalence = float(y_true.mean())
    ap = float(average_precision_score(y_true, probability))
    return {
        "operation_count": int(len(y_true)), "positive_count": int(y_true.sum()),
        "negative_count": int(len(y_true) - y_true.sum()),
        "validation_prevalence": prevalence, "training_prevalence": float(train_prevalence),
        "threshold": THRESHOLD, "average_precision": ap,
        "ap_prevalence_baseline": prevalence, "ap_lift_over_prevalence": ap - prevalence,
        "roc_auc": float(roc_auc_score(y_true, probability)),
        "brier_score": float(brier_score_loss(y_true, probability)), "ece_10": _ece(y_true, probability),
        "bce": float(log_loss(y_true, probability, labels=(0, 1))),
        "accuracy": float(accuracy_score(y_true, prediction)),
        "precision": float(precision_score(y_true, prediction, zero_division=0)),
        "recall": float(recall_score(y_true, prediction, zero_division=0)),
        "f1": float(f1_score(y_true, prediction, zero_division=0)),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        "probability_distribution": {"min": float(probability.min()), "max": float(probability.max()), "mean": float(probability.mean()), "std": float(probability.std())},
    }


def _safe_train_validation():
    builder = FactoryGraphBuilderV3(seed=42)
    train, validation = builder.build_train_validation()
    if builder.label_access.test_accessed:
        raise RuntimeError("Test labels were opened by baseline protocol")
    if [name for name, _ in builder.label_access.requests] != ["train", "validation"]:
        raise RuntimeError("Baseline protocol label access is not split-scoped")
    train_mask = train.graph["operation"].delay_valid_mask.cpu().numpy().ravel()
    validation_mask = validation.graph["operation"].delay_valid_mask.cpu().numpy().ravel()
    x_train = train.graph["operation"].x.cpu().numpy()[train_mask]
    x_validation = validation.graph["operation"].x.cpu().numpy()[validation_mask]
    y_train = train.labels["delay"].cpu().numpy().ravel()[train_mask].astype(int)
    y_validation = validation.labels["delay"].cpu().numpy().ravel()[validation_mask].astype(int)
    return builder, train, validation, x_train, y_train, x_validation, y_validation


def _select_logistic(x_train, y_train, x_validation, y_validation, train_prevalence):
    scaler = StandardScaler().fit(x_train)
    candidates = []
    for candidate in LOGISTIC_CANDIDATES:
        model = LogisticRegression(C=candidate.c, class_weight=None, solver="lbfgs", max_iter=2000, random_state=42)
        model.fit(scaler.transform(x_train), y_train)
        probability = model.predict_proba(scaler.transform(x_validation))[:, 1]
        candidates.append((delay_metric_report(y_validation, probability, train_prevalence=train_prevalence), candidate, model))
    report, candidate, model = min(candidates, key=lambda item: item[0]["bce"])
    return {"candidate_results": [{"config": asdict(c), "validation": r} for r, c, _ in candidates], "selected_config": asdict(candidate), "validation": report}, scaler, model


def _select_random_forest(x_train, y_train, x_validation, y_validation, train_prevalence):
    candidates = []
    for candidate in RANDOM_FOREST_CANDIDATES:
        model = RandomForestClassifier(n_estimators=candidate.n_estimators, max_depth=candidate.max_depth, min_samples_leaf=candidate.min_samples_leaf, max_features=candidate.max_features, class_weight=None, random_state=42, n_jobs=1)
        model.fit(x_train, y_train)
        probability = model.predict_proba(x_validation)[:, 1]
        candidates.append((delay_metric_report(y_validation, probability, train_prevalence=train_prevalence), candidate, model))
    report, candidate, model = min(candidates, key=lambda item: item[0]["bce"])
    return {"candidate_results": [{"config": asdict(c), "validation": r} for r, c, _ in candidates], "selected_config": asdict(candidate), "validation": report}, model


def _frozen_gnn_validation(train, validation, train_prevalence: float) -> dict:
    checkpoint_hash = _sha256(CHECKPOINT)
    if checkpoint_hash != EXPECTED_CHECKPOINT_SHA256:
        raise RuntimeError("Frozen GNN checkpoint SHA-256 mismatch")
    payload = torch.load(CHECKPOINT, map_location="cpu", weights_only=False)
    if payload["best_epoch"] != 15 or tuple(payload["enabled_heads"]) != ("delay",):
        raise RuntimeError("Unexpected frozen v3 checkpoint metadata")
    config = V3TrainingConfig(**payload["training_config"])
    model = MultiTaskFactoryGNN.from_graph(train.graph, hidden_channels=config.hidden_channels, dropout=config.dropout)
    model.load_state_dict(payload["model_state_dict"])
    model.eval()
    with torch.no_grad():
        output = model(validation.graph.x_dict, validation.graph.edge_index_dict, validation.graph["operation"].type_idx, enabled_heads=("delay",))
        mask = validation.graph["operation"].delay_valid_mask
        probability = torch.sigmoid(output["delay"][mask]).cpu().numpy().ravel()
    y_validation = validation.labels["delay"][mask].cpu().numpy().ravel().astype(int)
    return {"checkpoint_sha256": checkpoint_hash, "best_epoch": int(payload["best_epoch"]), "selection_metric": "minimum_validation_delay_bce", "validation": delay_metric_report(y_validation, probability, train_prevalence=train_prevalence)}


def freeze_validation_baseline_protocol_v3() -> dict:
    """Fit/select baselines on validation only; never exposes test labels."""
    set_v3_seed(42)
    builder, train, validation, x_train, y_train, x_validation, y_validation = _safe_train_validation()
    train_prevalence = float(y_train.mean())
    gnn = _frozen_gnn_validation(train, validation, train_prevalence)
    constant_probability = np.full(len(y_validation), train_prevalence)
    prevalence = {"configuration": {"kind": "constant_train_prevalence", "probability": train_prevalence}, "validation": delay_metric_report(y_validation, constant_probability, train_prevalence=train_prevalence)}
    logistic, scaler, logistic_model = _select_logistic(x_train, y_train, x_validation, y_validation, train_prevalence)
    forest, forest_model = _select_random_forest(x_train, y_train, x_validation, y_validation, train_prevalence)
    if builder.label_access.test_accessed:
        raise RuntimeError("Test labels opened during validation baseline freeze")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    model_dir = OUTPUT_DIR / "baselines"
    model_dir.mkdir(exist_ok=True)
    logistic_path = model_dir / "logistic_regression_selected.joblib"
    forest_path = model_dir / "random_forest_selected.joblib"
    joblib.dump({"scaler": scaler, "model": logistic_model, "feature_count": 7}, logistic_path)
    joblib.dump({"model": forest_model, "feature_count": 7}, forest_path)
    result = {
        "protocol_version": 1,
        "purpose": "validation-only baseline selection before coordinated one-time test evaluation",
        "feature_contract": "v3 seven pre-execution operation features only",
        "split": {"seed": 42, "train_order_count": len(train.order_ids), "validation_order_count": len(validation.order_ids), "test_labels_opened": False},
        "selection_rule": "minimum validation BCE; threshold fixed at 0.5 and never tuned on test",
        "gnn": gnn, "prevalence_baseline": prevalence,
        "logistic_regression": logistic, "random_forest": forest,
        "artifacts": {"logistic_joblib": str(logistic_path), "logistic_sha256": _sha256(logistic_path), "random_forest_joblib": str(forest_path), "random_forest_sha256": _sha256(forest_path)},
        "label_access": {"requests": [name for name, _ in builder.label_access.requests], "test_accessed": builder.label_access.test_accessed},
    }
    path = OUTPUT_DIR / "frozen_validation_baseline_protocol.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    result["protocol_path"] = str(path)
    result["protocol_sha256"] = _sha256(path)
    return result

