"""Run the neutral Factory-2 SPT baseline using the common dispatch engine."""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crafter.baselines.fifo_v1 import (  # noqa: E402
    build_workload_from_neutral_payload,
    run_spt_schedule,
    validate_fifo_schedule,
)
from crafter.evaluation.final_kpis import evaluate_final_schedule  # noqa: E402

EVAL = ROOT / "outputs" / "crafter" / "evaluation_v2"
LOAD = ROOT / "outputs" / "crafter" / "load_context_v1"
CRAFT = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
FIFO = ROOT / "outputs" / "crafter" / "baselines" / "fifo_factory2_v1"
EDD = ROOT / "outputs" / "crafter" / "baselines" / "edd_factory2_v1"
OUT = ROOT / "outputs" / "crafter" / "baselines" / "spt_factory2_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
                            encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(directory):
    return {str(path.relative_to(ROOT)): sha(path)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def metric(metrics, key, default=None):
    return metrics.get(key, default)


def main():
    frozen_before = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT),
                     **hashes(FIFO), **hashes(EDD)}
    neutral = read(EVAL / "neutral_factory2_state.json")
    identity = read(EVAL / "neutral_factory2_identity.json")
    craft_metrics = read(EVAL / "craft_factory2_metrics_v2.json")
    fifo_metrics = read(FIFO / "fifo_metrics.json")
    edd_metrics = read(EDD / "edd_metrics.json")
    workload = build_workload_from_neutral_payload(neutral)

    durations = [row.get("serial_productive_seconds") for row in neutral["operations"]]
    duration_audit = {
        "operation_count": len(durations),
        "missing_duration_count": sum(value is None for value in durations),
        "negative_duration_count": sum(value is not None and int(value) < 0 for value in durations),
        "zero_duration_count": sum(value is not None and int(value) == 0 for value in durations),
        "minimum_seconds": min((int(value) for value in durations if value is not None), default=None),
        "maximum_seconds": max((int(value) for value in durations if value is not None), default=None),
        "mean_seconds": (statistics.mean(int(value) for value in durations if value is not None)
                          if any(value is not None for value in durations) else None),
        "median_seconds": (statistics.median(int(value) for value in durations if value is not None)
                            if any(value is not None for value in durations) else None),
    }
    invalid_duration = (duration_audit["missing_duration_count"] > 0 or
                        duration_audit["negative_duration_count"] > 0)
    write("spt_contract.json", {
        "contract": "SPT_FACTORY2_BASELINE_V1",
        "policy": {
            "priority": ["serial_productive_seconds", "queue_entry_time", "order_id",
                          "routing_sequence", "operation_key"],
            "duration_source": "neutral operation serial_productive_seconds",
            "duration_not_using": ["realized_elapsed", "calendar_expanded", "crew_duration",
                                    "machine_count_adjusted", "CRAFT duration"],
            "construction": "same event-driven parallel engine as FIFO and EDD",
            "resource_tie_break": ["earliest feasible start", "line_id", "machine_ids",
                                   "worker_ids"],
        },
        "shared_engine": "crafter.baselines.fifo_v1.run_fifo_schedule(policy=SPT)",
        "duration_audit": duration_audit,
        "neutral_identity": identity,
        "database_access": "READ_ONLY_ARTIFACTS_ONLY", "database_writes": 0,
        "craft_rerun": False, "fifo_rerun": False, "edd_rerun": False,
    })
    if invalid_duration:
        write("spt_diagnostics.json", {"status": "INVALID_INPUT_DURATION", "duration_audit": duration_audit,
                                       "database_writes": 0})
        print("STATUS = SPT_BASELINE_INVALID_INPUT")
        print("BLOCKERS = " + json.dumps(duration_audit, sort_keys=True))
        return

    result = run_spt_schedule(workload)
    write("spt_schedule.json", {"status": result["status"], "policy": result["policy"],
                                "schedule": result["schedule"],
                                "operation_count": len(result["schedule"]), "database_writes": 0})
    validation = validate_fifo_schedule(workload, result["schedule"])
    write("spt_validation.json", validation)
    write("spt_runtime.json", {
        "status": "VALID" if result["status"] == "COMPLETE" else "INCOMPLETE",
        "spt_scheduler_seconds": result["runtime_seconds"],
        "measurement": "run_spt_schedule wall clock only; excludes validation, KPIs, artifacts, and Codex session",
        "events_processed": result["events_processed"], "database_writes": 0,
    })
    write("spt_diagnostics.json", {
        "status": result["status"], "duration_audit": duration_audit,
        "unscheduled_operation_keys": result["unscheduled_operation_keys"],
        "failure": result["failure"], "allocation_profile": result["allocation_profile"],
        "events_processed": result["events_processed"], "event_log": result["event_log"],
        "neutral_identity_match": identity["identity_match"], "database_writes": 0,
    })

    comparable = False
    metrics = {"validation_status": "NOT_COMPARABLE",
               "reason": "SPT did not pass complete canonical validation",
               "operations_completed": len(result["schedule"]), "database_writes": 0}
    if result["status"] == "COMPLETE" and validation["accepted"]:
        metrics = evaluate_final_schedule(
            workload, result["schedule"], validation=validation,
            runtime_profile={"benchmark_wall_seconds": result["runtime_seconds"],
                             "window_total_seconds": result["runtime_seconds"]},
        )
        comparable = metrics.get("validation_status") == "VALID"
    write("spt_metrics.json", metrics)

    complete = len(result["schedule"]) == len(neutral["operations"])
    frozen_after = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT),
                    **hashes(FIFO), **hashes(EDD)}
    changed = sorted(name for name, digest in frozen_before.items()
                     if frozen_after.get(name) != digest)
    baseline_valid = bool(complete and validation["accepted"] and comparable and not changed)

    four_way = {"classification": "DESCRIPTIVE_NO_OVERALL_WINNER", "comparable": comparable,
                "relative_to_craft": {}, "metrics": {}}
    if comparable:
        sources = {"CRAFT": craft_metrics, "FIFO": fifo_metrics,
                   "EDD": edd_metrics, "SPT": metrics}
        for key in ("operations_completed", "makespan_seconds", "on_time_delivery_percentage",
                    "total_tardiness_seconds", "mean_tardiness_seconds",
                    "maximum_tardiness_seconds", "mean_flow_time_seconds"):
            four_way["metrics"][key] = {label: data[key] for label, data in sources.items()}
        for key in ("worker_utilization", "machine_utilization"):
            four_way["metrics"][key] = {label: data[key]["utilization"]
                                         for label, data in sources.items()}
        four_way["metrics"]["scheduler_runtime_seconds"] = {
            "CRAFT": craft_metrics["runtime"]["accepted_window_runtime_seconds"],
            "FIFO": read(FIFO / "fifo_runtime.json")["fifo_scheduler_seconds"],
            "EDD": read(EDD / "edd_runtime.json")["edd_scheduler_seconds"],
            "SPT": result["runtime_seconds"],
        }
        for key in ("makespan_seconds", "total_tardiness_seconds", "mean_flow_time_seconds",
                    "scheduler_runtime_seconds"):
            craft_value = four_way["metrics"][key]["CRAFT"]
            four_way["relative_to_craft"][key] = {
                label: ((value - craft_value) / craft_value * 100.0)
                for label, value in four_way["metrics"][key].items() if label != "CRAFT"
            }
    write("factory2_four_way_comparison.json", four_way)
    write("spt_report.json", {
        "status": "SPT_BASELINE_VALID" if baseline_valid else "SPT_BASELINE_INCOMPLETE",
        "spt_baseline_valid": baseline_valid,
        "factory2_evaluation_pipeline_valid": baseline_valid,
        "safe_to_freeze_factory2_evaluation_pipeline": baseline_valid,
        "safe_to_begin_other_factory_audit": baseline_valid,
        "neutral_operation_count": len(neutral["operations"]),
        "neutral_identity_match": identity["identity_match"],
        "operations_scheduled": len(result["schedule"]),
        "orders_completed": metrics.get("orders_completed") if comparable else None,
        "validation": {"scope": validation["scope_status"], "replay": validation["replay_status"],
                        "physical": validation["physical_status"], "accepted": validation["accepted"]},
        "runtime_seconds": result["runtime_seconds"], "frozen_artifacts_unchanged": not changed,
        "database_writes": 0, "blockers": changed,
    })
    md = f"""# SPT Factory-2 baseline V1

Status: `{'SPT_BASELINE_VALID' if baseline_valid else 'SPT_BASELINE_INCOMPLETE'}`

- Neutral workload: `{len(neutral['operations'])}` operations; identity match `{identity['identity_match']}`.
- Scheduled: `{len(result['schedule'])}`; orders completed: `{metrics.get('orders_completed', 'NOT_AVAILABLE')}`.
- Scope / Replay / Physical: `{validation['scope_status']}` / `{validation['replay_status']}` / `{validation['physical_status']}`.
- SPT runtime: `{result['runtime_seconds']:.6f}` seconds.
- Priority: intrinsic neutral `serial_productive_seconds`, then queue entry, order, sequence, key.
- Same common FIFO/EDD event/resource/replay-stability engine; no CP-SAT/CRAFT guidance.
- CRAFT/FIFO/EDD reruns: `false`; database writes: `0`; frozen artifacts unchanged: `{not changed}`.
"""
    if comparable:
        md += f"""
## SPT metrics

- Makespan: `{metrics['makespan_seconds']}` seconds.
- On time: `{metrics['on_time_orders']} / {metrics['orders_with_due_dates']}` (`{metrics['on_time_delivery_percentage']:.6f}%`).
- Total tardiness: `{metrics['total_tardiness_seconds']}` seconds.
- Mean tardiness: `{metrics['mean_tardiness_seconds']}` seconds.
- Maximum tardiness: `{metrics['maximum_tardiness_seconds']}` seconds.
- Mean flow time: `{metrics['mean_flow_time_seconds']}` seconds.
- Worker utilization: `{metrics['worker_utilization']['utilization']}`.
- Machine utilization: `{metrics['machine_utilization']['utilization']}`.
"""
    (OUT / "factory2_four_way_comparison.md").write_text(
        json.dumps(four_way, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (OUT / "spt_report.md").write_text(md, encoding="utf-8")

    primary = [p for p in OUT.iterdir() if p.is_file()
               and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"}]
    manifest = {p.name: sha(p) for p in sorted(primary)}
    write("artifact_hash_manifest.json", manifest)
    checks = {name: sha(OUT / name) == digest for name, digest in manifest.items()}
    write("hash_verification.json", {"verified": all(checks.values()), "files": checks,
                                     "database_writes": 0})

    def value(key, default="NOT_AVAILABLE"):
        return metrics.get(key, default)

    print("STATUS = " + ("SPT_BASELINE_VALID" if baseline_valid else "SPT_BASELINE_INCOMPLETE"))
    print("CRAFT_RERUN = no")
    print("FIFO_RERUN = no")
    print("EDD_RERUN = no")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    print(f"NEUTRAL_OPERATION_COUNT = {len(neutral['operations'])}")
    print(f"NEUTRAL_IDENTITY_MATCH = {str(identity['identity_match']).lower()}")
    print(f"SPT_OPERATIONS_SCHEDULED = {len(result['schedule'])}")
    print(f"SPT_ORDERS_COMPLETED = {value('orders_completed')}")
    print(f"SPT_COMPLETE = {str(complete).lower()}")
    print(f"SPT_REPLAY_STATUS = {validation['replay_status']}")
    print(f"SPT_PHYSICAL_STATUS = {validation['physical_status']}")
    print(f"SPT_SCOPE_STATUS = {validation['scope_status']}")
    print(f"SPT_VALIDATION_STATUS = {'VALID' if validation['accepted'] else 'INVALID'}")
    for label, key in (("SPT_MAKESPAN_SECONDS", "makespan_seconds"),
                       ("SPT_ON_TIME_ORDERS", "on_time_orders"),
                       ("SPT_LATE_ORDERS", "late_orders"),
                       ("SPT_ON_TIME_PERCENT", "on_time_delivery_percentage"),
                       ("SPT_TOTAL_TARDINESS_SECONDS", "total_tardiness_seconds"),
                       ("SPT_MEAN_TARDINESS_SECONDS", "mean_tardiness_seconds"),
                       ("SPT_MAX_TARDINESS_SECONDS", "maximum_tardiness_seconds"),
                       ("SPT_MEAN_FLOW_TIME_SECONDS", "mean_flow_time_seconds"),):
        print(f"{label} = {value(key)}")
    for label, key in (("SPT_WORKER_UTILIZATION", "worker_utilization"),
                       ("SPT_MACHINE_UTILIZATION", "machine_utilization")):
        item = value(key, {})
        print(f"{label} = {item.get('utilization', 'NOT_AVAILABLE') if isinstance(item, dict) else 'NOT_AVAILABLE'}")
    print(f"SPT_RUNTIME_SECONDS = {result['runtime_seconds']}")
    print("SPT_BASELINE_VALID = " + ("yes" if baseline_valid else "no"))
    print("FACTORY2_EVALUATION_PIPELINE_VALID = " + ("yes" if baseline_valid else "no"))
    print("SAFE_TO_FREEZE_FACTORY2_EVALUATION_PIPELINE = " + ("yes" if baseline_valid else "no"))
    print("SAFE_TO_BEGIN_OTHER_FACTORY_AUDIT = " + ("yes" if baseline_valid else "no"))
    print("BLOCKERS = " + json.dumps(changed, sort_keys=True))
    print("NEXT_STEP = Prepare neutral workload extraction/count audit for another factory; do not run it in this task." if baseline_valid else "NEXT_STEP = Diagnose SPT baseline failure; do not begin another factory.")


if __name__ == "__main__":
    main()
