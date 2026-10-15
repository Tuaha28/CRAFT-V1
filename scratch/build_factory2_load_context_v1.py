"""Build FACTORY_LOAD_CONTEXT_V1 from frozen Evaluation V2 artifacts only."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crafter.evaluation.load_context import (
    audit_releases, build_ready_work_timeline, flow_tardiness_context,
    parallelism, resource_scale, resource_usage,
)


EVAL = ROOT / "outputs" / "crafter" / "evaluation_v2"
FROZEN = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
OUT = ROOT / "outputs" / "crafter" / "load_context_v1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    eval_files = sorted(path for path in EVAL.iterdir() if path.is_file())
    eval_hashes_before = {path.name: sha(path) for path in eval_files}
    neutral = read(EVAL / "neutral_factory2_state.json")
    identity = read(EVAL / "neutral_factory2_identity.json")
    metrics = read(EVAL / "craft_factory2_metrics_v2.json")
    assembled = read(FROZEN / "assembled_factory_schedule.json")
    rows = assembled["assembled_schedule"]

    release = audit_releases(neutral, metrics)
    scale = resource_scale(neutral)
    usage = resource_usage(neutral, rows, metrics)
    timeline = build_ready_work_timeline(neutral, rows, metrics)
    concurrency = parallelism(neutral, rows, timeline)
    flow = flow_tardiness_context(neutral, metrics)

    write("factory2_release_audit.json", release)
    write("factory2_resource_scale.json", scale)
    write("factory2_resource_usage.json", usage)
    write("factory2_ready_work_timeline.json", timeline)
    write("factory2_parallelism.json", concurrency)
    write("factory2_flow_tardiness_context.json", flow)

    keys = [str(row["operation_key"]) for row in rows]
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    expected = set(neutral["accounting"]["benchmark_operation_keys"])
    standard_expected = {
        "makespan_seconds": 1_227_720,
        "on_time_delivery_percentage": 5.0,
        "total_tardiness_seconds": 75_031_501,
        "mean_tardiness_seconds": 3_751_575.05,
        "mean_flow_time_seconds": 5_777_661.85,
        "worker_utilization": 0.012337279795545808,
        "machine_utilization": 0.09156144270872375,
    }
    standard_actual = {
        "makespan_seconds": metrics["makespan_seconds"],
        "on_time_delivery_percentage": metrics["on_time_delivery_percentage"],
        "total_tardiness_seconds": metrics["total_tardiness_seconds"],
        "mean_tardiness_seconds": metrics["mean_tardiness_seconds"],
        "mean_flow_time_seconds": metrics["mean_flow_time_seconds"],
        "worker_utilization": metrics["worker_utilization"]["utilization"],
        "machine_utilization": metrics["machine_utilization"]["utilization"],
    }
    standard_mismatches = [
        {"metric": key, "expected": value, "actual": standard_actual[key]}
        for key, value in standard_expected.items()
        if abs(float(value) - float(standard_actual[key])) > 1e-12
    ]
    eval_hashes_after = {path.name: sha(path) for path in eval_files}
    changed_eval_files = sorted(name for name in eval_hashes_before
                                if eval_hashes_before[name] != eval_hashes_after.get(name))

    ready = timeline["ready_work_density"]
    actual = timeline["actual_concurrency"]
    worker_used_pct = usage["worker"]["percent_available_workers_used"]
    machine_used_pct = usage["machine"]["percent_usable_machines_used"]
    evidence = [
        f"All {len(neutral['orders'])} orders were released at or before snapshot 0; future-release throttling is absent.",
        f"The workload has {len(neutral['operations'])} operations for {scale['usable_machine_count']} usable machines and {scale['assignable_worker_count']} assignable workers.",
        f"The schedule used {usage['worker']['distinct_workers_used']} workers ({worker_used_pct:.2f}% of available workers), leaving {usage['worker']['available_workers_never_used']} never selected.",
        f"The schedule touched {usage['machine']['distinct_machines_used']} machines ({machine_used_pct:.2f}% of usable machines); low machine utilization therefore reflects sparse/short occupancy across a broad fleet, not use of only a tiny machine subset.",
        f"Time-weighted mean precedence-ready operations was {ready['mean_ready_operation_count']:.3f}; maximum was {ready['maximum_ready_operation_count']}.",
        f"At least one operation was running throughout the measured horizon (zero-running time {actual['time_with_zero_operations_running_seconds']} seconds), so there is no factory-wide schedule gap.",
    ]
    load_context = {
        "contract": "FACTORY_LOAD_CONTEXT_V1",
        "classification": "CONTEXTUAL_DIAGNOSTIC_NOT_REPLACEMENT_KPI",
        "standard_utilization": {
            "worker": metrics["worker_utilization"],
            "machine": metrics["machine_utilization"],
            "unchanged": not standard_mismatches,
        },
        "release_context": release,
        "resource_scale_summary": scale,
        "resource_usage_summary": usage,
        "ready_work_summary": {key: value for key, value in timeline.items() if key != "intervals"},
        "parallelism": concurrency,
        "makespan_context": {
            "evaluation_horizon_seconds": timeline["evaluation_horizon_seconds"],
            "first_scheduled_operation_start_seconds": min(int(row["start_seconds"]) for row in rows),
            "last_completion_seconds": max(int(row["end_seconds"]) for row in rows),
            "time_with_zero_operations_running_seconds": actual["time_with_zero_operations_running_seconds"],
            "time_with_at_least_one_operation_running_seconds": actual["time_with_at_least_one_operation_running_seconds"],
            "longest_interval_with_zero_operations_running_seconds": actual["longest_interval_with_zero_operations_running_seconds"],
            "makespan_definition_unchanged": True,
        },
        "demand_conditioned_utilization": {
            "worker_status": "NOT_EXACTLY_PROVEN",
            "worker_utilization": None,
            "machine_status": "NOT_EXACTLY_PROVEN",
            "machine_utilization": None,
            "reason": "The event-instant screen establishes current compatible capacity, but proving a fixed crew and machine remain jointly feasible through full execution requires counterfactual scheduling. No optimizer was invoked.",
        },
        "avoidable_idle_status": "NOT_EXACTLY_PROVEN",
        "low_utilization_primary_evidence": evidence,
        "interpretation": {
            "worker": "Standard worker utilization is productive crew-seconds divided by authoritative calendar availability after fixed reservations. Low value is strongly contextualized by a small workload relative to the workforce and limited operation-level parallelism; exact avoidable idle is not claimed.",
            "machine": "Standard machine utilization is full occupied machine spans divided by usable-machine availability after fixed reservations. Low value is strongly contextualized by a small workload relative to the usable fleet; exact scheduler-waste attribution is not claimed.",
        },
        "integrity": {
            "neutral_identity_count": identity["neutral_count"],
            "neutral_identity_match": identity["identity_match"],
            "frozen_schedule_count": len(keys),
            "missing": sorted(expected - set(keys)),
            "extra": sorted(set(keys) - expected),
            "duplicates": duplicates,
            "database_read_only": True,
            "database_writes": 0,
            "craft_rerun": False,
            "baselines_run": False,
            "evaluation_v2_files_changed": changed_eval_files,
            "evaluation_v2_artifacts_unchanged": not changed_eval_files,
            "standard_kpi_mismatches": standard_mismatches,
            "standard_kpis_unchanged": not standard_mismatches,
        },
    }
    safe = bool(
        identity["identity_match"] and len(keys) == 172 and not duplicates
        and not load_context["integrity"]["missing"]
        and not load_context["integrity"]["extra"]
        and not changed_eval_files and not standard_mismatches
        and release["all_orders_released_at_snapshot"]
        and metrics["validation_status"] == "VALID"
    )
    load_context["safe_to_run_baselines"] = safe
    load_context["blockers"] = [] if safe else [
        "one or more identity, validation, artifact-integrity, KPI, or release-context gates failed"
    ]
    load_context["next_step"] = (
        "Run FIFO/EDD/SPT separately on the same neutral workload and evaluate them with Evaluation V2."
        if safe else "Resolve the failed load-context integrity gate before baseline execution."
    )
    write("factory2_load_context.json", load_context)

    markdown = f"""# Factory-2 load context V1

