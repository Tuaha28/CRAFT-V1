"""Build the neutral Factory-2 state and evaluate the frozen CRAFT result.

No scheduling or baseline policy is executed.  The source database is read
only through the frozen benchmark loader; outputs are JSON/Markdown evidence.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crafter.disruptions.benchmark import _load_state
from crafter.evaluation import extract_neutral_factory_workload, evaluate_final_schedule


OUT = ROOT / "outputs" / "crafter" / "evaluation_v2"
FROZEN = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
OLD = ROOT / "outputs" / "crafter" / "final_evaluation" / "craft_v1_frozen_report.json"
AUDIT = ROOT / "outputs" / "crafter" / "evaluation_audit" / "workload_audit.json"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha_keys(keys):
    return hashlib.sha256("\n".join(sorted(str(x) for x in keys)).encode("utf-8")).hexdigest()


def main():
    loaded = _load_state()
    workload = extract_neutral_factory_workload(
        loaded, factory_id=2, snapshot_as_of_seconds=0,
        authoritative_execution_facts={},
    )
    neutral = workload.as_dict()
    frozen_accounting = read(FROZEN / "factory_accounting.json")
    expected_keys = list(frozen_accounting["schedulable_operations"])
    actual_keys = list(neutral["accounting"]["benchmark_operation_keys"])
    identity = {
        "neutral_count": len(actual_keys),
        "authoritative_count": len(expected_keys),
        "missing": sorted(set(expected_keys) - set(actual_keys)),
        "extra": sorted(set(actual_keys) - set(expected_keys)),
        "duplicates": neutral["accounting"]["duplicates"],
        "neutral_sha256": sha_keys(actual_keys),
        "authoritative_sha256": sha_keys(expected_keys),
    }
    identity["identity_match"] = (
        identity["neutral_count"] == identity["authoritative_count"] == 172
        and not identity["missing"] and not identity["extra"]
        and not identity["duplicates"]
        and identity["neutral_sha256"] == identity["authoritative_sha256"]
    )
    write("neutral_factory2_state.json", neutral)
    write("neutral_factory2_identity.json", identity)

    assembled = read(FROZEN / "assembled_factory_schedule.json")
    validation = read(FROZEN / "assembled_factory_validation.json")
    runtime = read(FROZEN / "runtime_profile.json")
    metrics = evaluate_final_schedule(
        workload, assembled["assembled_schedule"],
        validation=validation, runtime_profile=runtime,
    )
    old = read(OLD).get("metrics", {}) if OLD.exists() else {}
    comparisons = {
        "makespan_seconds": {
            "old": old.get("makespan_seconds"), "new": metrics["makespan_seconds"]},
        "total_tardiness_seconds": {
            "old": old.get("total_tardiness_seconds"), "new": metrics["total_tardiness_seconds"]},
        "mean_tardiness_seconds": {
            "old": old.get("mean_tardiness_seconds"), "new": metrics["mean_tardiness_seconds"]},
        "maximum_tardiness_seconds": {
            "old": old.get("maximum_tardiness_seconds"), "new": metrics["maximum_tardiness_seconds"]},
        "on_time_delivery_percentage": {
            "old": old.get("on_time_percentage"), "new": metrics["on_time_delivery_percentage"]},
    }
    mismatches = []
    for name, values in comparisons.items():
        if values["old"] is None:
            continue
        if abs(float(values["old"]) - float(values["new"])) > 1e-9:
            mismatches.append({"metric": name, **values})
    metrics["comparison_with_old_report"] = {
        "metrics": comparisons,
        "mismatches": mismatches,
        "status": "MATCH" if not mismatches else "MISMATCH",
    }
    write("craft_factory2_metrics_v2.json", metrics)

    availability = {
        "worker": metrics["worker_utilization"],
        "machine": metrics["machine_utilization"],
        "authoritative_calendar_observation": (
            "The frozen source does not use a universal 07:00-16:00/Friday-off calendar. "
            "It uses three rotating eight-hour crew bands with staggered rest days; "
            "floaters use fixed Work_Schedules shifts. Usable machines are modeled "
            "continuously available absent fixed commitments."
        ),
        "database_writes": 0,
    }
    write("availability_audit.json", availability)

    contract = {
        "contract_version": "FINAL_SCHEDULE_KPI_V1",
        "input": "neutral factory workload + final valid assembled schedule",
        "validation_gates": [
            "complete operation identity", "no duplicates/missing/unexpected",
            "release and precedence valid", "Replay PASS", "Physical VALID", "Scope SCOPE_VALID",
            "zero authoritative violation counts",
        ],
        "definitions": {
            "order_completion": "end of final routing operation",
            "makespan": "absolute_last_completion_seconds - evaluation_origin_seconds",
            "tardiness": "max(0, order_completion - order_due_date)",
            "on_time_percent": "on_time_orders / orders_with_due_dates * 100",
            "flow_time": "order_completion - order_release",
            "worker_utilization": "productive fixed-crew seconds / authoritative roster availability after fixed reservations",
            "machine_utilization": "full machine occupied spans / authoritative usable-machine availability after fixed reservations",
        },
        "time_basis": {
            "unit": "seconds",
            "origin": neutral["evaluation_origin"],
            "snapshot_as_of_seconds": neutral["snapshot_as_of_seconds"],
        },
        "database_writes": 0,
    }
    write("kpi_contract.json", contract)
    contract_md = f"""# Final KPI contract

