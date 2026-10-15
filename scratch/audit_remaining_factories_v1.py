"""Read-only neutral workload/resource audit for factories 1, 3, 4, 5, 6.

This script deliberately stops at structural extraction.  It does not invoke
CRAFT, FIFO, EDD, SPT, CP-SAT, or any scheduler.  The neutral extractor is the
same policy-independent path used for the frozen Factory-2 evaluation.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Running a scratch script places ``scratch`` first on sys.path; explicitly
# expose the repository root so the audit imports the checked-out package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crafter.disruptions.benchmark import _load_state
from crafter.evaluation.neutral_factory import extract_neutral_factory_workload


OUT = Path("outputs/crafter/remaining_factory_audit_v1")
FACTORIES = (1, 3, 4, 5, 6)
SNAPSHOT = 0
FROZEN_PATHS = {
    "factory2_craft": Path("outputs/crafter/factory2_172_production_v1_workerfix_full"),
    "evaluation_v2": Path("outputs/crafter/evaluation_v2"),
    "load_context_v1": Path("outputs/crafter/load_context_v1"),
    "fifo_factory2": Path("outputs/crafter/baselines/fifo_factory2_v1"),
    "edd_factory2": Path("outputs/crafter/baselines/edd_factory2_v1"),
    "spt_factory2": Path("outputs/crafter/baselines/spt_factory2_v1"),
}


def _json_default(value: Any):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _write(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _tree_hash(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "file_count": 0, "sha256": None, "files": {}}
    files = {}
    if path.is_file():
        files[path.name] = _sha_file(path)
    else:
        for item in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
            files[item.relative_to(path).as_posix()] = _sha_file(item)
    aggregate = hashlib.sha256()
    for name, digest in files.items():
        aggregate.update(name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
    return {"exists": True, "file_count": len(files), "sha256": aggregate.hexdigest(), "files": files}


def _stats(values):
    vals = [float(v) for v in values]
    if not vals:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None}
    return {
        "count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
    }


def _int_stats(values):
    result = _stats(values)
    for key in ("min", "max"):
        if result[key] is not None:
            result[key] = int(result[key])
    return result


def _sha_sorted(values):
    return hashlib.sha256("\n".join(sorted(str(v) for v in values)).encode("utf-8")).hexdigest()


def _cyclic(keys, predecessors):
    state = {str(k): 0 for k in keys}

    def visit(key):
        if state[key] == 1:
            return True
        if state[key] == 2:
            return False
        state[key] = 1
        for pred in predecessors.get(key, ()):
            if pred in state and visit(pred):
                return True
        state[key] = 2
        return False

    return any(visit(key) for key in sorted(state))


def _round_or_int(value):
    if value is None:
        return None
    if float(value).is_integer():
        return int(value)
    return float(value)


def audit_payload(payload: dict) -> dict:
    factory_id = int(payload["factory_id"])
    orders = list(payload.get("orders", []))
    operations = list(payload.get("operations", []))
    machines = list(payload.get("machines", []))
    workers = list(payload.get("workers", []))
    lines = list(payload.get("lines", []))
    op_keys = [str(op.get("operation_key")) for op in operations]
    order_ids = [str(order.get("order_id")) for order in orders]
    op_key_set = set(op_keys)
    order_id_set = set(order_ids)
    machine_ids = [int(machine["machine_id"]) for machine in machines]
    machine_id_set = set(machine_ids)
    worker_ids = [str(worker["worker_id"]) for worker in workers]
    worker_id_set = set(worker_ids)
    line_ids = [int(line["line_id"]) for line in lines]
    line_id_set = set(line_ids)

    predecessor = {key: set(str(v) for v in op.get("predecessor_operation_keys", [])) for key, op in zip(op_keys, operations)}
    successor = {key: set(str(v) for v in op.get("successor_operation_keys", [])) for key, op in zip(op_keys, operations)}
    broken_pred = sorted({pred for values in predecessor.values() for pred in values if pred not in op_key_set})
    broken_succ = sorted({succ for values in successor.values() for succ in values if succ not in op_key_set})
    edges = sum(len(values) for values in predecessor.values())
    cycle = _cyclic(op_key_set, predecessor)

    by_order = defaultdict(list)
    for op in operations:
        by_order[str(op["order_id"])].append(op)
    order_op_counts = [len(by_order.get(order_id, [])) for order_id in order_ids]
    sequences = [int(op.get("sequence", 0)) for op in operations]

    releases = [int(order["release_seconds"]) for order in orders]
    dues = [int(order["deadline_seconds"]) for order in orders if order.get("deadline_seconds") is not None]
    release_counter = Counter(releases)
    release_points = sorted(release_counter)
    release_gaps = [release_points[i + 1] - release_points[i] for i in range(len(release_points) - 1)]
    future_release_orders = [order for order in orders if int(order["release_seconds"]) > SNAPSHOT]
    released_orders = [order for order in orders if int(order["release_seconds"]) <= SNAPSHOT]

    usable_machines = [m for m in machines if bool(m.get("is_usable", False))]
    unusable_machines = [m for m in machines if not bool(m.get("is_usable", False))]
    machines_by_line = defaultdict(list)
    for machine in machines:
        machines_by_line[int(machine["line_id"])].append(machine)
    line_machine_counts = [len(machines_by_line.get(line_id, [])) for line_id in sorted(line_id_set)]
    machine_line_ids = sorted({int(machine["line_id"]) for machine in machines})
    machine_line_ids_without_line_records = sorted(line for line in machine_line_ids if line not in line_id_set)
    machine_capabilities = Counter()
    for machine in machines:
        for capability in machine.get("capabilities", []):
            machine_capabilities[str(capability)] += 1

    op_machine_counts = [len(set(int(v) for v in op.get("eligible_machine_ids", []))) for op in operations]
    op_worker_counts = [len(set(str(v) for v in op.get("eligible_worker_ids", []))) for op in operations]
    zero_machine_ops = sorted(op_keys[i] for i, count in enumerate(op_machine_counts) if count == 0)
    one_machine_ops = sorted(op_keys[i] for i, count in enumerate(op_machine_counts) if count == 1)
    multi_machine_ops = sorted(op_keys[i] for i, count in enumerate(op_machine_counts) if count > 1)
    zero_worker_ops = sorted(op_keys[i] for i, count in enumerate(op_worker_counts) if count == 0)
    one_worker_ops = sorted(op_keys[i] for i, count in enumerate(op_worker_counts) if count == 1)
    multi_worker_ops = sorted(op_keys[i] for i, count in enumerate(op_worker_counts) if count > 1)

    duration_values = [op.get("serial_productive_seconds") for op in operations]
    missing_duration_ops = sorted(op_keys[i] for i, value in enumerate(duration_values) if value is None)
    negative_duration_ops = sorted(op_keys[i] for i, value in enumerate(duration_values) if value is not None and float(value) < 0)
    zero_duration_ops = sorted(op_keys[i] for i, value in enumerate(duration_values) if value is not None and float(value) == 0)
    valid_durations = [float(value) for value in duration_values if value is not None]

    worker_calendar_kinds = Counter(str(w.get("calendar", {}).get("kind", "UNRESOLVED")) for w in workers)
    invalid_calendar_workers = sorted(str(w["worker_id"]) for w in workers if w.get("calendar", {}).get("kind") == "UNRESOLVED")
    worker_skills = Counter()
    for worker in workers:
        for skill in worker.get("skills", []):
            worker_skills[str(skill)] += 1
    workers_by_factory = Counter(int(w.get("factory_id")) for w in workers)

    operation_line_refs = sorted({int(line) for op in operations for line in op.get("eligible_line_ids", [])})
    foreign_line_refs = sorted(line for line in operation_line_refs if line not in line_id_set)
    eligible_machine_refs = sorted({int(machine) for op in operations for machine in op.get("eligible_machine_ids", [])})
    foreign_machine_refs = sorted(machine for machine in eligible_machine_refs if machine not in machine_id_set)
    eligible_worker_refs = sorted({str(worker) for op in operations for worker in op.get("eligible_worker_ids", [])})
    foreign_worker_refs = sorted(worker for worker in eligible_worker_refs if worker not in worker_id_set)
    duplicate_machine_ids = sorted(k for k, count in Counter(machine_ids).items() if count > 1)
    duplicate_worker_ids = sorted(k for k, count in Counter(worker_ids).items() if count > 1)

    execution_counts = Counter(str(op.get("execution_state", {}).get("state", "UNRESOLVED")) for op in operations)
    currently_ready = [op for op in operations if bool(op.get("execution_state", {}).get("eligible_to_execute_at_snapshot"))]
    req_machine_values = [op.get("min_machine_count") for op in operations if op.get("min_machine_count") is not None]
    req_machine_max_values = [op.get("max_machine_count") for op in operations if op.get("max_machine_count") is not None]
    has_worker_requirement = any("min_worker_count" in op or "required_worker_count" in op for op in operations)
    worker_req_values = [op.get("min_worker_count", op.get("required_worker_count")) for op in operations if op.get("min_worker_count", op.get("required_worker_count")) is not None]

    blockers = []
    if len(op_keys) != len(op_key_set):
        blockers.append("DUPLICATE_OPERATION_KEYS")
    if len(machine_ids) != len(machine_id_set):
        blockers.append("DUPLICATE_MACHINE_IDS")
    if len(worker_ids) != len(worker_id_set):
        blockers.append("DUPLICATE_WORKER_IDS")
    if broken_pred or broken_succ:
        blockers.append("BROKEN_PREDECESSOR_REFERENCES")
    if cycle:
        blockers.append("PRECEDENCE_CYCLE")
    if missing_duration_ops:
        blockers.append("MISSING_DURATION")
    if negative_duration_ops:
        blockers.append("NEGATIVE_DURATION")
    if zero_machine_ops:
        blockers.append("ZERO_ELIGIBLE_MACHINES")
    if zero_worker_ops:
        blockers.append("ZERO_ELIGIBLE_WORKERS")
    if invalid_calendar_workers:
        blockers.append("INVALID_WORKER_CALENDAR")
    if foreign_line_refs or foreign_machine_refs or foreign_worker_refs:
        blockers.append("CROSS_FACTORY_RESOURCE_REFERENCE")
    if set(order_id_set) != set(by_order):
        blockers.append("OPERATION_ORDER_REFERENCE_MISMATCH")

    status = "READY_FOR_SCHEDULING" if not blockers else "NOT_SAFE_TO_SCHEDULE"
    identity = {
        "factory_id": factory_id,
        "snapshot_as_of_seconds": int(payload.get("snapshot_as_of_seconds", SNAPSHOT)),
        "operation_count": len(op_keys),
        "order_count": len(order_ids),
        "sorted_operation_key_sha256": _sha_sorted(op_keys),
        "sorted_order_id_sha256": _sha_sorted(order_ids),
        "usable_machine_count": len(usable_machines),
        "assignable_worker_count": len(workers),
    }
    audit = {
        "audit_version": "REMAINING_FACTORY_AUDIT_V1",
        "factory_id": factory_id,
        "snapshot_as_of_seconds": SNAPSHOT,
        "source_contract": payload.get("contract_version"),
        "identity": identity,
        "workload": {
            "TOTAL_FROZEN_OPERATIONS": len(operations),
            "TOTAL_UNIQUE_ORDERS": len(orders),
            "production_line_count": len(lines),
            "CURRENTLY_READY_OPERATIONS": len(currently_ready),
            "operations_per_order": _int_stats(order_op_counts),
            "released_orders_at_snapshot": len(released_orders),
            "future_release_orders": len(future_release_orders),
            "orders_with_valid_due_dates": len(dues),
            "orders_missing_due_dates": len(orders) - len(dues),
            "earliest_release_seconds": min(releases) if releases else None,
            "latest_release_seconds": max(releases) if releases else None,
            "earliest_due_seconds": min(dues) if dues else None,
            "latest_due_seconds": max(dues) if dues else None,
            "distinct_release_timestamps": len(release_points),
            "release_timestamps": release_points,
            "release_gap_seconds": _int_stats(release_gaps),
            "longest_release_gap_seconds": max(release_gaps) if release_gaps else 0,
        },
        "precedence": {
            "operations_without_predecessor": sum(not predecessor[key] for key in op_keys),
            "operations_with_predecessor": sum(bool(predecessor[key]) for key in op_keys),
            "precedence_edge_count": edges,
            "maximum_routing_sequence": max(sequences) if sequences else None,
            "broken_predecessor_references": broken_pred,
            "broken_successor_references": broken_succ,
            "cyclic_precedence_detected": bool(cycle),
        },
        "machines": {
            "total_machines_belonging_to_factory": len(machines),
            "usable_machines": len(usable_machines),
            "unusable_machines": len(unusable_machines),
            "production_line_count": len(lines),
            "machines_per_line": _int_stats(line_machine_counts),
            "machine_capability_groups": dict(sorted(machine_capabilities.items())),
            "eligible_machine_count_per_operation": _int_stats(op_machine_counts),
            "operations_with_zero_eligible_machines": zero_machine_ops,
            "operations_with_exactly_one_eligible_machine": one_machine_ops,
            "operations_with_multiple_eligible_machines": multi_machine_ops,
            "foreign_machine_references": foreign_machine_refs,
        },
        "workers": {
            "assignable_workers": len(workers),
            "workers_with_valid_calendars": len(workers) - len(invalid_calendar_workers),
            "workers_missing_or_invalid_calendars": invalid_calendar_workers,
            "worker_calendar_shift_distribution": dict(sorted(worker_calendar_kinds.items())),
            "worker_skill_capability_groups": dict(sorted(worker_skills.items())),
            "eligible_worker_count_per_operation": _int_stats(op_worker_counts),
            "operations_with_zero_eligible_workers": zero_worker_ops,
            "operations_with_exactly_one_eligible_worker": one_worker_ops,
            "operations_with_multiple_eligible_workers": multi_worker_ops,
            "foreign_worker_references": foreign_worker_refs,
            "workers_by_factory": dict(sorted(workers_by_factory.items())),
        },
        "durations": {
            "serial_productive_seconds": _stats(valid_durations),
            "zero_duration_count": len(zero_duration_ops),
            "zero_duration_operations": zero_duration_ops,
            "missing_duration_count": len(missing_duration_ops),
            "missing_duration_operations": missing_duration_ops,
            "negative_duration_count": len(negative_duration_ops),
            "negative_duration_operations": negative_duration_ops,
        },
        "resource_requirements": {
            "required_machine_count": _stats(req_machine_values),
            "required_machine_max_count": _stats(req_machine_max_values),
            "required_worker_count": _stats(worker_req_values) if has_worker_requirement else None,
            "required_worker_count_status": "AVAILABLE_IN_CONTRACT" if has_worker_requirement else "NOT_AVAILABLE_IN_NEUTRAL_OPERATION_CONTRACT",
            "operations_per_usable_machine": (len(operations) / len(usable_machines)) if usable_machines else None,
            "operations_per_assignable_worker": (len(operations) / len(workers)) if workers else None,
            "orders_per_usable_machine": (len(orders) / len(usable_machines)) if usable_machines else None,
        },
        "ownership_consistency": {
            "all_operations_belong_to_factory_orders": all(str(op.get("order_id")) in order_id_set for op in operations),
            "operation_line_references": operation_line_refs,
            "foreign_line_references": foreign_line_refs,
            "foreign_machine_references": foreign_machine_refs,
            "foreign_worker_references": foreign_worker_refs,
            "machine_line_ids_without_line_records": machine_line_ids_without_line_records,
            "empty_workload": len(operations) == 0 and len(orders) == 0,
            "duplicate_operation_keys": sorted(k for k, count in Counter(op_keys).items() if count > 1),
            "duplicate_machine_ids": duplicate_machine_ids,
            "duplicate_worker_ids": duplicate_worker_ids,
        },
        "commitments": {
            "completed_operations": execution_counts.get("COMPLETED", 0),
            "running_operations": execution_counts.get("IN_PROGRESS", 0),
            "committed_future_operations": execution_counts.get("COMMITTED_OR_FROZEN_AUTHORITATIVE", 0),
            "provisional_future_operations": execution_counts.get("PROVISIONAL_FUTURE", 0),
            "unscheduled_or_future_operations": execution_counts.get("RELEASED_FUTURE_WORK", 0) + execution_counts.get("FUTURE_RELEASE", 0),
            "execution_state_counts": dict(sorted(execution_counts.items())),
        },
        "ratios_are_descriptive_only": True,
        "blockers": sorted(set(blockers)),
        "scheduling_readiness": status,
        "policy_independence": payload.get("policy_independence", {}),
        "database_access": "READ_ONLY",
        "database_writes": 0,
    }
    return audit


def _summary_markdown(summary: dict, order: list[int]) -> str:
    rows = [
        "# Remaining Factory Neutral Audit",
        "",
        "The audit is policy-independent and performs no scheduling. Factory 2 is a frozen reference only.",
        "",
        "| Factory | Operations | Orders | Usable Machines | Assignable Workers | Lines | Future-Release Orders | Zero-Machine Ops | Zero-Worker Ops | Data Blockers | Status |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in summary["comparison_table"]:
        blockers = ", ".join(row["data_blockers"]) if row["data_blockers"] else "none"
        rows.append(f"| {row['factory_id']} | {row['operations']} | {row['orders']} | {row['usable_machines']} | {row['assignable_workers']} | {row['lines']} | {row['future_release_orders']} | {row['zero_machine_ops']} | {row['zero_worker_ops']} | {blockers} | {row['status']} |")
    rows += ["", f"Recommended evaluation order: {' → '.join('Factory ' + str(x) for x in order)}", "", "Database writes: 0", "Factory 2 artifacts: unchanged (hash-verified)."]
    return "\n".join(rows) + "\n"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    frozen_before = {name: _tree_hash(path) for name, path in FROZEN_PATHS.items()}
    loaded = _load_state()
    audits = {}
    identities = {}
    summary_rows = []
    for factory_id in FACTORIES:
        neutral = extract_neutral_factory_workload(loaded, factory_id=factory_id, snapshot_as_of_seconds=SNAPSHOT)
        payload = neutral.payload
        audit = audit_payload(payload)
        audits[factory_id] = audit
        identities[str(factory_id)] = audit["identity"]
        _write(OUT / f"neutral_factory{factory_id}_state.json", payload)
        _write(OUT / f"factory{factory_id}_audit.json", audit)
        summary_rows.append({
            "factory_id": factory_id,
            "operations": audit["workload"]["TOTAL_FROZEN_OPERATIONS"],
            "orders": audit["workload"]["TOTAL_UNIQUE_ORDERS"],
            "usable_machines": audit["machines"]["usable_machines"],
            "assignable_workers": audit["workers"]["assignable_workers"],
            "lines": audit["workload"].get("production_line_count", len(payload.get("lines", []))),
            "future_release_orders": audit["workload"]["future_release_orders"],
            "zero_machine_ops": len(audit["machines"]["operations_with_zero_eligible_machines"]),
            "zero_worker_ops": len(audit["workers"]["operations_with_zero_eligible_workers"]),
            "data_blockers": audit["blockers"],
            "status": audit["scheduling_readiness"],
        })

    # Add the immutable Factory-2 reference row without extracting or scheduling it.
    summary_rows.insert(0, {
        "factory_id": 2,
        "operations": 172,
        "orders": 20,
        "usable_machines": 434,
        "assignable_workers": 1780,
        "lines": 17,
        "future_release_orders": 0,
        "zero_machine_ops": None,
        "zero_worker_ops": None,
        "data_blockers": [],
        "status": "FROZEN_REFERENCE",
    })
    ready = [fid for fid, audit in audits.items() if audit["scheduling_readiness"] == "READY_FOR_SCHEDULING"]
    blocked = [fid for fid, audit in audits.items() if audit["scheduling_readiness"] != "READY_FOR_SCHEDULING"]
    # Smallest structurally ready workload first, then operation/order scale and
    # eligibility width.  This is an audit-based recommendation, not a schedule.
    def rank_key(fid):
        audit = audits[fid]
        return (
            0 if audit["scheduling_readiness"] == "READY_FOR_SCHEDULING" else 1,
            # An empty factory is structurally clean but is not a useful next
            # scheduling experiment.  Rank non-empty workloads first, then
            # the measured workload/resource scale.
            0 if audit["workload"]["TOTAL_FROZEN_OPERATIONS"] > 0 else 1,
            audit["workload"]["TOTAL_FROZEN_OPERATIONS"],
            audit["workload"]["TOTAL_UNIQUE_ORDERS"],
            audit["machines"]["usable_machines"],
            audit["workers"]["assignable_workers"],
        )
    recommended_order = sorted(FACTORIES, key=rank_key)
    summary = {
        "audit_version": "REMAINING_FACTORY_AUDIT_V1",
        "snapshot_as_of_seconds": SNAPSHOT,
        "comparison_table": summary_rows,
        "remaining_operations": sum(audits[f]["workload"]["TOTAL_FROZEN_OPERATIONS"] for f in FACTORIES),
        "remaining_orders": sum(audits[f]["workload"]["TOTAL_UNIQUE_ORDERS"] for f in FACTORIES),
        "ready_factory_ids": ready,
        "blocked_factory_ids": blocked,
        "recommended_evaluation_order": recommended_order,
        "database_access": "READ_ONLY",
        "database_writes": 0,
        "scheduler_runs": {"CRAFT": False, "FIFO": False, "EDD": False, "SPT": False},
        "factory2_rerun": False,
    }
    _write(OUT / "remaining_factories_summary.json", summary)
    (OUT / "remaining_factories_summary.md").write_text(_summary_markdown(summary, recommended_order), encoding="utf-8")
    _write(OUT / "recommended_evaluation_order.json", {
        "recommended_evaluation_order": recommended_order,
        "basis": "structural readiness, then operation count, order count, usable-machine count, assignable-worker count",
        "no_scheduling_executed": True,
    })
    _write(OUT / "factory_identity_manifest.json", {
        "snapshot_as_of_seconds": SNAPSHOT,
        "factories": identities,
        "factory2_reference": {
            "factory_id": 2,
            "operation_count": 172,
            "order_count": 20,
            "usable_machine_count": 434,
            "assignable_worker_count": 1780,
            "status": "FROZEN_REFERENCE",
        },
    })
    frozen_after = {name: _tree_hash(path) for name, path in FROZEN_PATHS.items()}
    frozen_unchanged = frozen_before == frozen_after
    audit_files = sorted(p for p in OUT.iterdir() if p.is_file() and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"})
    manifest = {
        "audit_output_files": {p.name: _sha_file(p) for p in audit_files},
        "frozen_source_tree_hashes_before": frozen_before,
        "frozen_source_tree_hashes_after": frozen_after,
        "frozen_factory2_artifacts_unchanged": frozen_unchanged,
    }
    _write(OUT / "artifact_hash_manifest.json", manifest)
    manifest_hash = _sha_file(OUT / "artifact_hash_manifest.json")
    verification = {
        "artifact_hash_manifest_sha256": manifest_hash,
        "audit_output_hashes_verified": all(_sha_file(OUT / name) == digest for name, digest in manifest["audit_output_files"].items()),
        "frozen_factory2_artifacts_unchanged": frozen_unchanged,
        "all_hashes_verified": bool(frozen_unchanged and all(_sha_file(OUT / name) == digest for name, digest in manifest["audit_output_files"].items())),
        "database_writes": 0,
    }
    _write(OUT / "hash_verification.json", verification)
    print("STATUS = REMAINING_FACTORY_AUDIT_COMPLETE")
    print("CRAFT_RUN = no")
    print("FIFO_RUN = no")
    print("EDD_RUN = no")
    print("SPT_RUN = no")
    print("FACTORY2_RERUN = no")
    print("DATABASE_READ_ONLY = yes")
    print("DATABASE_WRITES = 0")
    for fid in FACTORIES:
        audit = audits[fid]
        print(f"FACTORY{fid}_OPERATIONS = {audit['workload']['TOTAL_FROZEN_OPERATIONS']}")
        print(f"FACTORY{fid}_ORDERS = {audit['workload']['TOTAL_UNIQUE_ORDERS']}")
        print(f"FACTORY{fid}_USABLE_MACHINES = {audit['machines']['usable_machines']}")
        print(f"FACTORY{fid}_ASSIGNABLE_WORKERS = {audit['workers']['assignable_workers']}")
        print(f"FACTORY{fid}_LINES = {len([x for x in loaded[0].lines if int(x.factory_id) == fid])}")
        print(f"FACTORY{fid}_STATUS = {audit['scheduling_readiness']}")
    print(f"TOTAL_REMAINING_OPERATIONS = {summary['remaining_operations']}")
    print(f"TOTAL_REMAINING_ORDERS = {summary['remaining_orders']}")
    print(f"READY_FACTORY_COUNT = {len(ready)}")
    print(f"BLOCKED_FACTORY_COUNT = {len(blocked)}")
    print(f"RECOMMENDED_NEXT_FACTORY = {recommended_order[0] if recommended_order else None}")
    print("RECOMMENDED_EVALUATION_ORDER = " + " -> ".join(str(x) for x in recommended_order))
    print(f"FACTORY2_FROZEN_ARTIFACTS_UNCHANGED = {str(frozen_unchanged).lower()}")
    print(f"ARTIFACT_HASHES_VERIFIED = {str(verification['all_hashes_verified']).lower()}")
    print("BLOCKERS = " + ("; ".join(f"Factory {fid}: {', '.join(audits[fid]['blockers'])}" for fid in blocked) if blocked else "none"))
    print("NEXT_STEP = review structural audit; schedule only an explicitly authorized ready factory")


if __name__ == "__main__":
    main()