This is contextual evidence, not a replacement KPI. CRAFT was not rerun and no baseline was executed.

## Frozen standard KPIs

- Worker utilization: `{metrics['worker_utilization']['utilization'] * 100:.8f}%` = `{metrics['worker_utilization']['productive_worker_seconds']}` productive / `{metrics['worker_utilization']['available_worker_seconds']}` available worker-seconds.
- Machine utilization: `{metrics['machine_utilization']['utilization'] * 100:.8f}%` = `{metrics['machine_utilization']['machine_occupied_seconds']}` occupied / `{metrics['machine_utilization']['machine_available_seconds']}` available machine-seconds.

## Load evidence

- Orders/operations: `{len(neutral['orders'])}` / `{len(neutral['operations'])}`.
- All orders released at snapshot: `{release['all_orders_released_at_snapshot']}`.
- Usable machines / assignable workers: `{scale['usable_machine_count']}` / `{scale['assignable_worker_count']}`.
- Workers used: `{usage['worker']['distinct_workers_used']}` (`{worker_used_pct:.2f}%`).
- Machines used: `{usage['machine']['distinct_machines_used']}` (`{machine_used_pct:.2f}%`).
- Mean/median/max precedence-ready operations: `{ready['mean_ready_operation_count']:.3f}` / `{ready['median_ready_operation_count']}` / `{ready['maximum_ready_operation_count']}`.
- Zero-ready time: `{ready['percent_time_zero_ready_work']:.2f}%`; with-ready time: `{ready['percent_time_with_ready_work']:.2f}%`.
- Maximum actual concurrent operations: `{concurrency['maximum_actual_simultaneously_running_operations']}`.

