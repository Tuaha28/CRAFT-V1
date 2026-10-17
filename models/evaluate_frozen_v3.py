"""One-time coordinated untouched-test evaluation for frozen GNN v3 arms."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import time

import joblib
import numpy as np
import torch

from graph.graph_builder_v3 import FactoryGraphBuilderV3, order_id_hash
from models.baseline_protocol_v3 import (
    CHECKPOINT,
    EXPECTED_CHECKPOINT_SHA256,
    OUTPUT_DIR,
    delay_metric_report,
)
from models.gnn import MultiTaskFactoryGNN
from models.train_v3 import V3TrainingConfig


EXPECTED_LOGISTIC_SHA256 = "1fbbdafefcd4f510d7fd08046de1ab1c9a901064430c59f924626230e771fcf9"
EXPECTED_RANDOM_FOREST_SHA256 = "f83994556aeeec725045b598652a6597c298097f70536f739132e3892953176d"
LOGISTIC_PATH = OUTPUT_DIR / "baselines/logistic_regression_selected.joblib"
RANDOM_FOREST_PATH = OUTPUT_DIR / "baselines/random_forest_selected.joblib"
RESULT_PATH = OUTPUT_DIR / "final_untouched_test_evaluation.json"
RESULT_SHA_PATH = OUTPUT_DIR / "final_untouched_test_evaluation.sha256"


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _verified_artifacts() -> dict:
    actual = {
        "gnn_checkpoint": _sha256(CHECKPOINT),
        "logistic_regression": _sha256(LOGISTIC_PATH),
        "random_forest": _sha256(RANDOM_FOREST_PATH),
    }
    expected = {
        "gnn_checkpoint": EXPECTED_CHECKPOINT_SHA256,
        "logistic_regression": EXPECTED_LOGISTIC_SHA256,
        "random_forest": EXPECTED_RANDOM_FOREST_SHA256,
    }
    if actual != expected:
        raise RuntimeError(f"Frozen artifact hash mismatch: {actual}")
    return actual


def _test_report(y_true, probability, train_prevalence: float) -> dict:
    report = delay_metric_report(y_true, probability, train_prevalence=train_prevalence)
    report["test_prevalence"] = report.pop("validation_prevalence")
    return report


def evaluate_once_frozen_test_v3() -> dict:
    """Open test labels exactly once after all frozen hashes are verified."""
    if RESULT_PATH.exists() or RESULT_SHA_PATH.exists():
        raise RuntimeError("One-time untouched-test artifact already exists")
    hashes = _verified_artifacts()  # Required before any label access.
    started = time.perf_counter()

    builder = FactoryGraphBuilderV3(seed=42)
    train, validation = builder.build_train_validation()
    if builder.label_access.test_accessed:
        raise RuntimeError("Test labels opened before frozen-evaluation gate")
    checkpoint = torch.load(CHECKPOINT, map_location="cpu", weights_only=False)
    config = V3TrainingConfig(**checkpoint["training_config"])
    test = builder.build_test_after_checkpoint(
        priority_cutpoints=train.label_meta["priority_cutpoints"],
        checkpoint_sha256=hashes["gnn_checkpoint"],
    )
    if [name for name, _ in builder.label_access.requests] != ["train", "validation", "test"]:
        raise RuntimeError("Unexpected test label-access sequence")

    test_mask = test.graph["operation"].delay_valid_mask
    x_test = test.graph["operation"].x.cpu().numpy()[test_mask.cpu().numpy().ravel()]
    y_test = test.labels["delay"][test_mask].cpu().numpy().ravel().astype(int)
    train_mask = train.graph["operation"].delay_valid_mask
    train_prevalence = float(train.labels["delay"][train_mask].float().mean().cpu())

    model = MultiTaskFactoryGNN.from_graph(train.graph, hidden_channels=config.hidden_channels, dropout=config.dropout)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    with torch.no_grad():
        output = model(test.graph.x_dict, test.graph.edge_index_dict, test.graph["operation"].type_idx, enabled_heads=("delay",))
        gnn_probability = torch.sigmoid(output["delay"][test_mask]).cpu().numpy().ravel()

    logistic = joblib.load(LOGISTIC_PATH)
    forest = joblib.load(RANDOM_FOREST_PATH)
    logistic_probability = logistic["model"].predict_proba(logistic["scaler"].transform(x_test))[:, 1]
    forest_probability = forest["model"].predict_proba(x_test)[:, 1]
    prevalence_probability = np.full(len(y_test), train_prevalence)

    result = {
        "protocol_version": 1,
        "purpose": "one-time coordinated untouched-test evaluation; no fitting or tuning",
        "frozen_artifact_hashes": hashes,
        "checkpoint_best_epoch": int(checkpoint["best_epoch"]),
        "split": {
            "seed": 42,
            "test_order_count": len(test.order_ids),
            "test_order_ids_sha256": order_id_hash(test.order_ids),
            "test_graph_fingerprint": test.graph_fingerprint,
            "test_valid_operation_count": int(len(y_test)),
        },
        "label_access_event": {
            "requests": [name for name, _ in builder.label_access.requests],
            "test_accessed": builder.label_access.test_accessed,
            "sealed_checkpoint_sha256": builder.label_access._checkpoint_sha256,
            "test_order_count": len(builder.order_splits["test"]),
            "test_order_ids_sha256": order_id_hash(builder.order_splits["test"]),
        },
        "threshold": 0.5,
        "train_prevalence": train_prevalence,
        "gnn": _test_report(y_test, gnn_probability, train_prevalence),
        "logistic_regression": _test_report(y_test, logistic_probability, train_prevalence),
        "random_forest": _test_report(y_test, forest_probability, train_prevalence),
        "constant_train_prevalence": _test_report(y_test, prevalence_probability, train_prevalence),
        "runtime_seconds": time.perf_counter() - started,
        "test_label_access_count": sum(name == "test" for name, _ in builder.label_access.requests),
    }
    if result["test_label_access_count"] != 1:
        raise RuntimeError("Test label access was not exactly once")
    RESULT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    artifact_sha256 = _sha256(RESULT_PATH)
    RESULT_SHA_PATH.write_text(artifact_sha256 + "\n", encoding="ascii")
    result["evaluation_artifact_path"] = str(RESULT_PATH)
    result["evaluation_artifact_sha256"] = artifact_sha256
    return result
