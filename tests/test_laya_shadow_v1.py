from types import SimpleNamespace
import sys

from crafter.advisory.laya_shadow import DECISION_SCHEMA, LayaShadowProvider
from crafter.advisory.laya_runtime_bridge import LayaRuntimeBridge, PersistentLayaRuntimeBridge
from crafter.advisory.shadow_contracts import (
    CRAFT_SHADOW_STATE_V1,
    LAYA_DECISION_OPTIONS,
    build_shadow_state,
    build_shadow_trace_row,
    canonical_json,
    make_laya_shadow_decision,
    state_hash,
)
from crafter.repair.rolling_horizon_v2 import (
    RollingOrchestrationDecisionV2,
    SOLVE_READY_WORK,
)


def _decision(decision=SOLVE_READY_WORK, *, ready=1, deferred=0, unresolved=0,
              event=None, blockers=()):
    scan = {
        "results": [], "ready_candidate_count": ready,
        "deferred_candidate_count": deferred,
        "unresolved_candidate_count": unresolved,
    }
    if deferred:
        scan["results"].append({
            "operation_key": "deferred-1", "decision": "BOUNDARY_DEFERRED",
            "earliest_ready_seconds": 700,
            "blockers": [{"type": "COMMITTED_PREDECESSOR_COMPLETION",
                          "operation_key": "pred-1",
                          "authoritative_end_seconds": 700}],
        })
    return RollingOrchestrationDecisionV2(
        window_id="w", window_start_seconds=0, window_end_seconds=500,
        commit_end_seconds=200, admitted_operation_keys=("ready-1",) if ready else (),
        deferred_operation_keys=("deferred-1",) if deferred else (),
        decision=decision, decision_reason="teacher reason must not enter state",
        trigger_blockers=tuple(blockers), candidate_pool_count=ready + deferred + unresolved,
        ready_candidate_count=ready, deferred_candidate_count=deferred,
        unresolved_candidate_count=unresolved,
        provenance={"readiness_scan": scan, **({"next_relevant_event": event} if event else {})},
    )


def test_shadow_state_excludes_teacher_decision_and_reason():
    state = build_shadow_state(_decision())
    assert "authoritative_craft_decision" not in state.canonical_state
    assert "decision_reason" not in state.canonical_state
    assert "decision" not in state.canonical_state
    assert state.canonical_state["contract_version"] == CRAFT_SHADOW_STATE_V1


def test_canonical_state_hash_is_deterministic():
    first = {"b": {"z": 2, "a": 1}, "a": [set((3, 1)), 2]}
    second = {"a": [[1, 3], 2], "b": {"a": 1, "z": 2}}
    assert canonical_json(first) == canonical_json(second)
    assert state_hash(first) == state_hash(second)


def test_mock_laya_cannot_mutate_craft_decision_or_shadow_state():
    state = build_shadow_state(_decision())
    seen = {}

    def predictor(payload):
        seen.update(payload)
        payload["factory_state"]["ready_candidate_count"] = 0
        return {"decision": "FAST_FORWARD_TO_EVENT", "probabilities": {
            option: (1.0 if option == "FAST_FORWARD_TO_EVENT" else 0.0)
            for option in LAYA_DECISION_OPTIONS}, "confidence": 0.8}

    result = make_laya_shadow_decision(
        shadow_state=state, craft_decision=SOLVE_READY_WORK,
        provider=LayaShadowProvider(predictor=predictor))
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.disagreement_type == "LAYA_FAST_FORWARD_WHILE_CRAFT_HAS_READY_WORK"
    assert state.canonical_state["factory_state"]["ready_candidate_count"] == 1
    assert "authoritative_craft_decision" not in seen
    assert "decision_reason" not in seen


def test_laya_exception_preserves_authoritative_decision():
    provider = LayaShadowProvider(predictor=lambda _state: 1 / 0)
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=provider)
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_INFERENCE_ERROR"
    assert result.error_type == "ZeroDivisionError"


def test_invalid_laya_choice_is_logged_and_rejected():
    provider = LayaShadowProvider(predictor=lambda _state: {"decision": "NOT_A_DECISION"})
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=provider)
    assert result.laya_status == "LAYA_INVALID_DECISION"
    assert result.laya_predicted_decision is None
    assert result.error_type == "INVALID_LAYA_DECISION"
    assert result.authoritative_craft_decision == SOLVE_READY_WORK


def test_laya_unavailable_does_not_break_deterministic_orchestration():
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=LayaShadowProvider())
    assert result.laya_status in {"LAYA_UNAVAILABLE", "LAYA_RUNTIME_NOT_ESTABLISHED"}
    assert result.authoritative_craft_decision == SOLVE_READY_WORK


