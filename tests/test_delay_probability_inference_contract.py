"""Scheduling inference must undo weighted-BCE prior tilt in every live path."""
from pathlib import Path

import pytest
import torch

from models.gnn import prior_corrected_delay_probabilities


def test_prior_correction_matches_analytic_probability():
    logits = torch.tensor([[0.0], [1.25], [-0.8]])
    weight = 2.3854505971769817
    corrected = prior_corrected_delay_probabilities(logits, weight)
    expected = torch.sigmoid(logits - torch.log(torch.tensor(weight)))
    assert torch.allclose(corrected, expected)
    assert corrected[0].item() == pytest.approx(1.0 / (1.0 + weight))


@pytest.mark.parametrize("path", [
    "agent/decision_engine.py",
    "run_final_pipeline.py",
    "run_authoritative_comparison.py",
])
def test_live_scheduling_path_uses_prior_corrected_delay_probabilities(path):
    source = Path(path).read_text(encoding="utf-8")
    assert "prior_corrected_delay_probabilities(" in source
    assert "torch.sigmoid(out['delay'])" not in source
    assert "torch.sigmoid(predictions['delay'])" not in source
