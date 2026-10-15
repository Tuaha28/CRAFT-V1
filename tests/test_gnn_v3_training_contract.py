"""Entry-point contract tests for the delay-only, test-sealed v3 trainer."""

from types import SimpleNamespace

import torch
from torch_geometric.data import HeteroData

import models.train_v3 as train_v3


def _graph():
    graph = HeteroData()
    graph["machine"].x = torch.randn(2, 1)
    graph["worker"].x = torch.randn(2, 4)
    graph["operation"].x = torch.randn(3, 7)
    graph["operation"].type_idx = torch.tensor([0, 1, 2])
    graph["operation"].delay_valid_mask = torch.tensor([True, True, True])
    op_edges = torch.tensor([[0, 1, 2], [1, 2, 0]])
    machine_edges = torch.tensor([[0, 1, 0], [0, 1, 2]])
    worker_edges = torch.tensor([[0, 1, 0], [0, 1, 2]])
    graph["operation", "precedes", "operation"].edge_index = op_edges
    graph["operation", "contends_with", "operation"].edge_index = op_edges
    graph["machine", "operates", "operation"].edge_index = machine_edges
    graph["worker", "staffs", "operation"].edge_index = worker_edges
    graph["operation", "runs_on", "machine"].edge_index = machine_edges.flip(0)
    graph["operation", "staffed_by", "worker"].edge_index = worker_edges.flip(0)
    return graph


class _Access:
    test_accessed = False
    _checkpoint_sha256 = None
    requests = [("train", ("1",)), ("validation", ("2",))]


class _Builder:
    def __init__(self, seed):
        assert seed == 42
        self.label_access = _Access()

    def build_train_validation(self):
        train = SimpleNamespace(
            graph=_graph(),
            labels={"delay": torch.tensor([[0.0], [1.0], [1.0]])},
        )
        validation = SimpleNamespace(
            graph=_graph(),
            labels={"delay": torch.tensor([[1.0], [0.0], [1.0]])},
        )
        return train, validation


def test_v3_entrypoint_is_delay_only_and_never_opens_test(monkeypatch):
    monkeypatch.setattr(train_v3, "FactoryGraphBuilderV3", _Builder)
    config = train_v3.V3TrainingConfig(
        max_epochs=1, hidden_channels=8, dropout=0.0, device="cpu"
    )
    result = train_v3.smoke_train_validation_step_v3(config)
    assert result["objective"] == "delay_only"
    assert result["enabled_heads"] == ["delay"]
    assert result["input_dimensions"] == {
        "machine": 1, "worker": 4, "operation": 7,
    }
    assert result["label_requests"] == ["train", "validation"]
    assert result["test_labels_opened"] is False
    assert result["train_loss"] >= 0
    assert result["validation_loss"] >= 0


def test_v3_delay_loss_is_unweighted():
    assert train_v3.V3_DELAY_POS_WEIGHT == 1.0


def test_v3_delay_criterion_omits_pos_weight(monkeypatch):
    monkeypatch.setattr(train_v3, 'FactoryGraphBuilderV3', _Builder)
    state = train_v3.prepare_v3_training(train_v3.V3TrainingConfig(hidden_channels=8, dropout=0.0, device='cpu'))
    assert state['criterion'].pos_weight is None
    assert state['delay_pos_weight'] == 1.0