def test_probability_and_confidence_logging_is_preserved():
    provider = LayaShadowProvider(predictor=lambda _state: {
        "decision": "SOLVE_READY_WORK", "probabilities": {
            option: (0.9 if option == "SOLVE_READY_WORK" else 0.02)
            for option in LAYA_DECISION_OPTIONS},
        "confidence": 0.9, "answer_confidence": 0.88,
        "model_id": "mock-laya", "package_version": "test", "device": "cpu",
    })
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=provider)
    assert result.agreement is True
    assert result.probabilities["SOLVE_READY_WORK"] == 0.9
    assert result.confidence == 0.9
    assert result.answer_confidence == 0.88
    assert result.model_id == "mock-laya"


def test_trace_attaches_teacher_label_only_after_inference_input_exists():
    state = build_shadow_state(_decision())
    observed = {}

    def predictor(payload):
        observed.update(payload)
        return {"decision": "SOLVE_READY_WORK"}

    shadow = make_laya_shadow_decision(
        shadow_state=state, craft_decision=SOLVE_READY_WORK,
        provider=LayaShadowProvider(predictor=predictor))
    row = build_shadow_trace_row(
        run_id="test", factory_id=1, window_id="w", shadow_state=state,
        craft_decision=SOLVE_READY_WORK,
        craft_provenance={"source": "deterministic-craft"}, shadow_decision=shadow)
    assert "authoritative_craft_decision" not in observed
    assert row["craft"]["decision"] == SOLVE_READY_WORK
    assert row["laya"]["authoritative_craft_decision"] == SOLVE_READY_WORK
    assert row["database_writes"] == 0


def test_decision_schema_contains_exact_six_choices():
    assert DECISION_SCHEMA["properties"]["decision"]["enum"] == list(LAYA_DECISION_OPTIONS)


def test_remaining_count_is_explicit_and_independent_of_candidate_pool():
    state = build_shadow_state(_decision(ready=2), remaining_operation_count=10)
    assert state.canonical_state["factory_state"]["candidate_pool_count"] == 2
    assert state.canonical_state["factory_state"]["remaining_operation_count"] == 10


def test_missing_remaining_count_is_null_not_fabricated():
    state = build_shadow_state(_decision(ready=2))
    assert state.canonical_state["factory_state"]["remaining_operation_count"] is None


def test_ready_and_deferred_authoritative_event_is_exposed_independently_of_label():
    state = build_shadow_state(_decision(ready=1, deferred=1), remaining_operation_count=2)
    assert state.canonical_state["readiness_flags"]["has_ready_work"] is True
    assert state.canonical_state["readiness_flags"]["has_deferred_work"] is True
    assert state.canonical_state["readiness_flags"]["has_finite_authoritative_event"] is True
    assert state.canonical_state["temporal"]["earliest_authoritative_event_type"] == \
        "COMMITTED_PREDECESSOR_COMPLETION"
    assert state.canonical_state["temporal"]["seconds_until_earliest_authoritative_event"] == 700


def test_isolated_bridge_invalid_response_cannot_change_craft_decision(tmp_path):
    worker = tmp_path / "invalid_worker.py"
    worker.write_text("print('not-json')", encoding="utf-8")
    bridge = LayaRuntimeBridge(python_executable=sys.executable,
                               worker_command=(sys.executable, str(worker)))
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_INVALID_RESPONSE"


def test_isolated_bridge_invalid_choice_is_logged_without_control(tmp_path):
    worker = tmp_path / "invalid_choice_worker.py"
    worker.write_text("import json; print(json.dumps({'decision':'BAD'}))", encoding="utf-8")
    bridge = LayaRuntimeBridge(python_executable=sys.executable,
                               worker_command=(sys.executable, str(worker)))
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_INVALID_DECISION"


def test_isolated_bridge_timeout_cannot_change_craft_decision(tmp_path):
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time; time.sleep(2)", encoding="utf-8")
    bridge = LayaRuntimeBridge(python_executable=sys.executable,
                               worker_command=(sys.executable, str(worker)),
                               timeout_seconds=0.05)
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_RUNTIME_TIMEOUT"


def test_isolated_bridge_crash_cannot_change_craft_decision(tmp_path):
    worker = tmp_path / "crash_worker.py"
    worker.write_text("raise RuntimeError('crash')", encoding="utf-8")
    bridge = LayaRuntimeBridge(python_executable=sys.executable,
                               worker_command=(sys.executable, str(worker)))
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_RUNTIME_ERROR"


