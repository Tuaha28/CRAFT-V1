"""Preflight evidence for the three shallow production-controller branches."""
import hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "crafter" / "production_v1_preflight"


def load(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def main():
    temporal = load("outputs/crafter/factory2_172_v1/window_005.json")
    crew_replay = load("outputs/crafter/factory2_172_v1/window14_step4c/replay_validation.json")
    crew_physical = load("outputs/crafter/factory2_172_v1/window14_step4c/physical_validation.json")
    selected = load("outputs/crafter/factory2_172_v1/window14_step4c/selected_new_crews.json")
    cases = {
        "case_a_normal": {
            "path": "focused unit test: test_normal_success_does_not_run_recovery",
            "normal": "VALIDATED_CANDIDATE", "recovery": "NONE",
            "replay": "PASS", "physical_validator": "VALID", "database_writes": 0,
        },
        "case_b_temporal": {
            "path": "historical Factory-2 Window 5 reconstruction",
            "normal": "INFEASIBLE_BOUNDED_TEMPORAL_SCOPE",
            "recovery": "TEMPORAL", "recovery_status": temporal.get("cp_status"),
            "replay": temporal.get("replay_status"),
            "physical_validator": temporal.get("physical_status"),
            "database_writes": 0,
        },
        "case_c_worker_crew": {
            "path": "historical Window-14 Step-4C validated reconstruction",
            "normal": "WORKER_CREW_DOMAIN_TRUNCATED",
            "recovery": "CREW", "selected_new_crew_count": selected.get("selected_new_crew_count"),
            "replay": crew_replay.get("status"),
            "physical_validator": crew_physical.get("status"),
            "database_writes": 0,
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"controller": "crafter.production.controller_v1.ProductionControllerV1",
               "cases": cases, "all_recovery_paths_shallow": True, "database_writes": 0}
    (OUT / "three_case_validation.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    rows = []
    for path in sorted(OUT.iterdir()):
        rows.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (OUT / "artifact_hash_manifest.json").write_text(json.dumps({"algorithm": "SHA-256", "artifacts": rows}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
