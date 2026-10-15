"""
Regression tests for PROJECT_STATUS.txt Section 0H.14: the Softplus fix for
Head 2's dead-ReLU collapse (0H.13), and the corrected loss weighting that
goes with it.

Section 0H.13 measured, directly, that a plain ReLU as the completion
head's final activation collapsed under the corrected (parity) loss
weight: every operation's pre-activation went negative within ~25 epochs,
a ReLU clipped there has EXACTLY zero gradient, and the head never
recovered for the remaining 225 of 250 epochs (val loss identical to 4
decimal places the whole time). These tests exercise the exact failure
condition directly -- a strongly negative pre-activation -- and confirm
Softplus's gradient only ever approaches zero, never reaches it.
"""
import torch
import pytest
from torch_geometric.data import HeteroData

from models.gnn import MultiTaskFactoryGNN, COMPLETION_SCALE_MINUTES
import models.train as train_mod


def _metadata():
    return (
        ['machine', 'worker', 'operation'],
        [
            ('operation', 'precedes', 'operation'),
            ('machine', 'operates', 'operation'),
            ('worker', 'staffs', 'operation'),
            ('operation', 'runs_on', 'machine'),
            ('operation', 'staffed_by', 'worker'),
        ],
    )


def _tiny_graph():
    """Minimal synthetic HeteroData -- no DB dependency -- matching the
    real schema's node/edge types and feature widths exactly."""
    data = HeteroData()
    data['machine'].x = torch.randn(2, 5)
    data['worker'].x = torch.randn(2, 4)
    data['operation'].x = torch.randn(3, 12)  # 9 continuous + mean_breakdown_count_norm, see chat
    # operation_type index, one per operation node -- not part of .x, see
    # graph/graph_builder.py. Any valid index in range works for this
    # architecture-only test; doesn't need to match a real taxonomy.
    data['operation'].type_idx = torch.tensor([0, 1, 2])
    prec = torch.tensor([[0, 1], [1, 2]])
    mach = torch.tensor([[0, 0, 1], [0, 1, 2]])
    work = torch.tensor([[0, 1], [0, 1]])
    data['operation', 'precedes', 'operation'].edge_index = prec
    data['machine', 'operates', 'operation'].edge_index = mach
    data['worker', 'staffs', 'operation'].edge_index = work
    data['operation', 'runs_on', 'machine'].edge_index = mach.flip(0)
    data['operation', 'staffed_by', 'worker'].edge_index = work.flip(0)
    return data


# ---------------------------------------------------------------------------
# The core property: Softplus vs. the dead-ReLU failure mode
# ---------------------------------------------------------------------------

def test_completion_head_output_is_always_non_negative():
    torch.manual_seed(0)
    model = MultiTaskFactoryGNN(metadata=_metadata(), hidden_channels=8, operation_in_dim=12)
    # Wide range including strongly negative values -- the exact condition
    # that drove the old ReLU's pre-activation negative in 0H.13.
    x = torch.randn(200, 8) * 10.0
    out = model.completion_head(x)
    assert (out >= 0).all()


def test_completion_head_gradient_never_collapses_to_exactly_zero():
    # The property this fix exists for. A dead ReLU here has EXACTLY zero
    # gradient and cannot recover (measured, 0H.13). Softplus's gradient
    # (sigmoid of the pre-activation) is vanishingly small for very
    # negative inputs but never bit-for-bit zero.
    torch.manual_seed(0)
    model = MultiTaskFactoryGNN(metadata=_metadata(), hidden_channels=8, operation_in_dim=12)
    x = (torch.randn(500, 8) * 10.0 - 20.0).requires_grad_(True)  # deliberately very negative
    out = model.completion_head(x)
    out.sum().backward()
    assert (x.grad.abs() > 0).all()


def test_forward_pass_completion_output_is_non_negative_and_raw_minutes_shaped():
    # End-to-end (real forward pass, no DB): confirms the full model still
    # runs with Softplus, and that COMPLETION_SCALE_MINUTES's multiply
    # keeps the PUBLIC output non-negative -- the same contract the old
    # ReLU-ended head provided, unchanged for every downstream consumer.
    torch.manual_seed(0)
    data = _tiny_graph()
    model = MultiTaskFactoryGNN(metadata=data.metadata(), hidden_channels=8, operation_in_dim=12)
    model.eval()
    with torch.no_grad():
        out = model(data.x_dict, data.edge_index_dict, data['operation'].type_idx)
    completion = out['completion']
    assert completion.shape == (3, 1)
    assert (completion >= 0).all()


# ---------------------------------------------------------------------------
# The shared scale constant stays in sync between gnn.py and train.py
# ---------------------------------------------------------------------------

def test_train_py_imports_the_same_scale_constant_gnn_py_defines():
    # models/train.py's loss computation and models/gnn.py's forward()
    # must divide/multiply by the identical constant, or the training-time
    # loss and the model's public raw-minutes output silently disagree.
    assert train_mod.COMPLETION_SCALE_MINUTES == COMPLETION_SCALE_MINUTES


def test_completion_scale_is_the_documented_1000_minutes():
    assert COMPLETION_SCALE_MINUTES == pytest.approx(1000.0)