def _persistent_mock_worker(tmp_path, mode="normal"):
    worker = tmp_path / f"persistent_{mode}.py"
    worker.write_text("""
import json, os, sys, time
mode = %r
print(json.dumps({'status':'READY','protocol_version':'CRAFT_LAYA_JSONL_V1',
 'worker_pid':os.getpid(),'model_load_count':1,'model_id':'mock','model_subfolder':'test',
 'device':'cpu','model_load_latency_ms':1.0}), flush=True)
for raw in sys.stdin:
    p = json.loads(raw)
    if p.get('action') == 'ping':
        print(json.dumps({'request_id':p.get('request_id'),'status':'PONG','worker_pid':os.getpid(),
                          'model_load_count':1}), flush=True)
    elif p.get('action') == 'shutdown':
        print(json.dumps({'request_id':p.get('request_id'),'status':'SHUTDOWN_ACK'}), flush=True)
        break
    elif p.get('action') == 'infer':
        if mode == 'mismatch':
            print(json.dumps({'request_id':'wrong','status':'LAYA_INFERENCE_COMPLETE',
                              'laya_predicted_decision':'SOLVE_READY_WORK'}), flush=True)
        elif mode == 'malformed':
            print('not-json', flush=True)
        elif mode == 'slow':
            time.sleep(2)
        elif mode == 'crash':
            os._exit(3)
        else:
            print(json.dumps({'request_id':p.get('request_id'),'status':'LAYA_INFERENCE_COMPLETE',
                              'laya_predicted_decision':'SOLVE_READY_WORK','confidence':0.5,
                              'model_load_count':1}), flush=True)
""" % mode, encoding="utf-8")
    return worker


def test_persistent_worker_ready_reuses_pid_and_loads_once(tmp_path):
    worker = _persistent_mock_worker(tmp_path)
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=2)
    startup = bridge.start()
    first = bridge.infer({"x": 1})
    second = bridge.infer({"x": 2})
    assert startup["status"] == "READY"
    assert first["status"] == second["status"] == "LAYA_INFERENCE_COMPLETE"
    assert first["worker_pid"] if "worker_pid" in first else True
    assert bridge.worker_pid == startup["worker_pid"]
    assert startup["model_load_count"] == first["model_load_count"] == 1
    assert bridge.close()["worker_exited"] is True


def test_persistent_request_ids_and_shutdown(tmp_path):
    worker = _persistent_mock_worker(tmp_path)
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=2)
    result = bridge.infer({}, request_id="window-001")
    assert result["request_id"] == "window-001"
    assert bridge.close()["status"] == "SHUTDOWN_ACK"


def test_persistent_mismatched_request_id_is_protocol_error(tmp_path):
    worker = _persistent_mock_worker(tmp_path, "mismatch")
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=2,
                                         auto_restart_once=False)
    result = bridge.infer({})
    assert result["status"] == "LAYA_PROTOCOL_ERROR"
    assert bridge.close()["worker_exited"] is True


def test_persistent_malformed_response_is_safe_failure(tmp_path):
    worker = _persistent_mock_worker(tmp_path, "malformed")
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=2,
                                         auto_restart_once=False)
    result = bridge.infer({})
    assert result["status"] == "LAYA_INVALID_RESPONSE"
    bridge.close()


def test_persistent_timeout_does_not_change_craft(tmp_path):
    worker = _persistent_mock_worker(tmp_path, "slow")
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=0.05,
                                         auto_restart_once=False)
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status == "LAYA_RUNTIME_TIMEOUT"
    bridge.close()


def test_persistent_worker_crash_isolated_and_restart_bounded(tmp_path):
    worker = _persistent_mock_worker(tmp_path, "crash")
    bridge = PersistentLayaRuntimeBridge(worker_command=(sys.executable, str(worker)),
                                         startup_timeout_seconds=2, inference_timeout_seconds=1)
    result = make_laya_shadow_decision(
        shadow_state=build_shadow_state(_decision()), craft_decision=SOLVE_READY_WORK,
        provider=type("Provider", (), {"predict": lambda _self, state: bridge.infer(state)})())
    assert result.authoritative_craft_decision == SOLVE_READY_WORK
    assert result.laya_status in {"LAYA_WORKER_DIED", "LAYA_RUNTIME_ERROR"}
    assert bridge.worker_restart_count <= 1
    bridge.close()


def test_legacy_oneshot_bridge_accepts_ready_then_jsonl_result(tmp_path):
    worker = tmp_path / "ready_then_result.py"
    worker.write_text(
        "import json; print(json.dumps({'status':'READY'})); "
        "print(json.dumps({'laya_status':'LAYA_INFERENCE_COMPLETE',"
        "'laya_predicted_decision':'SOLVE_READY_WORK'}))",
        encoding="utf-8")
    bridge = LayaRuntimeBridge(python_executable=sys.executable,
                               worker_command=(sys.executable, str(worker)))
    result = bridge.infer({})
    assert result["laya_status"] == "LAYA_INFERENCE_COMPLETE"
    assert result["laya_predicted_decision"] == SOLVE_READY_WORK
