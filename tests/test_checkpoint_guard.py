"""Checkpoint failures must stop rescheduling before inference or export."""
from unittest.mock import Mock

import pytest
import torch

import agent.decision_engine as module
from agent.decision_engine import AIDecisionEngine
from graph.feature_schema import (
    PreprocessingArtifactError, operation_schema, save_preprocessing,
)


def _write_sidecar(path, breakdown_scale=1.5, delay_pos_weight=2.0):
    """The preprocessing artifact every inference path now requires."""
    save_preprocessing(path, operation_schema(), breakdown_scale, delay_pos_weight)


def engine_for(path):
    engine = AIDecisionEngine()
    engine.model_path = str(path)
    return engine


def test_missing_checkpoint_aborts_before_database_graph_or_ga(tmp_path, monkeypatch):
    builder = Mock()
    scheduler = Mock()
    monkeypatch.setattr(module, "FactoryGraphBuilder", builder)
    monkeypatch.setattr(module, "GAScheduler", scheduler)
    engine = engine_for(tmp_path / "missing.pth")
    with pytest.raises(FileNotFoundError, match="untrained weights are not permitted"):
        engine.trigger_rescheduling()
    builder.assert_not_called()
    scheduler.assert_not_called()


@pytest.mark.parametrize("kind", ["corrupt", "incompatible"])
def test_unloadable_checkpoint_raises_clear_error_without_inference(tmp_path, monkeypatch, kind):
    path = tmp_path / "bad.pth"
    if kind == "corrupt":
        path.write_bytes(b"not a torch checkpoint")
    else:
        torch.save(torch.nn.Linear(3, 4).state_dict(), path)
    model = torch.nn.Linear(2, 1)
    forward = Mock(side_effect=AssertionError("inference must not run"))
    model.forward = forward
    monkeypatch.setattr(module, "MultiTaskFactoryGNN", lambda **kwargs: model)
    with pytest.raises(RuntimeError, match="Failed to load GNN checkpoint") as caught:
        engine_for(path)._load_trained_model(([], []))
    assert str(path) in str(caught.value)
    assert caught.value.__cause__ is not None
    forward.assert_not_called()


def test_valid_checkpoint_is_loaded_and_eval_mode_is_set(tmp_path, monkeypatch):
    path = tmp_path / "valid.pth"
    source = torch.nn.Linear(2, 1)
    torch.save(source.state_dict(), path)
    _write_sidecar(path)
    target = torch.nn.Linear(2, 1)
    assert target.training
    monkeypatch.setattr(module, "MultiTaskFactoryGNN", lambda **kwargs: target)
    loaded = engine_for(path)._load_trained_model(([], []))
    assert loaded is target
    assert not loaded.training
    for name, tensor in source.state_dict().items():
        assert torch.equal(loaded.state_dict()[name], tensor)


def test_checkpoint_without_its_preprocessing_artifact_is_refused(tmp_path, monkeypatch):
    """A checkpoint whose preprocessing artifact is absent must not load.

    Without it the model would be handed inputs of unknown shape and
    unknown normalisation -- the exact silent-divergence this artifact
    exists to prevent -- so it fails with a typed, explanatory error rather
    than a bare FileNotFoundError from pathlib.
    """
    path = tmp_path / "orphan.pth"
    torch.save(torch.nn.Linear(2, 1).state_dict(), path)  # no sidecar written
    monkeypatch.setattr(module, "MultiTaskFactoryGNN", lambda **kwargs: torch.nn.Linear(2, 1))
    with pytest.raises((PreprocessingArtifactError, RuntimeError), match="preprocessing"):
        engine_for(path)._load_trained_model(([], []))


def test_load_failure_propagates_from_rescheduling_before_ga(tmp_path, monkeypatch):
    path = tmp_path / "bad.pth"
    path.write_bytes(b"invalid")
    _write_sidecar(path)
    graph = Mock()
    builder = Mock()
    builder.build_graph.return_value = (graph, {})
    monkeypatch.setattr(module, "FactoryGraphBuilder", lambda: builder)
    monkeypatch.setattr(module, "apply_planning_horizon", lambda *args: None)
    monkeypatch.setattr(module, "MultiTaskFactoryGNN", lambda **kwargs: torch.nn.Linear(2, 1))
    scheduler = Mock()
    monkeypatch.setattr(module, "GAScheduler", scheduler)
    engine = engine_for(path)
    monkeypatch.setattr(engine, "_fetch_production_lines", lambda: None)
    with pytest.raises(RuntimeError, match="rescheduling aborted"):
        engine.trigger_rescheduling()
    scheduler.assert_not_called()
