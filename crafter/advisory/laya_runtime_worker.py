"""Subprocess entry point for Laya.

This module intentionally does not import Laya at module import time.  It is
executed only in the explicitly configured isolated interpreter.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import sys
import time


def _respond(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _decision_schema(requested_schema):
    requested_schema = requested_schema or {}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"decision": dict(
            (requested_schema.get("properties") or {}).get("decision", {
                "type": "string", "enum": [
                    "SOLVE_READY_WORK", "HORIZON_EXTENSION_REQUIRED",
                    "NEIGHBORHOOD_EXPANSION_REQUIRED", "FAST_FORWARD_TO_EVENT",
                    "NO_EXECUTABLE_WORK", "UNRESOLVED_ROLLING_STATE",
                ],
            }))},
        "required": ["decision"],
    }


def _load_agent(diagnostics):
    started = time.perf_counter()
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            import laya  # type: ignore
    except Exception as exc:
        return None, {"status": "LAYA_STARTUP_ERROR", "error_type": type(exc).__name__,
                      "error_message": str(exc),
                      "model_load_latency_ms": (time.perf_counter() - started) * 1000.0}
    model_id = os.environ.get("CRAFT_LAYA_MODEL", "convaiinnovations/laya")
    subfolder = os.environ.get("CRAFT_LAYA_SUBFOLDER", "typed-decisions")
    device = os.environ.get("CRAFT_LAYA_DEVICE") or None
    try:
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            # Laya 0.3.x exposes the documented Agent API.  The typed-decisions
            # subfolder is selected only because DEFAULT_MODELS in the installed
            # release explicitly advertises it.
            agent = laya.load(model_id, device=device, subfolder=subfolder)
        if diagnostics.getvalue():
            sys.stderr.write(diagnostics.getvalue())
        return (agent, {"status": "READY", "model_id": model_id,
                        "model_subfolder": subfolder, "device": device or "cpu",
                        "package_version": getattr(laya, "__version__", None),
                        "python_version": sys.version, "torch_version": _version("torch"),
                        "transformers_version": _version("transformers"),
                        "worker_pid": os.getpid(), "model_load_count": 1,
                        "model_load_latency_ms": (time.perf_counter() - started) * 1000.0})
    except Exception as exc:
        if diagnostics.getvalue():
            sys.stderr.write(diagnostics.getvalue())
        return None, {"status": "LAYA_STARTUP_ERROR", "error_type": type(exc).__name__,
                      "error_message": str(exc),
                      "model_load_latency_ms": (time.perf_counter() - started) * 1000.0}


def _version(module_name):
    try:
        module = __import__(module_name)
        return getattr(module, "__version__", None)
    except Exception:
        return None


def _infer(agent, payload, metadata):
    started = time.perf_counter()
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            details = agent.decide(payload.get("state"),
                                   schema=_decision_schema(payload.get("decision_schema")),
                                   return_details=True)
        values = getattr(details, "values", {}) or {}
        confidences = getattr(details, "confidence", {}) or {}
        probabilities = getattr(details, "probabilities", {}) or {}
        answers = getattr(details, "answers", {}) or {}
        decision = values.get("decision")
        answer_confidence = None
        if isinstance(answers.get("decision"), dict):
            answer_confidence = answers["decision"].get("answer_confidence")
        result = {"status": "LAYA_INFERENCE_COMPLETE",
                  "laya_status": "LAYA_INFERENCE_COMPLETE",
                  "laya_predicted_decision": decision,
                  "probabilities": probabilities.get("decision"),
                  "confidence": confidences.get("decision"),
                  "answer_confidence": answer_confidence,
                  "answers": answers, "usage": getattr(details, "usage", None),
                  "routing": getattr(details, "routing", None),
                  "model_id": metadata.get("model_id"),
                  "model_subfolder": metadata.get("model_subfolder"),
                  "subfolder": metadata.get("model_subfolder"),
                  "package_version": metadata.get("package_version"),
                  "device": metadata.get("device"),
                  "worker_pid": metadata.get("worker_pid"),
                  "latency_ms": (time.perf_counter() - started) * 1000.0,
                  "model_load_count": metadata.get("model_load_count", 1)}
        if diagnostics.getvalue():
            sys.stderr.write(diagnostics.getvalue())
        return result
    except Exception as exc:
        if diagnostics.getvalue():
            sys.stderr.write(diagnostics.getvalue())
        return {"status": "LAYA_INFERENCE_ERROR", "laya_status": "LAYA_INFERENCE_ERROR",
                "laya_predicted_decision": None, "error_type": type(exc).__name__,
                "error_message": str(exc),
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "model_load_count": metadata.get("model_load_count", 1)}


def main() -> int:
    agent, startup = _load_agent(None)
    if agent is None:
        _respond(startup)
        return 1
    startup.update({"protocol_version": "CRAFT_LAYA_JSONL_V1"})
    _respond(startup)
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except Exception as exc:
            _respond({"status": "LAYA_INVALID_REQUEST", "error_type": type(exc).__name__,
                      "error_message": str(exc)})
            continue
        request_id = payload.get("request_id")
        action = payload.get("action")
        if action == "ping" or action == "health":
            _respond({"request_id": request_id, "status": "PONG", "worker_pid": os.getpid(),
                      "model_load_count": 1})
        elif action == "infer":
            result = _infer(agent, payload, startup)
            result["request_id"] = request_id
            _respond(result)
        elif action == "shutdown":
            _respond({"request_id": request_id, "status": "SHUTDOWN_ACK"})
            return 0
        else:
            _respond({"request_id": request_id, "status": "LAYA_INVALID_REQUEST",
                      "error_type": "UnknownAction", "error_message": repr(action)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
