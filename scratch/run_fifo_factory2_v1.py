"""Run the one neutral Factory-2 FIFO baseline and emit evaluation evidence."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crafter.baselines.fifo_v1 import (
    build_workload_from_neutral_payload,
    run_fifo_schedule,
    validate_fifo_schedule,
)
from crafter.evaluation.final_kpis import EvaluationError, evaluate_final_schedule


EVAL = ROOT / "outputs" / "crafter" / "evaluation_v2"
LOAD = ROOT / "outputs" / "crafter" / "load_context_v1"
CRAFT = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
OUT = ROOT / "outputs" / "crafter" / "baselines" / "fifo_factory2_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(directory):
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def main():
    frozen_before = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT)}
    neutral = read(EVAL / "neutral_factory2_state.json")
    identity = read(EVAL / "neutral_factory2_identity.json")
    craft_metrics = read(EVAL / "craft_factory2_metrics_v2.json")
    workload = build_workload_from_neutral_payload(neutral)

    contract = {
        "contract": "FIFO_FACTORY2_BASELINE_V1",
        "policy": {
            "ready_queue": "queue entry = release for first operation; max(release, FIFO predecessor completion) for successors",
            "priority": ["queue_entry_time", "order_id", "routing_sequence", "operation_key"],
            "construction": "event-driven list scheduling; all compatible operations may start at one event",
            "resource_tie_break": ["earliest feasible start", "line_id", "machine_ids", "worker_ids"],
        },
        "policy_neutral_primitives_reused": {
            "utils.shift_rotation.batch_available_at": "authoritative rotating crew calendar",
            "utils.work_calendar.productive_segments": "authoritative pause/resume execution walk",
            "crafter.lns_probe.joint_modes._fixed_crew_execution": "fixed crew plus reservation-aware productive segments",
            "crafter.authoritative_semantics.authoritative_changeover_seconds": "machine sequence gap",
            "validation.schedule_validator.validate_exported_schedule": "independent physical validation",
            "crafter.repair.scoped_validation.replay_assembled_phenotype": "exact assembled semantic replay",
            "crafter.evaluation.final_kpis.evaluate_final_schedule": "unchanged Evaluation V2 KPI contract",
            "neutral_factory2_state.json": "policy-independent source workload and resources",
        },
        "craft_policy_not_reused": [
            "neighborhood/affected-region selection", "Step3A", "Step3B", "CP-SAT",
            "CRAFT objectives", "rolling repair", "accepted schedule guidance",
            "GNN", "GA", "CRAFT priority scores",
        ],
        "resource_domain": {
            "machines": "all eligible usable machines on every eligible line are examined; first legal k per deterministic tie",
            "workers": "all eligible workers grouped by authoritative calendar; exact/compatible fixed crews constructed without combinations",
            "arbitrary_machine_cap": None, "arbitrary_worker_cap": None,
            "worker_combinations_enumerated": 0,
        },
        "neutral_identity": identity,
        "database_access": "READ_ONLY_ARTIFACTS_ONLY", "database_writes": 0,
        "craft_rerun": False, "edd_run": False, "spt_run": False,
    }
    write("fifo_contract.json", contract)

    result = run_fifo_schedule(workload)
    write("fifo_schedule.json", {
        "status": result["status"], "schedule": result["schedule"],
        "operation_count": len(result["schedule"]), "database_writes": 0,
    })
    validation = validate_fifo_schedule(workload, result["schedule"])
    write("fifo_validation.json", validation)
    runtime = {
        "status": "VALID", "fifo_scheduler_seconds": result["runtime_seconds"],
        "measurement": "run_fifo_schedule wall clock only; excludes validation, KPIs, artifacts, and Codex session",
        "events_processed": result["events_processed"], "database_writes": 0,
    }
    write("fifo_runtime.json", runtime)
    diagnostics = {
        "status": result["status"], "unscheduled_operation_keys": result["unscheduled_operation_keys"],
        "failure": result["failure"], "allocation_profile": result["allocation_profile"],
        "events_processed": result["events_processed"], "event_log": result["event_log"],
        "neutral_identity_match": identity["identity_match"], "database_writes": 0,
    }
    write("fifo_diagnostics.json", diagnostics)

    metrics = {
        "validation_status": "NOT_COMPARABLE",
        "reason": "FIFO did not pass complete canonical validation",
        "operations_completed": len(result["schedule"]),
        "database_writes": 0,
    }
    if result["status"] == "COMPLETE" and validation["accepted"]:
        metrics = evaluate_final_schedule(
            workload, result["schedule"], validation=validation,
            runtime_profile={
                "benchmark_wall_seconds": result["runtime_seconds"],
                "window_total_seconds": result["runtime_seconds"],
            },
        )
    write("fifo_metrics.json", metrics)

    comparable = metrics.get("validation_status") == "VALID"
    comparison = {
        "classification": "DESCRIPTIVE_NO_OVERALL_WINNER",
        "comparable": comparable,
        "metrics": {},
    }
    mapping = {
        "operations_completed": "operations_completed",
        "makespan_seconds": "makespan_seconds",
        "on_time_delivery_percentage": "on_time_delivery_percentage",
        "total_tardiness_seconds": "total_tardiness_seconds",
        "mean_tardiness_seconds": "mean_tardiness_seconds",
        "mean_flow_time_seconds": "mean_flow_time_seconds",
    }
    if comparable:
        for label, key in mapping.items():
            comparison["metrics"][label] = {"FIFO": metrics[key], "CRAFT": craft_metrics[key]}
        comparison["metrics"]["worker_utilization"] = {
            "FIFO": metrics["worker_utilization"]["utilization"],
            "CRAFT": craft_metrics["worker_utilization"]["utilization"],
        }
        comparison["metrics"]["machine_utilization"] = {
            "FIFO": metrics["machine_utilization"]["utilization"],
            "CRAFT": craft_metrics["machine_utilization"]["utilization"],
        }
        comparison["metrics"]["scheduler_runtime_seconds"] = {
            "FIFO": result["runtime_seconds"],
            "CRAFT": craft_metrics["runtime"]["accepted_window_runtime_seconds"],
        }
    write("fifo_vs_craft_descriptive.json", comparison)

    frozen_after = {**hashes(EVAL), **hashes(LOAD), **hashes(CRAFT)}
    changed_source_artifacts = sorted(
        name for name, digest in frozen_before.items() if frozen_after.get(name) != digest)
    complete = len(result["schedule"]) == 172 and not result["unscheduled_operation_keys"]
    baseline_valid = bool(complete and validation["accepted"] and comparable)
    status = (
        "FIFO_BASELINE_VALID" if baseline_valid else
        "FIFO_BASELINE_INCOMPLETE" if not complete else "FIFO_BASELINE_VALIDATION_FAILED"
    )
    blockers = []
    if result["failure"]:
        blockers.append(result["failure"])
    if not validation["accepted"]:
        blockers.append({
            "classification": "VALIDATION_FAILED", "scope": validation["scope_status"],
            "replay": validation["replay_status"], "physical": validation["physical_status"],
        })
    if changed_source_artifacts:
        blockers.append({"classification": "FROZEN_ARTIFACT_MUTATION", "files": changed_source_artifacts})
        baseline_valid = False
        status = "FIFO_BASELINE_VALIDATION_FAILED"

    report = {
        "status": status, "fifo_baseline_valid": baseline_valid,
        "safe_to_reuse_baseline_engine_for_edd_spt": baseline_valid,
        "neutral_operation_count": len(neutral["operations"]),
        "neutral_identity_match": identity["identity_match"],
        "operations_scheduled": len(result["schedule"]),
        "orders_completed": metrics.get("orders_completed") if comparable else None,
        "validation": {
            "scope": validation["scope_status"], "replay": validation["replay_status"],
            "physical": validation["physical_status"], "accepted": validation["accepted"],
        },
        "runtime_seconds": result["runtime_seconds"], "blockers": blockers,
        "frozen_source_artifacts_unchanged": not changed_source_artifacts,
        "craft_rerun": False, "edd_run": False, "spt_run": False,
        "database_read_only": True, "database_writes": 0,
    }
    write("fifo_report.json", report)
    md = f"""# FIFO Factory-2 baseline V1