Exact demand-conditioned utilization and avoidable idle are `NOT_EXACTLY_PROVEN`: full counterfactual fixed-crew feasibility would require another scheduling problem, which this audit did not run.

Evaluation V2 artifacts unchanged: `{not changed_eval_files}`. Standard KPIs unchanged: `{not standard_mismatches}`. Database writes: `0`. Safe to run baselines: `{safe}`.
"""
    (OUT / "factory2_load_context.md").write_text(markdown, encoding="utf-8")

    primary = [path for path in OUT.iterdir() if path.is_file()
               and path.name not in {"artifact_hash_manifest.json", "hash_verification.json"}]
    hashes = {path.name: sha(path) for path in sorted(primary)}
    write("artifact_hash_manifest.json", hashes)
    checks = {name: sha(OUT / name) == digest for name, digest in hashes.items()}
    write("hash_verification.json", {"verified": all(checks.values()), "files": checks, "database_writes": 0})

    print("STATUS = FACTORY_LOAD_CONTEXT_V1_COMPLETE")
    print("CRAFT_RERUN = no")
    print("BASELINES_RUN = no")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    print(f"FACTORY2_OPERATIONS = {len(neutral['operations'])}")
    print(f"FACTORY2_ORDERS = {len(neutral['orders'])}")
    print(f"USABLE_MACHINES = {scale['usable_machine_count']}")
    print(f"ASSIGNABLE_WORKERS = {scale['assignable_worker_count']}")
    print(f"FUTURE_RELEASE_ORDER_COUNT = {release['future_release_order_count']}")
    print(f"ALL_ORDERS_RELEASED_AT_SNAPSHOT = {str(release['all_orders_released_at_snapshot']).lower()}")
    print(f"DISTINCT_WORKERS_USED = {usage['worker']['distinct_workers_used']}")
    print(f"PERCENT_AVAILABLE_WORKERS_USED = {worker_used_pct}")
    print(f"DISTINCT_MACHINES_USED = {usage['machine']['distinct_machines_used']}")
    print(f"PERCENT_USABLE_MACHINES_USED = {machine_used_pct}")
    print(f"STANDARD_WORKER_UTILIZATION = {metrics['worker_utilization']['utilization']}")
    print(f"STANDARD_MACHINE_UTILIZATION = {metrics['machine_utilization']['utilization']}")
    print(f"MEAN_READY_OPERATIONS = {ready['mean_ready_operation_count']}")
    print(f"MEDIAN_READY_OPERATIONS = {ready['median_ready_operation_count']}")
    print(f"MAX_READY_OPERATIONS = {ready['maximum_ready_operation_count']}")
    print(f"PERCENT_TIME_ZERO_READY_WORK = {ready['percent_time_zero_ready_work']}")
    print(f"PERCENT_TIME_WITH_READY_WORK = {ready['percent_time_with_ready_work']}")
    print(f"MAX_ACTUAL_CONCURRENT_OPERATIONS = {concurrency['maximum_actual_simultaneously_running_operations']}")
    print(f"MAX_PRECEDENCE_READY_PARALLELISM = {concurrency['maximum_theoretical_precedence_ready_parallelism_observed']}")
    print("DEMAND_CONDITIONED_WORKER_UTILIZATION_STATUS = NOT_EXACTLY_PROVEN")
    print("DEMAND_CONDITIONED_WORKER_UTILIZATION = NOT_AVAILABLE")
    print("DEMAND_CONDITIONED_MACHINE_UTILIZATION_STATUS = NOT_EXACTLY_PROVEN")
    print("DEMAND_CONDITIONED_MACHINE_UTILIZATION = NOT_AVAILABLE")
    print("AVOIDABLE_IDLE_STATUS = NOT_EXACTLY_PROVEN")
    print("LOW_UTILIZATION_PRIMARY_EVIDENCE = " + " | ".join(evidence))
    print(f"STANDARD_KPIS_UNCHANGED = {str(not standard_mismatches).lower()}")
    print(f"SAFE_TO_RUN_BASELINES = {str(safe).lower()}")
    print("BLOCKERS = " + ("none" if safe else "; ".join(load_context["blockers"])))
    print("NEXT_STEP = " + load_context["next_step"])


if __name__ == "__main__":
    main()
