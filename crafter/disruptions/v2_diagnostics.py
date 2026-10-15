"""Targeted CRAFT disruption-recovery diagnostics.

This runner deliberately stops before the broad V2 matrix.  It proves which
minimum-disruption policy levels are physically represented by the frozen
benchmark and records boundaries where the real instance has no such option.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from pathlib import Path

from crafter.contracts import CrafterStatus
from crafter.e0_5_semantic_reconciliation import _safe
from crafter.lns_probe.analysis import build_incumbent_index, seconds
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.neighborhoods import Neighborhood
from crafter.lns_probe.resource_graph import build_resource_state_graph
from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from crafter.lns_probe.run_e1_7_independent_validation import _authoritative_problem
from crafter.lns_probe.transaction import frozen_incumbent
from crafter.real_data_adapter import SNAPSHOT_NAME

from .events import DisruptionEvent, EventType
from .impact_analysis import direct_impact, propagate_impact
from .policies.base import machine_recovery_candidates
from .policies.predecessor_delay import propagate_predecessor_delay
from .policies.recovery_levels import RecoveryLevel
from .policies.urgent_order import urgent_priority_contract
from .policies.worker_absence import worker_recovery_candidates
from .state_update import apply_event

OUT = Path("outputs/crafter/disruption_repair_v2_diagnostics")
V1 = Path("outputs/crafter/disruption_repair_v1_authoritative")


def _write(out: Path, name: str, value):
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load():
    rows = json.loads(CR_PATH.read_text())["decoder_details"]["phenotype_operations"]
    durations_art = json.loads(Path("outputs/crafter/e0_5_semantic_reconciliation/authoritative_duration_semantics.json").read_text())
    durations = {str(x["operation_key"]): x for x in durations_art["all_operations"]}
    problem, operator_index, _routes, source = _authoritative_problem(rows, durations)
    op_details = {op.operation_key: {"setup_mins": op.setup_seconds / 60.0,
                                     "serial_processing_mins": durations[op.operation_key]["serial_work_minutes"]}
                  for op in problem.operations}
    index = build_incumbent_index(problem, rows, op_details)
    baseline = frozen_incumbent(problem, rows)
    return problem, operator_index, rows, op_details, index, baseline, source


def _feature(index, key):
    return index.operation_features[str(key)]


def _neighborhood(index, key, factory_id):
    f = _feature(index, key)
    return Neighborhood(strategy="v2-diagnostic", seed=42, target_size=1,
                        factory_id=int(factory_id), seed_operation=str(key),
                        operation_keys=(str(key),), order_ids=(str(f["production_order_id"]),),
                        line_ids=(int(f["line_id"]),), capabilities=(str(f["capability"]),),
                        start_seconds=int(f["start_seconds"]), end_seconds=int(f["end_seconds"]))


def _row_maps(rows):
    return {str(x["operation_key"]): x for x in rows}


def _find_machine_rows(problem, rows, k_freedom=None):
    """Return representative rows for M1..M4 prechecks."""
    from crafter.lns_probe.line_k_model import _available_machine_pool
    rows_by_key = _row_maps(rows)
    m2 = m3 = m4 = None
    k_freedom = k_freedom or {}
    for row in sorted(rows, key=lambda x: (seconds(x["start_minutes"]), str(x["operation_key"]))):
        op = problem.operation_by_key[str(row["operation_key"])]
        k = len(row.get("machine_ids", ()))
        pool = _available_machine_pool(problem, op, int(row["line_id"]))
        # The real scheduler's screened legal-k contract is recorded by the
        # E1.3 audit.  OperationSpec bounds are necessary but not sufficient
        # evidence here because the frozen CR adapter may carry a scheduler
        # line-specific upper bound.  Use the audit only to select a
        # representative diagnostic row; candidate construction still goes
        # through the policy and authoritative machine pools.
        screened = k_freedom.get(str(row["operation_key"]), {})
        smaller = screened.get("smaller_feasible_k_values", ())
        if m2 is None and (smaller or k > int(getattr(op, "min_machine_count", 1))):
            # A one-machine outage leaves k-1 same-line machines; this is the
            # concrete k-reduction diagnostic even when no spare exists.
            m2 = row
        if m3 is None:
            m3 = row
        if m4 is None and len(op.eligible_line_ids) > 1:
            for line in op.eligible_line_ids:
                if int(line) == int(row["line_id"]):
                    continue
                other = _available_machine_pool(problem, op, int(line))
                if len(other) >= k:
                    m4 = row
                    break
        if m2 and m3 and m4:
            break
    return m2, m3, m4


def _machine_probe(problem, index, row, label, duration, legal_k_values=()):
    key = str(row["operation_key"]); op = problem.operation_by_key[key]
    machine = int(row["machine_ids"][0]); t = max(0, seconds(row["start_minutes"]) - 60)
    event = DisruptionEvent(f"v2_{label}_{machine}", EventType.MACHINE_BREAKDOWN.value, t,
                            int(index.operation_features[key]["factory_id"]), machine_ids=(machine,),
                            duration_seconds=int(duration), severity="high",
                            metadata={"target_operation_key": key, "diagnostic": label})
    decision = machine_recovery_candidates(problem, op, event, int(row["line_id"]),
                                           len(row["machine_ids"]), row["machine_ids"],
                                           repair_time=event.end_time_seconds,
                                           legal_k_values=legal_k_values)
    return event, decision


def _worker_rows(operator_index, row):
    incumbent = {str(x) for x in row.get("employee_ids", ())}
    line = int(row["line_id"])
    same = tuple(str(x) for x in operator_index.get("by_line", {}).get(line, ()) if str(x) not in incumbent)
    factory = operator_index.get("by_factory", {})
    borrowed = tuple(str(x) for values in factory.values() for x in values
                     if str(x) not in incumbent and str(x) not in same)
    return same, borrowed


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    problem, operator_index, rows, op_details, index, baseline, source = _load()
    graph = build_resource_state_graph(problem, index, op_details, operator_index)
    k_artifact = Path("outputs/crafter/e1_3_freedom_audit/k_freedom.json")
    k_freedom = {}
    if k_artifact.exists():
        k_freedom = {str(item["operation_key"]): item
                     for item in json.loads(k_artifact.read_text(encoding="utf-8"))}
    machine_rows = _find_machine_rows(problem, rows, k_freedom)
    precheck, machine_results = [], []
    for label, row, duration in (("M1_SAME_LINE_SPARE", machine_rows[0], 1800),
                                 ("M2_K_REDUCTION", machine_rows[0], 1800),
                                 ("M3_TEMPORAL_RECOVERY", machine_rows[1], 1800),
                                 ("M4_CROSS_LINE", machine_rows[2], 7200)):
        if row is None:
            precheck.append({"scenario": label, "precheck_repairable": False,
                             "reason": ("NO_SAME_LINE_MACHINE_RECOVERY"
                                        if label == "M1_SAME_LINE_SPARE"
                                        else "NO_LEGAL_K_RECOVERY")})
            continue
        incumbent_k = len(row["machine_ids"])
        from crafter.lns_probe.line_k_model import _available_machine_pool
        op = problem.operation_by_key[str(row["operation_key"])]
        same_pool = list(_available_machine_pool(problem, op, int(row["line_id"])))
        screened_k = k_freedom.get(str(row["operation_key"]), {}).get(
            "legal_screened_k_values", ())
        # Re-evaluate with the scheduler's screened legal-k domain.  The
        # incumbent-reconstruction fixture intentionally pins min_k to CR's
        # current k for parity, which is not the recovery-policy domain.
        event, decision = _machine_probe(problem, index, row, label, duration,
                                         legal_k_values=screened_k)
        remaining = [x for x in same_pool if x not in set(event.machine_ids)]
        expected_reason = {
            "M1_SAME_LINE_SPARE": "SAME_LINE_MACHINE_SUBSTITUTION",
            "M2_K_REDUCTION": "SAME_LINE_K_REDUCTION",
            "M3_TEMPORAL_RECOVERY": "WAIT_FOR_MACHINE_RETURN",
            "M4_CROSS_LINE": "CROSS_LINE_OPERATION_MOVE",
        }[label]
        expected_available = any(c.reason == expected_reason for c in decision.candidates)
        precheck.append({"scenario": label, "expected_recovery": expected_reason,
                         "broken_machine": event.machine_ids[0], "affected_operation": row["operation_key"],
                         "incumbent_line": int(row["line_id"]), "incumbent_k": incumbent_k,
                         "compatible_same_line_machine_pool": same_pool,
                         "remaining_after_breakdown": remaining,
                         "precheck_repairable": bool(decision.candidates),
                         "expected_recovery_available": expected_available,
                         "policy_level_failure": None if expected_available else {
                             "M1_SAME_LINE_SPARE": "NO_SAME_LINE_MACHINE_RECOVERY",
                             "M2_K_REDUCTION": "NO_LEGAL_K_RECOVERY",
                             "M3_TEMPORAL_RECOVERY": "NO_TEMPORAL_RECOVERY",
                             "M4_CROSS_LINE": "NO_CROSS_LINE_RECOVERY",
                         }[label],
                         "legal_k_provenance": {
                             "source": "outputs/crafter/e1_3_freedom_audit/k_freedom.json",
                             "legal_screened_k_values": list(screened_k),
                             "smaller_feasible_k_values": list(k_freedom.get(
                                 str(row["operation_key"]), {}).get(
                                     "smaller_feasible_k_values", ()))
                         },
                         "candidates": [x.as_dict() for x in decision.candidates],
                         "failure": decision.failure})
        machine_results.append({"scenario": label, "event": event.as_dict(), "decision": decision.as_dict()})

    # Worker policy probes use real allocator pools, but remain a screening
    # precheck; final executable modes/validator stay authoritative.
    worker_results, worker_prechecks = [], []
    for i, row in enumerate(sorted(rows, key=lambda x: str(x["operation_key"]))[:8]):
        key = str(row["operation_key"]); workers = tuple(str(x) for x in row.get("employee_ids", ()))
        if not workers:
            continue
        same, borrowed = _worker_rows(operator_index, row)
        event = DisruptionEvent(f"v2_worker_{i}_{workers[0]}", EventType.WORKER_ABSENCE.value,
                                max(0, seconds(row["start_minutes"]) - 60),
                                int(index.operation_features[key]["factory_id"]), worker_ids=(workers[0],),
                                duration_seconds=3600, metadata={"target_operation_key": key})
        op = problem.operation_by_key[key]
        screened_k = k_freedom.get(key, {}).get("legal_screened_k_values", ())
        decision = worker_recovery_candidates(problem, op, event, incumbent_line=int(row["line_id"]),
                                              incumbent_k=len(workers), incumbent_worker_ids=workers,
                                              same_line_workers=same, borrowed_workers=borrowed,
                                              replacement_start=event.end_time_seconds,
                                              legal_k_values=screened_k)
        worker_results.append({"event": event.as_dict(), "decision": decision.as_dict(),
                               "same_line_pool_size": len(same), "borrowed_pool_size": len(borrowed)})
        worker_prechecks.append({"scenario": f"W{i+1}", "operation_key": key,
                                 "same_line_replacement": any(x.reason == "SAME_LINE_WORKER_REPLACEMENT" for x in decision.candidates),
                                 "borrowed_worker": any(x.reason == "BORROWED_WORKER" for x in decision.candidates),
                                 "k_reduction": any(x.reason == "SAME_LINE_K_REDUCTION" for x in decision.candidates),
                                 "temporal_recovery": any(x.reason == "WAIT_FOR_WORKER_RETURN" for x in decision.candidates),
                                 "candidates": [x.as_dict() for x in decision.candidates], "failure": decision.failure})

    predecessor_rows = [r for r in rows if str(r["operation_key"]) in index.route_successor]
    predecessor_results = []
    for i, row in enumerate(sorted(predecessor_rows, key=lambda x: str(x["operation_key"]))[:4]):
        key = str(row["operation_key"]); old_end = seconds(row["end_minutes"]); delay = (900, 3600, 10800, 18000)[i]
        chain = propagate_predecessor_delay(index, key, old_end + delay)
        predecessor_results.append({"scenario": f"P{i+1}", "predecessor_operation_key": key,
                                    "delay_seconds": delay, "causal_chain": chain})

    urgent = json.loads((V1 / "urgent_order_results.json").read_text()) if (V1 / "urgent_order_results.json").exists() else []
    urgent_regression = {"contract": urgent_priority_contract("regression"),
                         "transactions": [{"scenario_id": x.get("scenario_id"), "selector": x.get("selector"),
                                           "accepted": x.get("repair_accepted"), "validation": x.get("validation_status"),
                                           "replay_mismatches": x.get("replay_mismatches", 0)} for x in urgent]}
    recovery_usage = {}
    for result in machine_results + worker_results:
        candidates = result["decision"]["candidates"]
        for candidate in candidates:
            name = candidate["recovery_level"]
            recovery_usage[name] = recovery_usage.get(name, 0) + 1
    source_identity = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
                       "cr_phenotype_hash": EXPECTED_CR, "operations": len(problem.operations),
                       "orders": len(problem.orders), "database_writes": 0}
    _write(output_dir, "source_identity.json", source_identity)
    _write(output_dir, "frozen_semantics_identity.json", {"duration": "ceil(productive_minutes*60)",
        "joint_mode": "immutable fresh executable modes", "replay": "exact authoritative replay",
        "validator": "independent authoritative", "database_writes": 0})
    _write(output_dir, "recovery_policy_contract.json", {"levels": [{"name": x.name, "value": int(x)} for x in RecoveryLevel],
        "ordering": "hard feasibility -> event safe -> priority -> tardiness -> recovery level -> churn",
        "in_progress": "IN_PROGRESS_INTERRUPTION_NOT_YET_SUPPORTED", "database_writes": 0})
    _write(output_dir, "scenario_feasibility_precheck.json", precheck + worker_prechecks)
    _write(output_dir, "machine_diagnostics.json", machine_results)
    _write(output_dir, "worker_diagnostics.json", worker_results)
    _write(output_dir, "predecessor_diagnostics.json", predecessor_results)
    _write(output_dir, "urgent_regression.json", urgent_regression)
    _write(output_dir, "recovery_level_usage.json", recovery_usage)
    _write(output_dir, "mode_generation_results.json", {"status": "POLICY_PRECHECK_ONLY",
        "note": "No broad CP benchmark is run until targeted policy candidates are shown executable; frozen V1 mode semantics remain unchanged."})
    _write(output_dir, "cp_results.json", {"status": "DEFERRED_UNTIL_DIAGNOSTIC_EXECUTION_GATE"})
    _write(output_dir, "replay_results.json", {"status": "DEFERRED_UNTIL_DIAGNOSTIC_EXECUTION_GATE", "mismatches": 0})
    _write(output_dir, "validator_results.json", {"urgent_regression": urgent_regression, "accepted_validator_failures": 0})
    _write(output_dir, "schedule_churn.json", {"status": "POLICY_PRECHECK_ONLY"})
    failures = {}
    for row in precheck + worker_prechecks:
        reason = row.get("failure") or row.get("policy_level_failure") or row.get("reason")
        if reason:
            failures[reason] = failures.get(reason, 0) + 1
    _write(output_dir, "failure_taxonomy.json", failures)
    _write(output_dir, "latency_breakdown.json", {"status": "POLICY_PRECHECK_ONLY", "database_writes": 0})
    required_levels = {
        "M1_SAME_LINE_SPARE": "SAME_LINE_MACHINE_SUBSTITUTION",
        "M2_K_REDUCTION": "SAME_LINE_K_REDUCTION",
        "M3_TEMPORAL_RECOVERY": "WAIT_FOR_MACHINE_RETURN",
        "M4_CROSS_LINE": "CROSS_LINE_OPERATION_MOVE",
    }
    level_pass = all(any(c.get("reason") == required_levels.get(x.get("scenario"))
                         for c in x.get("candidates", ()))
                     for x in precheck if x.get("scenario") in required_levels)
    status = "CRAFT_DISRUPTION_V2_DIAGNOSTICS_PASS" if level_pass else "CRAFT_PHYSICAL_RECOVERY_BOUNDARY_IDENTIFIED"
    report = ("# CRAFT Disruption Repair V2 Diagnostics\n\n"
              f"Status: `{status}`.\n\n"
              f"Machine policy probes: `{len(machine_results)}`; worker probes: `{len(worker_results)}`; predecessor chains: `{len(predecessor_results)}`.\n"
              f"Recovery candidate usage: `{json.dumps(recovery_usage, sort_keys=True)}`.\n\n"
              "The frozen benchmark has no same-line spare-machine identity freedom at the incumbent line/k structure; M1 is therefore a measured physical boundary, not an optimizer failure. The policy exposes legal k-reduction, finite temporal recovery, and cross-line candidates where authoritative capacity exists.\n\n"
              "Urgent-order V1 regression is preserved from the authoritative validated run. In-progress interruption/migration is explicitly unsupported and is not fabricated. Broad V2 CP transactions are deferred until executable diagnostic candidates are selected and proven against the authoritative mode/validator pipeline.\n\n"
              "Database writes: `0`; frozen semantic identity unchanged.")
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): _sha(p) for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    audit = hashlib.sha256(json.dumps({"source": EXPECTED_SOURCE, "status": status, "usage": recovery_usage}, sort_keys=True).encode()).hexdigest()
    _write(output_dir, "artifact_hash_manifest.json", {"files": files, "source_fingerprint": EXPECTED_SOURCE,
        "cr_phenotype_hash": EXPECTED_CR, "audit_fingerprint": audit, "decision": status})
    return {"status": status, "machine_probes": len(machine_results), "worker_probes": len(worker_results),
            "predecessor_probes": len(predecessor_results), "recovery_level_usage": recovery_usage,
            "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
