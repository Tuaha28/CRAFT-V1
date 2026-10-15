"""Run the neutral Factory-2 EDD baseline using the validated FIFO engine."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crafter.baselines.fifo_v1 import (  # noqa: E402
    build_workload_from_neutral_payload,
    run_edd_schedule,
    validate_fifo_schedule,
)
from crafter.evaluation.final_kpis import evaluate_final_schedule  # noqa: E402

EVAL = ROOT / "outputs" / "crafter" / "evaluation_v2"
LOAD = ROOT / "outputs" / "crafter" / "load_context_v1"
CRAFT = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
FIFO = ROOT / "outputs" / "crafter" / "baselines" / "fifo_factory2_v1"
OUT = ROOT / "outputs" / "crafter" / "baselines" / "edd_factory2_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(directory):
    return {str(path.relative_to(ROOT)): sha(path)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def main():
    frozen_before = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT), **hashes(FIFO)}
    neutral = read(EVAL / "neutral_factory2_state.json")
    identity = read(EVAL / "neutral_factory2_identity.json")
    craft_metrics = read(EVAL / "craft_factory2_metrics_v2.json")
    fifo_metrics = read(FIFO / "fifo_metrics.json")
    workload = build_workload_from_neutral_payload(neutral)

    write("edd_contract.json", {
        "contract": "EDD_FACTORY2_BASELINE_V1",
        "policy": {
            "ready_queue": "released and predecessor-complete operations only",
            "priority": ["order_due_date", "queue_entry_time", "order_id",
                          "routing_sequence", "operation_key"],
            "missing_due_dates": "after valid due dates, deterministic tie-breaks",
            "construction": "same event-driven parallel engine as FIFO",
            "resource_tie_break": ["earliest feasible start", "line_id",
                                   "machine_ids", "worker_ids"],
        },
        "shared_engine": "crafter.baselines.fifo_v1.run_fifo_schedule(policy=EDD)",
        "neutral_identity": identity,
        "database_access": "READ_ONLY_ARTIFACTS_ONLY",
        "database_writes": 0,
        "craft_rerun": False, "fifo_rerun": False, "spt_run": False,
    })

    result = run_edd_schedule(workload)
    write("edd_schedule.json", {
        "status": result["status"], "policy": result["policy"],
        "schedule": result["schedule"],
        "operation_count": len(result["schedule"]), "database_writes": 0,
    })
    validation = validate_fifo_schedule(workload, result["schedule"])
    write("edd_validation.json", validation)
    write("edd_runtime.json", {
        "status": "VALID" if result["status"] == "COMPLETE" else "INCOMPLETE",
        "edd_scheduler_seconds": result["runtime_seconds"],
        "measurement": "run_edd_schedule wall clock only; excludes validation, KPIs, artifacts, and Codex session",
        "events_processed": result["events_processed"], "database_writes": 0,
    })
    write("edd_diagnostics.json", {
        "status": result["status"],
        "unscheduled_operation_keys": result["unscheduled_operation_keys"],
        "failure": result["failure"], "allocation_profile": result["allocation_profile"],
        "events_processed": result["events_processed"], "event_log": result["event_log"],
        "neutral_identity_match": identity["identity_match"], "database_writes": 0,
    })

    comparable = False
    metrics = {"validation_status": "NOT_COMPARABLE",
               "reason": "EDD did not pass complete canonical validation",
               "operations_completed": len(result["schedule"]), "database_writes": 0}
    if result["status"] == "COMPLETE" and validation["accepted"]:
        metrics = evaluate_final_schedule(
            workload, result["schedule"], validation=validation,
            runtime_profile={"benchmark_wall_seconds": result["runtime_seconds"],
                             "window_total_seconds": result["runtime_seconds"]},
        )
        comparable = metrics.get("validation_status") == "VALID"
    write("edd_metrics.json", metrics)

    comparison = {"classification": "DESCRIPTIVE_NO_OVERALL_WINNER",
                  "comparable": comparable, "metrics": {}}
    if comparable:
        mapping = {
            "operations_completed": "operations_completed",
            "makespan_seconds": "makespan_seconds",
            "on_time_delivery_percentage": "on_time_delivery_percentage",
            "total_tardiness_seconds": "total_tardiness_seconds",
            "mean_tardiness_seconds": "mean_tardiness_seconds",
            "mean_flow_time_seconds": "mean_flow_time_seconds",
        }
        for label, key in mapping.items():
            comparison["metrics"][label] = {
                "CRAFT": craft_metrics[key], "FIFO": fifo_metrics[key], "EDD": metrics[key]}
        for key in ("worker_utilization", "machine_utilization"):
            comparison["metrics"][key] = {
                "CRAFT": craft_metrics[key]["utilization"],
                "FIFO": fifo_metrics[key]["utilization"],
                "EDD": metrics[key]["utilization"],
            }
        comparison["metrics"]["scheduler_runtime_seconds"] = {
            "CRAFT": craft_metrics["runtime"]["accepted_window_runtime_seconds"],
            "FIFO": read(FIFO / "fifo_runtime.json")["fifo_scheduler_seconds"],
            "EDD": result["runtime_seconds"],
        }
    write("edd_vs_fifo_vs_craft.json", comparison)

    frozen_after = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT), **hashes(FIFO)}
    changed = sorted(name for name, digest in frozen_before.items()
                     if frozen_after.get(name) != digest)
    complete = len(result["schedule"]) == len(neutral["operations"])
    baseline_valid = bool(complete and validation["accepted"] and comparable and not changed)
    status = "EDD_BASELINE_VALID" if baseline_valid else (
        "EDD_BASELINE_INCOMPLETE" if not complete else "EDD_BASELINE_VALIDATION_FAILED")
    blockers = []
    if result["failure"]:
        blockers.append(result["failure"])
    if not validation["accepted"]:
        blockers.append({"classification": "VALIDATION_FAILED",
                         "scope": validation["scope_status"],
                         "replay": validation["replay_status"],
                         "physical": validation["physical_status"]})
    if changed:
        blockers.append({"classification": "FROZEN_ARTIFACT_MUTATION", "files": changed})
    write("edd_report.json", {
        "status": status, "edd_baseline_valid": baseline_valid,
        "safe_to_run_spt": baseline_valid,
        "neutral_operation_count": len(neutral["operations"]),
        "neutral_identity_match": identity["identity_match"],
        "operations_scheduled": len(result["schedule"]),
        "orders_completed": metrics.get("orders_completed") if comparable else None,
        "validation": {"scope": validation["scope_status"],
                        "replay": validation["replay_status"],
                        "physical": validation["physical_status"],
                        "accepted": validation["accepted"]},
        "runtime_seconds": result["runtime_seconds"], "blockers": blockers,
        "frozen_source_artifacts_unchanged": not changed,
        "craft_rerun": False, "fifo_rerun": False, "spt_run": False,
        "database_read_only": True, "database_writes": 0,
    })
    md = f"""# EDD Factory-2 baseline V1