All policies are evaluated by one implementation using the neutral factory state. Complete KPIs are emitted only after exact identity, release, precedence, Replay, Physical, and Scope gates pass.

- Time unit: seconds relative to `{neutral['evaluation_origin']}`.
- Makespan: last completion minus evaluation origin seconds (`0`).
- Order completion: final routing operation end.
- Tardiness: `max(0, completion - due)` at order level.
- Flow time: `completion - release` at order level.
- Worker utilization: productive crew-seconds divided by exact rotating/fixed-shift availability after fixed reservations.
- Machine utilization: full occupied machine spans divided by usable continuous-machine availability after fixed reservations.

Database writes: `0`.
"""
    (OUT / "kpi_contract.md").write_text(contract_md, encoding="utf-8")

    summary_metrics = {key: value for key, value in metrics.items()
                       if key not in {"worker_utilization", "machine_utilization"}}
    summary_metrics["worker_utilization"] = {
        key: value for key, value in metrics["worker_utilization"].items() if key != "per_worker"}
    summary_metrics["machine_utilization"] = {
        key: value for key, value in metrics["machine_utilization"].items() if key != "per_machine"}
    report = {
        "status": "EVALUATION_V2_COMPLETE" if identity["identity_match"] and metrics["validation_status"] == "VALID" else "EVALUATION_V2_BLOCKED",
        "neutral_identity": identity,
        "metrics": summary_metrics,
        "database_read_only": True,
        "database_writes": 0,
        "craft_rerun": False,
        "safe_to_run_baselines": bool(
            identity["identity_match"]
            and metrics["validation_status"] == "VALID"
            and not mismatches
            and metrics["worker_utilization"]["status"] == "VALID"
            and metrics["machine_utilization"]["status"] == "VALID"
        ),
        "blockers": [],
    }
    if mismatches:
        report["blockers"].append("old/new makespan, tardiness, or on-time metric mismatch")
    if metrics["worker_utilization"]["status"] != "VALID":
        report["blockers"].append("worker availability denominator not proven")
    if metrics["machine_utilization"]["status"] != "VALID":
        report["blockers"].append("machine availability denominator not proven")
    write("evaluation_v2_report.json", report)

    metric_md = f"""# Frozen Factory-2 metrics (Evaluation V2)

- Operations completed: {metrics['operations_completed']}
- Orders completed: {metrics['orders_completed']}
- Makespan: {metrics['makespan_seconds']} seconds
- On time: {metrics['on_time_orders']} / {metrics['orders_with_due_dates']} ({metrics['on_time_delivery_percentage']:.6f}%)
- Total tardiness: {metrics['total_tardiness_seconds']} seconds
- Mean tardiness: {metrics['mean_tardiness_seconds']} seconds
- Maximum tardiness: {metrics['maximum_tardiness_seconds']} seconds
- Mean flow time: {metrics['mean_flow_time_seconds']} seconds
- Worker utilization: {metrics['worker_utilization']['utilization']} ({metrics['worker_utilization']['status']})
- Machine utilization: {metrics['machine_utilization']['utilization']} ({metrics['machine_utilization']['status']})
- Accepted-window runtime: {metrics['runtime']['accepted_window_runtime_seconds']} seconds
- Old-report comparison: {metrics['comparison_with_old_report']['status']}
- Database writes: 0
"""
    (OUT / "craft_factory2_metrics_v2.md").write_text(metric_md, encoding="utf-8")
    report_md = f"""# Evaluation V2 report

Status: `{report['status']}`

Neutral Factory-2 identity: `{identity['identity_match']}` ({identity['neutral_count']} operations, SHA-256 `{identity['neutral_sha256']}`).

Frozen CRAFT schedule validation: `{metrics['validation_status']}`. CRAFT was not rerun. Worker utilization status: `{metrics['worker_utilization']['status']}`. Machine utilization status: `{metrics['machine_utilization']['status']}`. Old makespan/tardiness/on-time comparison: `{metrics['comparison_with_old_report']['status']}`.

Safe to run baselines: `{report['safe_to_run_baselines']}`. Database writes: `0`.
"""
    (OUT / "evaluation_v2_report.md").write_text(report_md, encoding="utf-8")

    primary = [path for path in OUT.iterdir() if path.is_file()
               and path.name not in {"artifact_hash_manifest.json", "hash_verification.json"}]
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(primary)}
    write("artifact_hash_manifest.json", hashes)
    checks = {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() == digest
              for name, digest in hashes.items()}
    write("hash_verification.json", {
        "verified": all(checks.values()), "files": checks, "database_writes": 0})
    print(json.dumps({
        "status": report["status"], "identity": identity["identity_match"],
        "metrics_status": metrics["validation_status"],
        "worker_utilization_status": metrics["worker_utilization"]["status"],
        "machine_utilization_status": metrics["machine_utilization"]["status"],
        "mismatches": mismatches,
    }, indent=2))


if __name__ == "__main__":
    main()
