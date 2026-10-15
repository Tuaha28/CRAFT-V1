"""Synthetic, label-free dimensional-contract tests for leak-free GNN v3."""

import pytest
import torch
import torch.nn.functional as F
from torch_geometric.data import HeteroData

from models.gnn import (
    COMPLETION_SCALE_MINUTES,
    MultiTaskFactoryGNN,
    node_input_dimensions_from_graph,
)


def _graph(machine_width=1, worker_width=4, operation_width=7):
    data = HeteroData()
    data["machine"].x = torch.randn(2, machine_width)
    data["worker"].x = torch.randn(2, worker_width)
    data["operation"].x = torch.randn(3, operation_width)
    data["operation"].type_idx = torch.tensor([0, 1, 2], dtype=torch.long)
    op_edges = torch.tensor([[0, 1, 2], [1, 2, 0]], dtype=torch.long)
    machine_edges = torch.tensor([[0, 1, 0], [0, 1, 2]], dtype=torch.long)
    worker_edges = torch.tensor([[0, 1, 0], [0, 1, 2]], dtype=torch.long)
    data["operation", "precedes", "operation"].edge_index = op_edges
    data["operation", "contends_with", "operation"].edge_index = op_edges
    data["machine", "operates", "operation"].edge_index = machine_edges
    data["worker", "staffs", "operation"].edge_index = worker_edges
    data["operation", "runs_on", "machine"].edge_index = machine_edges.flip(0)
    data["operation", "staffed_by", "worker"].edge_index = worker_edges.flip(0)
    return data


def _loss(outputs):
    delay_target = torch.tensor([[0.0], [1.0], [1.0]])
    completion_target = torch.tensor([[10.0], [20.0], [30.0]])
    priority_target = torch.tensor([[0.2], [0.6], [0.8]])
    return (
        F.binary_cross_entropy_with_logits(outputs["delay"], delay_target)
        + F.huber_loss(
            outputs["completion"] / COMPLETION_SCALE_MINUTES,
            completion_target / COMPLETION_SCALE_MINUTES,
            delta=0.4,
        )
        + F.mse_loss(outputs["priority"], priority_target)
    )


def test_v3_graph_constructor_infers_exact_safe_widths():
    data = _graph()
    assert node_input_dimensions_from_graph(data) == {
        "machine": 1, "worker": 4, "operation": 7,
    }
    model = MultiTaskFactoryGNN.from_graph(
        data, hidden_channels=8, dropout=0.0
    )
    assert model.input_dimensions == {
        "machine": 1, "worker": 4, "operation": 7,
    }
    assert model.lin_dict_delay["machine"].in_features == 1
    assert model.lin_dict_delay["worker"].in_features == 4
    assert model.lin_dict_delay["operation"].in_features == 11


def test_v3_train_and_validation_forwards_have_expected_shapes():
    data = _graph()
    model = MultiTaskFactoryGNN.from_graph(
        data, hidden_channels=8, dropout=0.0
    )
    model.train()
    train_output = model(
        data.x_dict, data.edge_index_dict, data["operation"].type_idx
    )
    model.eval()
    with torch.no_grad():
        validation_output = model(
            data.x_dict, data.edge_index_dict, data["operation"].type_idx
        )
    for output in (train_output, validation_output):
        assert set(output) == {"delay", "completion", "priority"}
        assert all(value.shape == (3, 1) for value in output.values())
        assert all(torch.isfinite(value).all() for value in output.values())
    assert (validation_output["completion"] >= 0).all()
    assert ((validation_output["priority"] >= 0) & (
        validation_output["priority"] <= 1
    )).all()


def test_v3_loss_and_one_backward_pass_smoke():
    data = _graph()
    model = MultiTaskFactoryGNN.from_graph(
        data, hidden_channels=8, dropout=0.0
    )
    output = model(data.x_dict, data.edge_index_dict, data["operation"].type_idx)
    loss = _loss(output)
    assert torch.isfinite(loss)
    loss.backward()
    assert model.lin_dict_delay["machine"].weight.grad is not None
    assert torch.isfinite(model.lin_dict_delay["machine"].weight.grad).all()
    assert model.delay_head[0].weight.grad is not None


def test_legacy_resource_defaults_remain_compatible():
    data = _graph(machine_width=5, worker_width=4, operation_width=7)
    model = MultiTaskFactoryGNN(
        data.metadata(), hidden_channels=8, dropout=0.0, operation_in_dim=7
    )
    assert model.input_dimensions == {
        "machine": 5, "worker": 4, "operation": 7,
    }
    with pytest.raises(ValueError, match="conflicts"):
        MultiTaskFactoryGNN.from_graph(data, machine_in_dim=1)
