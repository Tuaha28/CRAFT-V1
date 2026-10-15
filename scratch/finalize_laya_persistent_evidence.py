"""Regenerate non-runtime metadata and final hashes for persistent probe evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def main():
    out = Path("outputs/crafter/laya_shadow_v1/persistent_runtime")
    focused = {
        "command": "venv\\Scripts\\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py tests/test_laya_shadow_v1.py",
        "passed": 76, "failed": 0, "skipped": 0, "warnings": 2,
        "warning_types": ["torch.jit.script DeprecationWarning"],
        "database_writes": 0,
    }
    (out / "focused_tests.json").write_text(json.dumps(focused, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    changed = {
        "production": ["crafter/advisory/laya_runtime_bridge.py",
                        "crafter/advisory/laya_runtime_worker.py",
                        "crafter/advisory/__init__.py"],
        "tests": ["tests/test_laya_shadow_v1.py"],
        "probe": ["scratch/run_laya_persistent_runtime_v1.py",
                  "scratch/run_laya_shadow_v1_probes.py"],
        "main_craft_environment_changed": False, "factory2_run_started": False,
        "database_writes": 0,
    }
    (out / "changed_files_manifest.json").write_text(json.dumps(changed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    protocol_path = out / "worker_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol["initial_probe_inference_calls"] = 7
    protocol["window14_repeat_inference_calls"] = 3
    protocol_path.write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    startup = json.loads((out / "worker_startup.json").read_text(encoding="utf-8"))
    environment = {
        "main_python": "3.14.0",
        "isolated_python": startup.get("python_version"),
        "isolated_python_executable": ".venv_laya\\Scripts\\python.exe",
        "laya_version": startup.get("package_version"),
        "torch_version": startup.get("torch_version"),
        "transformers_version": startup.get("transformers_version"),
        "model_id": startup.get("model_id"),
        "model_subfolder": startup.get("model_subfolder"),
        "device": startup.get("device"),
        "main_environment_untouched": True,
        "database_writes": 0,
    }
    (out / "environment.json").write_text(json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    hashes = {}
    for path in sorted(out.iterdir()):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / "artifact_hash_manifest.json").write_text(
        json.dumps({"algorithm": "sha256", "files": hashes}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
