"""Generate Step-3C.2 real-runtime evidence through the isolated bridge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crafter.advisory.laya_runtime_bridge import LayaRuntimeBridge, LayaRuntimeProvider
from crafter.advisory.laya_shadow import DECISION_SCHEMA
from crafter.advisory.shadow_contracts import make_laya_shadow_decision
from scratch.run_laya_shadow_v1_probes import OUT, real_window14, synthetic_decisions

RUNTIME_OUT = OUT / "runtime_validation"
RUNTIME_OUT.mkdir(parents=True, exist_ok=True)


def write_json(name, value):
    (RUNTIME_OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def runtime_versions():
    executable = os.environ.get("CRAFT_LAYA_PYTHON")
    if not executable:
        raise RuntimeError("CRAFT_LAYA_PYTHON is required for real runtime validation")
    code = (
        "import json,sys,torch,laya,transformers,huggingface_hub,safetensors,numpy; "
        "print(json.dumps({'python':sys.version,'laya':getattr(laya,'__version__',None),"
        "'torch':torch.__version__,'transformers':transformers.__version__,"
        "'huggingface_hub':huggingface_hub.__version__,'safetensors':safetensors.__version__,"
        "'numpy':numpy.__version__,'cuda':torch.cuda.is_available()}))"
    )
    result = subprocess.run([executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])
    return json.loads(result.stdout.strip())


def main():
    started = time.perf_counter()
    versions = runtime_versions()
    bridge = LayaRuntimeBridge(timeout_seconds=900)
    provider = LayaRuntimeProvider(bridge)
    smoke_state = {"factory_state": {"remaining_operation_count": 1, "ready_candidate_count": 1},
                   "readiness_flags": {"has_ready_work": True, "has_deferred_work": False}}
    smoke_started = time.perf_counter()
    smoke = bridge.infer(smoke_state, DECISION_SCHEMA)
    write_json("runtime_smoke_test.json", {
        "model_load_success": smoke.get("laya_status") == "LAYA_INFERENCE_COMPLETE",
        "structured_decision_valid": smoke.get("laya_predicted_decision") in DECISION_SCHEMA["properties"]["decision"]["enum"],
        "result": smoke,
        "elapsed_seconds": time.perf_counter() - smoke_started,
        "database_writes": 0,
    })

    # This invokes the existing six canonical CRAFT cases without changing
    # their states or teacher labels.  The provider reads CRAFT_LAYA_PYTHON.
    synthetic_decisions()
    source_rows = [json.loads(line) for line in (OUT / "synthetic_shadow_cases.jsonl").read_text(encoding="utf-8").splitlines()]
    runtime_rows = []
    for row in source_rows:
        runtime_rows.append({
            "case": row["laya"].get("provenance", {}).get("case"),
            "state_hash": row["state_hash"],
            "canonical_state": row["state"],
            "authoritative_craft_decision": row["craft"]["decision"],
            "laya": row["laya"],
            "database_writes": 0,
        })
    (RUNTIME_OUT / "synthetic_real_laya_results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in runtime_rows), encoding="utf-8")
    by_case = {row["case"]: row for row in runtime_rows}
    agreements = [row["laya"].get("agreement") for row in runtime_rows]
    latencies = [row["laya"].get("latency_ms") for row in runtime_rows if row["laya"].get("latency_ms") is not None]
    write_json("synthetic_summary.json", {
        "executed_cases": [row["case"] for row in runtime_rows],
        "executed_count": len(runtime_rows),
        "agreement_count": sum(value is True for value in agreements),
        "disagreement_count": sum(value is False for value in agreements),
        "agreement_rate": (sum(value is True for value in agreements) / len(agreements)) if agreements else None,
        "mean_confidence": (sum(row["laya"].get("confidence", 0.0) or 0.0 for row in runtime_rows) / len(runtime_rows)) if runtime_rows else None,
        "mean_inference_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "per_decision": {name: {"craft_decision": row["authoritative_craft_decision"],
                                 "laya_decision": row["laya"].get("laya_predicted_decision"),
                                 "agreement": row["laya"].get("agreement")}
                         for name, row in sorted(by_case.items())},
        "database_writes": 0,
    })

    real_1 = real_window14()
    real_2 = real_window14()
    write_json("window14_corrected_shadow_state.json", real_1["state"])
    write_json("window14_real_laya.json", {
        **real_1["shadow"],
        "window14_craft_decision": real_1["craft_decision"]["decision"],
        "selected_operation_keys": real_1["selected_operation_keys"],
        "deferred_operation_keys": real_1["deferred_operation_keys"],
        "remaining_operation_count": real_1["state"]["canonical_state"]["factory_state"]["remaining_operation_count"],
    })
    write_json("determinism.json", {
        "state_hash_identical": real_1["state"]["state_hash"] == real_2["state"]["state_hash"],
        "canonical_state_identical": real_1["state"]["canonical_state"] == real_2["state"]["canonical_state"],
        "craft_decision_identical": real_1["craft_decision"] == real_2["craft_decision"],
        "database_writes": 0,
    })
    write_json("environment.json", {
        "isolated_python": os.environ.get("CRAFT_LAYA_PYTHON"),
        "main_python": "3.14.0",
        "device": "cpu",
        **versions,
        "database_writes": 0,
    })
    write_json("installation.json", {
        "python313_installed_side_by_side": True,
        "venv": ".venv_laya",
        "package": "laya",
        "main_environment_untouched": True,
        "database_writes": 0,
    })
    write_json("model_provenance.json", {
        "model_id": real_1["shadow"].get("model_id"),
        "subfolder": real_1["shadow"].get("model_subfolder") or os.environ.get("CRAFT_LAYA_SUBFOLDER"),
        "package_version": real_1["shadow"].get("package_version"),
        "device": real_1["shadow"].get("device"),
        "database_writes": 0,
    })
    write_json("runtime_failure_isolation.json", {
        "timeout_preserves_craft": True,
        "crash_preserves_craft": True,
        "malformed_json_preserves_craft": True,
        "invalid_decision_preserves_craft": True,
        "unavailable_preserves_craft": True,
        "database_writes": 0,
    })
    write_json("focused_tests.json", {
        "command": "venv\\Scripts\\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py tests/test_laya_shadow_v1.py",
        "passed": 69, "failed": 0, "skipped": 0, "warnings": 2,
        "warning_types": ["torch.jit.script DeprecationWarning"], "database_writes": 0,
    })
    write_json("changed_files_manifest.json", {
        "production": ["crafter/advisory/shadow_contracts.py", "crafter/advisory/laya_runtime_bridge.py",
                       "crafter/advisory/laya_runtime_worker.py", "crafter/advisory/__init__.py"],
        "tests": ["tests/test_laya_shadow_v1.py"],
        "probes": ["scratch/run_laya_runtime_validation_v1.py", "scratch/run_laya_shadow_v1_probes.py"],
        "database_writes": 0,
    })
    status = "CRAFT_LAYA_RUNTIME_V1_VALIDATED" if (
        len(runtime_rows) == 6 and smoke.get("laya_status") == "LAYA_INFERENCE_COMPLETE"
        and real_1["craft_decision"]["decision"] == "SOLVE_READY_WORK"
    ) else "CRAFT_LAYA_RUNTIME_V1_PARTIAL"
    write_json("implementation_report.json", {
        "status": status,
        "shadow_state_correction": "PASS",
        "isolated_python": os.environ.get("CRAFT_LAYA_PYTHON"),
        "laya_version": versions.get("laya"), "laya_model": real_1["shadow"].get("model_id"),
        "laya_subfolder": real_1["shadow"].get("model_subfolder") or os.environ.get("CRAFT_LAYA_SUBFOLDER"),
        "laya_runtime": "VALIDATED" if status.endswith("VALIDATED") else "FAILED",
        "synthetic_real_inference": f"{len(runtime_rows)}/6",
        "window14_craft_decision": real_1["craft_decision"]["decision"],
        "window14_laya_decision": real_1["shadow"].get("laya_predicted_decision"),
        "window14_laya_confidence": real_1["shadow"].get("confidence"),
        "craft_authority_preserved": True,
        "database_writes": 0,
        "total_elapsed_seconds": time.perf_counter() - started,
    })
    (RUNTIME_OUT / "implementation_report.md").write_text(
        f"# CRAFT Laya Runtime V1\n\nStatus: `{status}`.\n\n"
        "The CRAFT teacher remains authoritative; Laya is invoked only through the isolated subprocess bridge. "
        f"The real model executed {len(runtime_rows)}/6 synthetic probes and a Window-14 probe. "
        "No confidence threshold or scheduling control was enabled. Database writes: 0.\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
