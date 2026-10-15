"""Optional Laya provider boundary; never imported by deterministic CRAFT code."""
from __future__ import annotations

import copy
import importlib.util
import time
from typing import Any, Callable, Mapping

from .shadow_contracts import LAYA_DECISION_OPTIONS


class LayaShadowUnavailable(RuntimeError):
    pass


DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string", "enum": list(LAYA_DECISION_OPTIONS),
            "description": (
                "Choose SOLVE_READY_WORK when legal ready work exists; "
                "HORIZON_EXTENSION_REQUIRED when important work is blocked by a "
                "hard future readiness bound; NEIGHBORHOOD_EXPANSION_REQUIRED "
                "when movable causal closure exceeds the bounded neighborhood; "
                "FAST_FORWARD_TO_EVENT when no ready work exists and a finite "
                "authoritative event is known; NO_EXECUTABLE_WORK when the local "
                "remaining pool is empty; UNRESOLVED_ROLLING_STATE when truth or "
                "safe progress is insufficient."
            ),
        },
        "probabilities": {"type": "object"},
        "confidence": {"type": ["number", "null"]},
        "answer_confidence": {"type": ["number", "null"]},
    },
    "required": ["decision"],
}


class LayaShadowProvider:
    """Lazy, injectable Laya adapter.

    A callable can be supplied for tests or a future pinned Laya integration.
    No package, checkpoint, or model is loaded on import.  The callable only
    receives the normalized state dictionary, never the CRAFT teacher label.
    """

    def __init__(self, predictor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 *, model_id: str | None = None, package_version: str | None = None,
                 device: str | None = None):
        self._predictor = predictor
        self.model_id = model_id
        self.package_version = package_version
        self.device = device

    @property
    def available(self) -> bool:
        return self._predictor is not None

    def predict(self, canonical_state: Mapping[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if self._predictor is None:
            # Deliberately lazy capability detection: this does not import or
            # install Laya, and never destabilizes the CRAFT environment.
            package_present = bool(importlib.util.find_spec("laya"))
            return {
                "laya_status": "LAYA_UNAVAILABLE" if not package_present else "LAYA_RUNTIME_NOT_ESTABLISHED",
                "laya_predicted_decision": None, "probabilities": None,
                "confidence": None, "answer_confidence": None,
                "model_id": self.model_id, "package_version": self.package_version,
                "device": self.device, "latency_ms": (time.perf_counter() - started) * 1000.0,
            }
        try:
            # A defensive copy prevents an advisory implementation from
            # mutating the normalized state held by the CRAFT trace.
            raw = self._predictor(copy.deepcopy(dict(canonical_state)))
            if not isinstance(raw, Mapping):
                raise TypeError("Laya predictor must return a mapping")
            result = dict(raw)
            result.setdefault("laya_status", "LAYA_INFERENCE_COMPLETE")
            result.setdefault("model_id", self.model_id)
            result.setdefault("package_version", self.package_version)
            result.setdefault("device", self.device)
            result.setdefault("latency_ms", (time.perf_counter() - started) * 1000.0)
            return result
        except Exception as exc:
            return {
                "laya_status": "LAYA_INFERENCE_ERROR", "laya_predicted_decision": None,
                "probabilities": None, "confidence": None, "answer_confidence": None,
                "model_id": self.model_id, "package_version": self.package_version,
                "device": self.device, "latency_ms": (time.perf_counter() - started) * 1000.0,
                "error_type": type(exc).__name__, "error_message": str(exc),
            }