Status: `{status}`

- Neutral workload: `{len(neutral['operations'])}` operations; identity match `{identity['identity_match']}`.
- Scheduled: `{len(result['schedule'])}` operations.
- Scope / Replay / Physical: `{validation['scope_status']}` / `{validation['replay_status']}` / `{validation['physical_status']}`.
- FIFO scheduler runtime: `{result['runtime_seconds']:.6f}` seconds.
- Worker combinations enumerated: `{result['allocation_profile'].get('worker_combinations_enumerated', 0)}`.
- CRAFT rerun: `false`; EDD run: `false`; SPT run: `false`; database writes: `0`.
- Frozen Evaluation V2, load-context, and CRAFT artifacts unchanged: `{not changed_source_artifacts}`.

This baseline changes only ready-queue priority. It uses event-driven parallel dispatch and the same authoritative calendar/segment, commitment, replay, validation, and Evaluation V2 KPI semantics. The comparison is descriptive and declares no overall winner.
"""
    if comparable:
        md += f"""
## FIFO metrics

- Makespan: `{metrics['makespan_seconds']}` seconds.
- On time: `{metrics['on_time_orders']} / {metrics['orders_with_due_dates']}` (`{metrics['on_time_delivery_percentage']:.6f}%`).
- Total tardiness: `{metrics['total_tardiness_seconds']}` seconds.
- Mean flow time: `{metrics['mean_flow_time_seconds']}` seconds.
- Worker utilization: `{metrics['worker_utilization']['utilization']}`.
- Machine utilization: `{metrics['machine_utilization']['utilization']}`.
"""
    (OUT / "fifo_report.md").write_text(md, encoding="utf-8")

    primary = [path for path in OUT.iterdir() if path.is_file()
               and path.name not in {"artifact_hash_manifest.json", "hash_verification.json"}]
    manifest = {path.name: sha(path) for path in sorted(primary)}
    write("artifact_hash_manifest.json", manifest)
    checks = {name: sha(OUT / name) == digest for name, digest in manifest.items()}
    write("hash_verification.json", {
        "verified": all(checks.values()), "files": checks, "database_writes": 0,
    })

    def value(path, default="NOT_AVAILABLE"):
        cursor = metrics
        for key in path:
            if not isinstance(cursor, dict) or key not in cursor:
                return default
            cursor = cursor[key]
        return cursor

    print(f"STATUS = {status}")
    print("CRAFT_RERUN = no")
    print("EDD_RUN = no")
    print("SPT_RUN = no")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    print(f"NEUTRAL_OPERATION_COUNT = {len(neutral['operations'])}")
    print(f"NEUTRAL_IDENTITY_MATCH = {str(identity['identity_match']).lower()}")
    print(f"FIFO_OPERATIONS_SCHEDULED = {len(result['schedule'])}")
    print(f"FIFO_ORDERS_COMPLETED = {value(('orders_completed',))}")
    print(f"FIFO_COMPLETE = {str(complete).lower()}")
    print(f"FIFO_REPLAY_STATUS = {validation['replay_status']}")
    print(f"FIFO_PHYSICAL_STATUS = {validation['physical_status']}")
    print(f"FIFO_SCOPE_STATUS = {validation['scope_status']}")
    print(f"FIFO_VALIDATION_STATUS = {'VALID' if validation['accepted'] else 'INVALID'}")
    print(f"FIFO_MAKESPAN_SECONDS = {value(('makespan_seconds',))}")
    print(f"FIFO_ON_TIME_ORDERS = {value(('on_time_orders',))}")
    print(f"FIFO_LATE_ORDERS = {value(('late_orders',))}")
    print(f"FIFO_ON_TIME_PERCENT = {value(('on_time_delivery_percentage',))}")
    print(f"FIFO_TOTAL_TARDINESS_SECONDS = {value(('total_tardiness_seconds',))}")
    print(f"FIFO_MEAN_TARDINESS_SECONDS = {value(('mean_tardiness_seconds',))}")
    print(f"FIFO_MAX_TARDINESS_SECONDS = {value(('maximum_tardiness_seconds',))}")
    print(f"FIFO_MEAN_FLOW_TIME_SECONDS = {value(('mean_flow_time_seconds',))}")
    print(f"FIFO_WORKER_UTILIZATION = {value(('worker_utilization', 'utilization'))}")
    print(f"FIFO_MACHINE_UTILIZATION = {value(('machine_utilization', 'utilization'))}")
    print(f"FIFO_RUNTIME_SECONDS = {result['runtime_seconds']}")
    print(f"CRAFT_MAKESPAN_SECONDS = {craft_metrics['makespan_seconds']}")
    print(f"CRAFT_ON_TIME_PERCENT = {craft_metrics['on_time_delivery_percentage']}")
    print(f"CRAFT_TOTAL_TARDINESS_SECONDS = {craft_metrics['total_tardiness_seconds']}")
    print(f"CRAFT_MEAN_FLOW_TIME_SECONDS = {craft_metrics['mean_flow_time_seconds']}")
    print(f"CRAFT_WORKER_UTILIZATION = {craft_metrics['worker_utilization']['utilization']}")
    print(f"CRAFT_MACHINE_UTILIZATION = {craft_metrics['machine_utilization']['utilization']}")
    print(f"CRAFT_RUNTIME_SECONDS = {craft_metrics['runtime']['accepted_window_runtime_seconds']}")
    print(f"FIFO_BASELINE_VALID = {str(baseline_valid).lower()}")
    print(f"SAFE_TO_REUSE_BASELINE_ENGINE_FOR_EDD_SPT = {str(baseline_valid).lower()}")
    print("BLOCKERS = " + ("none" if not blockers else json.dumps(blockers, sort_keys=True)))
    print("NEXT_STEP = " + (
        "Reuse this validated neutral engine with an independently specified EDD priority rule."
        if baseline_valid else "Diagnose the recorded FIFO blocker before implementing another baseline."
    ))


if __name__ == "__main__":
    main()
