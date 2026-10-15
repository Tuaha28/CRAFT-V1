"""Safety checks for the isolated, hash-pinned Factory-2 benchmark input."""

import hashlib
import pickle

import pytest

from scratch import run_one_factory_craft


def test_capture_is_loaded_without_falling_back_to_database(monkeypatch, tmp_path):
    payload = pickle.dumps(tuple(range(9)))
    path = tmp_path / "input.pkl"
    path.write_bytes(payload)
    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_PATH", str(path))
    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(run_one_factory_craft, "_load_state", lambda: pytest.fail("database fallback"))

    assert run_one_factory_craft._load_run_state() == tuple(range(9))


def test_capture_hash_mismatch_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "input.pkl"
    path.write_bytes(pickle.dumps(tuple(range(9))))
    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_PATH", str(path))
    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_SHA256", "0" * 64)
    monkeypatch.setattr(run_one_factory_craft, "_load_state", lambda: pytest.fail("database fallback"))

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        run_one_factory_craft._load_run_state()


def test_capture_requires_hash_and_expected_shape(monkeypatch, tmp_path):
    payload = pickle.dumps((1, 2))
    path = tmp_path / "input.pkl"
    path.write_bytes(payload)
    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_PATH", str(path))
    monkeypatch.delenv("CRAFT_ONE_FACTORY_CAPTURE_SHA256", raising=False)
    with pytest.raises(RuntimeError, match="SHA-256 is required"):
        run_one_factory_craft._load_run_state()

    monkeypatch.setenv("CRAFT_ONE_FACTORY_CAPTURE_SHA256", hashlib.sha256(payload).hexdigest())
    with pytest.raises(RuntimeError, match="capture shape"):
        run_one_factory_craft._load_run_state()
