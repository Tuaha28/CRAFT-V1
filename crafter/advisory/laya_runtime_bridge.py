"""Isolated subprocess bridge for optional Laya shadow inference.

The deterministic scheduler never imports this module unless a caller elects
to collect shadow telemetry.  A worker communicates strictly through one JSON
request and one JSON response; failures are data and cannot affect CRAFT.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence

from .laya_shadow import DECISION_SCHEMA
from .shadow_contracts import LAYA_DECISION_OPTIONS, canonical_json


class LayaRuntimeBridge:
    def __init__(self, *, python_executable: str | None = None,
                 timeout_seconds: float = 120.0,
                 worker_module: str = "crafter.advisory.laya_runtime_worker",
                 worker_command: Sequence[str] | None = None):
        self.python_executable = python_executable or os.environ.get("CRAFT_LAYA_PYTHON")
        self.timeout_seconds = float(timeout_seconds)
        self.worker_module = worker_module
        self.worker_command = tuple(worker_command) if worker_command is not None else None

    def _command(self) -> list[str] | None:
        if self.worker_command is not None:
            return [str(part) for part in self.worker_command]
        if not self.python_executable:
            return None
        return [str(self.python_executable), "-m", self.worker_module]

    def infer(self, canonical_state: Mapping[str, Any],
              decision_schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        command = self._command()
        if command is None:
            return {"laya_status": "LAYA_RUNTIME_NOT_ESTABLISHED",
                    "laya_predicted_decision": None, "error_type": "NO_CONFIGURED_RUNTIME",
                    "latency_ms": (time.perf_counter() - started) * 1000.0}
        payload = {"request_id": "oneshot-infer", "action": "infer",
                   "state": json.loads(canonical_json(canonical_state)),
                   "decision_schema": dict(decision_schema or DECISION_SCHEMA)}
        try:
            completed = subprocess.run(
                command, input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                text=True, capture_output=True, timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"laya_status": "LAYA_RUNTIME_TIMEOUT", "laya_predicted_decision": None,
                    "error_type": "TimeoutExpired", "error_message": str(exc),
                    "latency_ms": (time.perf_counter() - started) * 1000.0}
        except Exception as exc:
            return {"laya_status": "LAYA_RUNTIME_ERROR", "laya_predicted_decision": None,
                    "error_type": type(exc).__name__, "error_message": str(exc),
                    "latency_ms": (time.perf_counter() - started) * 1000.0}
        latency = (time.perf_counter() - started) * 1000.0
        if completed.returncode != 0:
            return {"laya_status": "LAYA_RUNTIME_ERROR", "laya_predicted_decision": None,
                    "error_type": "WorkerExit", "error_message":
                    (completed.stderr or "").strip()[-2000:], "latency_ms": latency}
        try:
            # The shared worker emits a READY handshake followed by the
            # request response.  Select the final JSONL protocol message so
            # the legacy one-shot adapter remains compatible.
            messages = [json.loads(line) for line in (completed.stdout or "").splitlines()
                        if line.strip()]
            if not messages:
                raise ValueError("worker returned no JSON protocol message")
            result = messages[-1]
        except Exception as exc:
            return {"laya_status": "LAYA_INVALID_RESPONSE", "laya_predicted_decision": None,
                    "error_type": type(exc).__name__, "error_message": str(exc),
                    "raw_output_tail": (completed.stdout or "")[-1000:], "latency_ms": latency}
        if not isinstance(result, Mapping):
            return {"laya_status": "LAYA_INVALID_RESPONSE", "laya_predicted_decision": None,
                    "error_type": "ResponseTypeError", "error_message": "response is not an object",
                    "latency_ms": latency}
        result = dict(result)
        result.setdefault("latency_ms", latency)
        predicted = result.get("laya_predicted_decision", result.get("decision"))
        if predicted is not None and predicted not in LAYA_DECISION_OPTIONS:
            result.update({"laya_status": "LAYA_INVALID_DECISION",
                           "laya_predicted_decision": None,
                           "error_type": "INVALID_LAYA_DECISION",
                           "error_message": f"invalid Laya shadow decision: {predicted!r}"})
        elif predicted is not None:
            result["laya_predicted_decision"] = predicted
            result.setdefault("laya_status", "LAYA_INFERENCE_COMPLETE")
        return result


class LayaRuntimeProvider:
    """Provider-compatible adapter around :class:`LayaRuntimeBridge`."""
    def __init__(self, bridge: LayaRuntimeBridge):
        self.bridge = bridge

    def predict(self, canonical_state: Mapping[str, Any]) -> dict[str, Any]:
        return self.bridge.infer(canonical_state, DECISION_SCHEMA)


class PersistentLayaRuntimeBridge:
    """Persistent JSONL Laya worker used only for shadow telemetry.

    The worker is deliberately isolated in a separately configured Python
    executable.  A failed advisory process is returned as data; this class
    never changes the deterministic CRAFT decision.
    """

    def __init__(self, *, python_executable: str | None = None,
                 startup_timeout_seconds: float = 900.0,
                 inference_timeout_seconds: float = 120.0,
                 shutdown_timeout_seconds: float = 10.0,
                 worker_module: str = "crafter.advisory.laya_runtime_worker",
                 worker_command: Sequence[str] | None = None,
                 auto_restart_once: bool = True):
        self.python_executable = python_executable or os.environ.get("CRAFT_LAYA_PYTHON")
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.inference_timeout_seconds = float(inference_timeout_seconds)
        self.shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self.worker_module = worker_module
        self.worker_command = tuple(worker_command) if worker_command is not None else None
        self.auto_restart_once = bool(auto_restart_once)
        self._process = None
        self._stdout_queue = None
        self._reader_threads = []
        self._request_counter = 0
        self._started = False
        self._startup_info = None
        self.worker_restart_count = 0

    def _command(self) -> list[str] | None:
        if self.worker_command is not None:
            return [str(part) for part in self.worker_command]
        if not self.python_executable:
            return None
        return [str(self.python_executable), "-m", self.worker_module]

    @property
    def worker_pid(self):
        return self._startup_info.get("worker_pid") if self._startup_info else None

    @property
    def startup_info(self):
        return dict(self._startup_info or {})

    @property
    def started(self):
        return bool(self._started and self._process is not None and self._process.poll() is None)

    def _spawn(self):
        command = self._command()
        if command is None:
            return {"status": "LAYA_RUNTIME_NOT_ESTABLISHED",
                    "error_type": "NO_CONFIGURED_RUNTIME"}
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                cwd=os.getcwd(), env=os.environ.copy(),
            )
            self._stdout_queue = queue.Queue()
            self._reader_threads = [threading.Thread(target=self._read_stdout, daemon=True),
                                    threading.Thread(target=self._drain_stderr, daemon=True)]
            for thread in self._reader_threads:
                thread.start()
            return self._read_response(self.startup_timeout_seconds)
        except Exception as exc:
            self._terminate_process()
            return {"status": "LAYA_RUNTIME_ERROR", "error_type": type(exc).__name__,
                    "error_message": str(exc)}

    def _read_stdout(self):
        try:
            for line in self._process.stdout:
                self._stdout_queue.put(line)
        except Exception as exc:
            self._stdout_queue.put(json.dumps({"status": "LAYA_PROTOCOL_ERROR",
                                                "error_type": type(exc).__name__,
                                                "error_message": str(exc)}))
        finally:
            self._stdout_queue.put(None)

    def _drain_stderr(self):
        try:
            for _line in self._process.stderr:
                pass
        except Exception:
            pass

    def _read_response(self, timeout):
        try:
            line = self._stdout_queue.get(timeout=float(timeout))
        except queue.Empty:
            return {"status": "LAYA_RUNTIME_TIMEOUT", "error_type": "TimeoutExpired"}
        if line is None:
            return {"status": "LAYA_WORKER_DIED", "error_type": "WorkerExit",
                    "error_message": "worker stdout closed"}
        try:
            result = json.loads(line)
        except Exception as exc:
            return {"status": "LAYA_INVALID_RESPONSE", "error_type": type(exc).__name__,
                    "error_message": str(exc), "raw_output_tail": str(line)[-1000:]}
        if not isinstance(result, Mapping):
            return {"status": "LAYA_INVALID_RESPONSE", "error_type": "ResponseTypeError",
                    "error_message": "response is not an object"}
        return dict(result)

    def _send(self, payload, timeout):
        if not self.started:
            return {"status": "LAYA_WORKER_DIED", "error_type": "WorkerNotRunning"}
        try:
            self._process.stdin.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except Exception as exc:
            return {"status": "LAYA_WORKER_DIED", "error_type": type(exc).__name__,
                    "error_message": str(exc)}
        return self._read_response(timeout)

    def start(self):
        if self.started:
            return dict(self._startup_info)
        result = self._spawn()
        if result.get("status") != "READY":
            self._terminate_process()
            self._started = False
            raise RuntimeError(json.dumps(result, sort_keys=True))
        self._startup_info = result
        self._started = True
        return dict(result)

    def _terminate_process(self):
        process = self._process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=self.shutdown_timeout_seconds)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except Exception:
                    pass
        self._process = None
        self._started = False

    def _restart(self):
        self._terminate_process()
        self.worker_restart_count += 1
        result = self._spawn()
        if result.get("status") != "READY":
            self._terminate_process()
            return result
        self._startup_info = result
        self._started = True
        return result

    def infer(self, canonical_state: Mapping[str, Any],
              decision_schema: Mapping[str, Any] | None = None,
              *, request_id: str | None = None) -> dict[str, Any]:
        if not self.started:
            try:
                self.start()
            except Exception as exc:
                return {"laya_status": "LAYA_RUNTIME_ERROR", "status": "LAYA_RUNTIME_ERROR",
                        "laya_predicted_decision": None, "error_type": type(exc).__name__,
                        "error_message": str(exc), "worker_restart_count": self.worker_restart_count}
        self._request_counter += 1
        request_id = request_id or f"laya-request-{self._request_counter:06d}"
        payload = {"request_id": request_id, "action": "infer",
                   "state": json.loads(canonical_json(canonical_state)),
                   "decision_schema": dict(decision_schema or DECISION_SCHEMA)}
        started = time.perf_counter()
        response = self._send(payload, self.inference_timeout_seconds)
        if response.get("status") in {"LAYA_WORKER_DIED", "LAYA_RUNTIME_TIMEOUT"}:
            if self.auto_restart_once and self.worker_restart_count == 0:
                restart = self._restart()
                if restart.get("status") == "READY":
                    response = self._send(payload, self.inference_timeout_seconds)
                else:
                    response = restart
        if response.get("request_id") not in {None, request_id}:
            response = {"status": "LAYA_PROTOCOL_ERROR", "laya_status": "LAYA_PROTOCOL_ERROR",
                        "laya_predicted_decision": None, "error_type": "RequestIdMismatch",
                        "error_message": f"expected {request_id!r}, got {response.get('request_id')!r}"}
        response.setdefault("latency_ms", (time.perf_counter() - started) * 1000.0)
        response.setdefault("worker_restart_count", self.worker_restart_count)
        response.setdefault("laya_status", response.get("status", "LAYA_INFERENCE_COMPLETE"))
        predicted = response.get("laya_predicted_decision", response.get("decision"))
        if predicted is not None and predicted not in LAYA_DECISION_OPTIONS:
            response.update({"laya_status": "LAYA_INVALID_DECISION", "status": "LAYA_INVALID_DECISION",
                             "laya_predicted_decision": None, "error_type": "INVALID_LAYA_DECISION",
                             "error_message": f"invalid Laya shadow decision: {predicted!r}"})
        elif predicted is not None:
            response["laya_predicted_decision"] = predicted
            response.setdefault("laya_status", response.get("status", "LAYA_INFERENCE_COMPLETE"))
        return response

    def ping(self):
        if not self.started:
            self.start()
        self._request_counter += 1
        return self._send({"request_id": f"laya-ping-{self._request_counter:06d}", "action": "ping"},
                          self.inference_timeout_seconds)

    def close(self):
        if self._process is None:
            return {"status": "SHUTDOWN_ACK", "already_closed": True}
        response = {"status": "SHUTDOWN_TIMEOUT"}
        if self.started:
            self._request_counter += 1
            response = self._send({"request_id": f"laya-shutdown-{self._request_counter:06d}",
                                   "action": "shutdown"}, self.shutdown_timeout_seconds)
        self._terminate_process()
        response["worker_exited"] = self._process is None
        return response

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class PersistentLayaRuntimeProvider:
    def __init__(self, bridge: PersistentLayaRuntimeBridge):
        self.bridge = bridge

    def predict(self, canonical_state: Mapping[str, Any]) -> dict[str, Any]:
        return self.bridge.infer(canonical_state, DECISION_SCHEMA)
