"""Fingerprint this isolated audit without touching frozen artifacts."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FILES = (
    "web_v1/audits/execution_boundary_v1/factory1_source.json",
    "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json",
    "web_v1/audits/factory1_simulated_continuation_v1/build_scenario.py",
    "web_v1/audits/factory1_simulated_continuation_v1/validate_scenario.py",
    "web_v1/audits/factory1_simulated_continuation_v1/run_bounded_smoke.py",
    "web_v1/audits/factory1_simulated_continuation_v1/test_scenario.py",
    "web_v1/audits/factory1_simulated_continuation_v1/scenario.json",
    "web_v1/audits/factory1_simulated_continuation_v1/identity_ledger.json",
    "web_v1/audits/factory1_simulated_continuation_v1/validation.json",
    "web_v1/audits/factory1_simulated_continuation_v1/bounded_smoke.json",
    "web_v1/audits/factory1_simulated_continuation_v1/bounded_smoke_timing.json",
)


def main():
    scenario = json.loads((HERE / "scenario.json").read_text())
    manifest = {
        "audit": "FACTORY1_SIMULATED_CONTINUATION_V1",
        "reference_time": scenario["reference_time"],
        "scenario_id": scenario["scenario_id"],
        "seed": scenario["seed"],
        "scenario_hash": scenario["scenario_hash"],
        "factory_wide_schedule_completed": False,
        "production_admission_enabled": False,
        "files_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES},
    }
    target = HERE / "manifest.json"
    if target.exists() and json.loads(target.read_text()) != manifest:
        raise RuntimeError("EXISTING_MANIFEST_DIFFERS")
    if not target.exists():
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"files": len(FILES), "scenario_hash": scenario["scenario_hash"]}))


if __name__ == "__main__":
    main()