Status: `{status}`

- Neutral workload: `{len(neutral['operations'])}` operations; identity match `{identity['identity_match']}`.
- Scheduled: `{len(result['schedule'])}` operations; orders completed: `{metrics.get('orders_completed', 'NOT_AVAILABLE')}`.
- Scope / Replay / Physical: `{validation['scope_status']}` / `{validation['replay_status']}` / `{validation['physical_status']}`.
- EDD scheduler runtime: `{result['runtime_seconds']:.6f}` seconds.
- Shared engine: validated FIFO event/resource/replay-stability engine; only ready-operation priority changed to EDD.
- CRAFT rerun: `false`; FIFO rerun: `false`; SPT run: `false`; database writes: `0`.
- Frozen Evaluation V2, load-context, CRAFT, and FIFO artifacts unchanged: `{not changed}`.
"""
    if comparable:
        md += f"""
## EDD metrics

- Makespan: `{metrics['makespan_seconds']}` seconds.
- On time: `{metrics['on_time_orders']} / {metrics['orders_with_due_dates']}` (`{metrics['on_time_delivery_percentage']:.6f}%`).
- Total tardiness: `{metrics['total_tardiness_seconds']}` seconds.
- Mean tardiness: `{metrics['mean_tardiness_seconds']}` seconds.
- Maximum tardiness: `{metrics['maximum_tardiness_seconds']}` seconds.
- Mean flow time: `{metrics['mean_flow_time_seconds']}` seconds.
- Worker utilization: `{metrics['worker_utilization']['utilization']}`.
- Machine utilization: `{metrics['machine_utilization']['utilization']}`.
"""
    (OUT / "edd_report.md").write_text(md, encoding="utf-8")

    primary = [p for p in OUT.iterdir() if p.is_file()
               and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"}]
    manifest = {p.name: sha(p) for p in sorted(primary)}
    write("artifact_hash_manifest.json", manifest)
    checks = {name: sha(OUT / name) == digest for name, digest in manifest.items()}
    write("hash_verification.json", {"verified": all(checks.values()),
                                     "files": checks, "database_writes": 0})

    def value(key, default="NOT_AVAILABLE"):
        return metrics.get(key, default)

    print(f"STATUS = {status}")
    print("CRAFT_RERUN = no")
    print("FIFO_RERUN = no")
    print("SPT_RUN = no")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    print(f"NEUTRAL_OPERATION_COUNT = {len(neutral['operations'])}")
    print(f"NEUTRAL_IDENTITY_MATCH = {str(identity['identity_match']).lower()}")
    print(f"EDD_OPERATIONS_SCHEDULED = {len(result['schedule'])}")
    print(f"EDD_ORDERS_COMPLETED = {value('orders_completed')}")
    print(f"EDD_COMPLETE = {str(complete).lower()}")
    print(f"EDD_REPLAY_STATUS = {validation['replay_status']}")
    print(f"EDD_PHYSICAL_STATUS = {validation['physical_status']}")
    print(f"EDD_SCOPE_STATUS = {validation['scope_status']}")
    print(f"EDD_VALIDATION_STATUS = {'VALID' if validation['accepted'] else 'INVALID'}")
    for label, key in (("EDD_MAKESPAN_SECONDS", "makespan_seconds"),
                       ("EDD_ON_TIME_ORDERS", "on_time_orders"),
                       ("EDD_LATE_ORDERS", "late_orders"),
                       ("EDD_ON_TIME_PERCENT", "on_time_delivery_percentage"),
                       ("EDD_TOTAL_TARDINESS_SECONDS", "total_tardiness_seconds"),
                       ("EDD_MEAN_TARDINESS_SECONDS", "mean_tardiness_seconds"),
                       ("EDD_MAX_TARDINESS_SECONDS", "maximum_tardiness_seconds"),
                       ("EDD_MEAN_FLOW_TIME_SECONDS", "mean_flow_time_seconds")):
        print(f"{label} = {value(key)}")
    print(f"EDD_WORKER_UTILIZATION = {value('worker_utilization', {}).get('utilization', 'NOT_AVAILABLE') if isinstance(value('worker_utilization', {}), dict) else 'NOT_AVAILABLE'}")
    print(f"EDD_MACHINE_UTILIZATION = {value('machine_utilization', {}).get('utilization', 'NOT_AVAILABLE') if isinstance(value('machine_utilization', {}), dict) else 'NOT_AVAILABLE'}")
    print(f"EDD_RUNTIME_SECONDS = {result['runtime_seconds']}")
    print("EDD_BASELINE_VALID = " + ("yes" if baseline_valid else "no"))
    print("SAFE_TO_RUN_SPT = " + ("yes" if baseline_valid else "no"))
    print(f"BLOCKERS = {json.dumps(blockers, sort_keys=True)}")
    print("NEXT_STEP = Run SPT only after separate authorization." if baseline_valid else "NEXT_STEP = Diagnose EDD blocker; do not run SPT.")


if __name__ == "__main__":
    main()
