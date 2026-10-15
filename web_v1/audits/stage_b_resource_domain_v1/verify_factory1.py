"""Compare saved, source-pinned 25-operation domains without rerunning CP-SAT."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LEGACY_DB_FORBIDDEN"))

from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SNAPSHOT, SCENARIO
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload
from web_v1.audits.global_resource_state_stage_a.run_factory1 import build_snapshot
from web_v1.backend.integrations.craft.global_resource_state import validate_candidate

HERE = Path(__file__).resolve().parent
BASE = ROOT / "web_v1/audits/factory1_realistic_neighborhood_v1"


def metrics(output):
    windows = output["windows"]
    return {"configurations": output["domain"]["selected_configurations"],
            "accepted_windows": sum(row["status"] == "ACCEPTED" for row in windows),
            "scheduled_operations": len(output["accepted_keys"]),
            "calendar_regimes": sum(row["calendar_regimes"] for row in windows),
            "construction_seconds": output["construction_seconds"],
            "calendar_seconds": sum(row["calendar_seconds"] for row in windows),
            "cp_solve_seconds_reported": sum(stage.get("solve_seconds") or 0
                                             for row in windows for stage in row["stages"]),
            "total_wall_seconds": output["total_wall_seconds"],
            "sampled_peak_rss_mb": output["sampled_peak_rss_mb"],
            "replay": [row["replay"] for row in windows],
            "physical": [row["physical"] for row in windows],
            "scope": [row["scope"] for row in windows]}


def main():
    started = time.perf_counter()
    source_bytes = SOURCE.read_bytes()
    normalized = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
    baseline = json.loads((BASE / "bounded_25_expanded_v1.json").read_text(encoding="utf-8"))
    stage_b = json.loads((BASE / "bounded_25_stage_b_v1.json").read_text(encoding="utf-8"))
    same_input = (baseline["source_sha256"] == stage_b["source_sha256"] == sha256(source_bytes).hexdigest()
                  and baseline["snapshot_hash"] == stage_b["snapshot_hash"] == normalized["snapshot_hash"]
                  and baseline["scenario_hash"] == stage_b["scenario_hash"] == scenario["scenario_hash"]
                  and baseline["selected_keys"] == stage_b["selected_keys"])
    if not same_input:
        raise RuntimeError("FACTORY1_PINNED_INPUT_CHANGED")
    independent = validate_payload(json.loads(source_bytes), normalized, scenario,
                                   stage_b, source_bytes)
    global_state = build_snapshot(json.loads(source_bytes), normalized, scenario,
                                  stage_b["selected_keys"])
    candidate_rows = [row for window in stage_b["windows"]
                      for row in window["selected_schedule"]]
    global_check = validate_candidate(global_state, candidate_rows)
    new = metrics(stage_b)
    report = {"classification": "SIMULATION_ONLY_NOT_PRODUCTION",
              "source_sha256": stage_b["source_sha256"],
              "snapshot_hash": stage_b["snapshot_hash"],
              "scenario_hash": stage_b["scenario_hash"],
              "global_state_fingerprint": global_state["fingerprint"],
              "same_source_and_operation_set": same_input,
              "baseline": metrics(baseline), "stage_b": new,
              "resource_choices_changed": baseline["domain"]["chosen_machine_ids"] != stage_b["domain"]["chosen_machine_ids"]
                                          or baseline["domain"]["chosen_worker_ids"] != stage_b["domain"]["chosen_worker_ids"],
              "complete_legal_domain_proven": False,
              "approved_outside_assignments": len(global_state["approved_outside"]),
              "simulated_fixed_holds": len(global_state["fixed_commitments"]),
              "independent_assembled": independent,
              "global_conflict": global_check,
              "verification_wall_seconds": time.perf_counter() - started}
    report["status"] = ("PASS" if same_input and new["scheduled_operations"] == 25
                        and new["accepted_windows"] == 7
                        and all(value == "PASS" for value in new["replay"])
                        and all(value == "VALID" for value in new["physical"])
                        and all(value == "SCOPE_VALID" for value in new["scope"])
                        and independent["status"] == global_check["status"] == "PASS"
                        else "FAIL")
    target = HERE / "factory1_25_comparison_v1.json"
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "baseline": report["baseline"],
                      "stage_b": new, "independent": independent["status"],
                      "global_conflict": global_check["status"], "path": str(target)}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
