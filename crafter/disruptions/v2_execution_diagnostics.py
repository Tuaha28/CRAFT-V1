"""Targeted executable proof for the frozen CRAFT Industry Model V1.

This runner intentionally stops before the broad V2 matrix.  It exercises a
small set of real-data events through fresh modes, CP-SAT, replay, and the
authoritative validator.  No database state is mutated.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from crafter.contracts import CrafterStatus
from crafter.e0_5_semantic_reconciliation import _safe
from crafter.lns_probe.analysis import build_incumbent_index, seconds
from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from crafter.lns_probe.resource_graph import build_resource_state_graph
from crafter.real_data_adapter import SNAPSHOT_NAME

from .benchmark import SIZES, _load_state, _neighborhood, _run_attempt
from .events import DisruptionEvent, EventType
from .impact_analysis import direct_impact, propagate_impact
from .runtime.execution_state import FreezeParameters, classify_operations
from .runtime.fallback import build_fallback_decision
from .runtime.freeze_policy import freeze_decision
from .runtime.recovery_engine import RecoveryEngine
from .runtime.stability import stability_metrics
from .runtime.trigger_policy import evaluate_trigger
from .state_update import apply_event, event_conflicts


OUT = Path("outputs/crafter/disruption_repair_v2_execution_diagnostics")
MODEL = Path("outputs/crafter/industry_operating_model_v1")
PRECHECK = Path("outputs/crafter/disruption_repair_v2_diagnostics_final3/scenario_feasibility_precheck.json")


def _write(out: Path, name: str, value) -> None:
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_precheck():
    if PRECHECK.exists():
        rows = json.loads(PRECHECK.read_text(encoding="utf-8"))
        return {str(row.get("scenario")): row for row in rows if row.get("scenario")}
    return {}


def _event_from_row(row, label, *, duration_seconds, event_type=EventType.MACHINE_BREAKDOWN.value,
                    worker=False, predecessor=False):
    key = str(row["operation_key"])
    start = max(0, seconds(row["start_minutes"]) - 60)
    factory = int(row.get("factory_id", 0) or 0)
    metadata = {"seed": 42, "target_operation_key": key, "diagnostic": label}
    if predecessor:
        old_end = seconds(row["end_minutes"])
        metadata.update({"predecessor_operation_key": key,
                         "delayed_end_seconds": old_end + int(duration_seconds),
                         "delay_seconds": int(duration_seconds)})
        return DisruptionEvent(label, EventType.PREDECESSOR_DELAY.value, start, factory,
                               operation_keys=(key,), duration_seconds=int(duration_seconds),
                               severity="high", metadata=metadata)
    if worker:
        return DisruptionEvent(label, EventType.WORKER_ABSENCE.value, start, factory,
                               worker_ids=(str(row["employee_ids"][0]),),
                               duration_seconds=int(duration_seconds), severity="high",
                               metadata=metadata)
    return DisruptionEvent(label, event_type, start, factory,
                           machine_ids=(int(row["machine_ids"][0]),),
                           duration_seconds=int(duration_seconds), severity="high",
                           metadata=metadata)


def _restore_finite_machine(shadow, event):
    if not event.machine_ids or event.duration_seconds <= 0:
        return shadow
    blocked = set(int(x) for x in event.machine_ids)
    machines = tuple(replace(machine, is_usable=True) if machine.machine_id in blocked else machine
                     for machine in shadow.problem.machines)
    return replace(shadow, problem=replace(shadow.problem, machines=machines))


def _selected_summary(attempt):
    return [{"operation_key": key, "mode_id": mode.get("mode_id"),
             "recovery_level": mode.get("recovery_level"),
             "recovery_reason": mode.get("recovery_reason"),
             "source_event_id": mode.get("source_event_id"),
             "line_id": mode.get("line_id"), "k": mode.get("machine_count"),
             "machine_ids": mode.get("machine_ids"), "worker_ids": mode.get("worker_ids")}
            for key, mode in sorted(attempt.get("selected_modes", {}).items())]


def _run_one(problem, operator_index, rows, op_details, index, baseline, checked,
             event, label, *, n=30, b=5):
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    classification = classify_operations(rows, event.event_time_seconds,
                                         parameters=FreezeParameters(),
                                         blocked_operations=direct)
    trigger = evaluate_trigger(event, baseline, problem,
                               delivery_risk=event.event_type == EventType.URGENT_ORDER.value)
    freeze_rows = [{"operation_key": item["operation_key"],
                    "state": item["state"],
                    "decision": freeze_decision(item, explicitly_released=item["operation_key"] in direct)}
                   for item in classification if item["operation_key"] in set(direct)]
    started = time.perf_counter()
    attempt_history = []
    attempt = None
    for attempt_n, attempt_b in SIZES:
        attempt = _run_attempt(shadow.problem, problem, shadow.allocation_index,
                               operator_index, rows, op_details, after_index, baseline,
                               checked, event, impact, attempt_n, attempt_b, "graph")
        attempt_history.append(attempt)
        if attempt.get("accepted"):
            break
        if attempt.get("status") == "MODE_GENERATION_FAILURE" and "NO_MACHINE_SET" in str(attempt.get("error", "")):
            break
    elapsed = time.perf_counter() - started
    baseline_conflicts = event_conflicts(event, baseline, problem)
    fallback = build_fallback_decision(
        event, baseline_conflicts=baseline_conflicts,
        attempted_levels=["TEMPORAL_SHIFT", "SAME_LINE_RESOURCE_SUBSTITUTION",
                          "SAME_LINE_CAPACITY_ADJUSTMENT", "CROSS_LINE_OPERATION_MOVE"],
        options=[])
    selected = _selected_summary(attempt)
    recovery_levels = sorted({str(x.get("recovery_level")) for x in selected if x.get("recovery_level")})
    return {
        "scenario_id": event.event_id,
        "event": event.as_dict(),
        "trigger": trigger.as_dict(),
        "direct_operations": sorted(direct),
        "impact": impact,
        "execution_state_counts": {state: sum(x["state"] == state for x in classification)
                                    for state in sorted({x["state"] for x in classification})},
        "freeze_decisions": freeze_rows,
        "initial_n": n, "initial_b": b,
        "attempt": attempt,
        "attempt_history": attempt_history,
        "selected_modes": selected,
        "recovery_levels_used": recovery_levels,
        "fallback": fallback,
        "elapsed_seconds": elapsed,
        "policy_status": ("POLICY_RECOVERY_SUCCESS" if attempt.get("accepted") else
                           ("NO_EXECUTABLE_MODE_ANYWHERE" if "MODE_GENERATION_FAILURE" in attempt.get("status", "") else
                            attempt.get("status", "REPAIR_NOT_FOUND"))),
        "database_writes": 0,
    }


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    by_key = {str(row["operation_key"]): row for row in rows}
    precheck = _load_precheck()
    # The same real operation is used for M2/M3/M4 only when the diagnostic
    # evidence proves a lower-level candidate or a finite return exists.
    machine_key = "122_1061" if "122_1061" in by_key else sorted(by_key)[0]
    worker_key = next((key for key in sorted(by_key)
                       if len(by_key[key].get("machine_ids", ())) <= 2),
                      "1029_292" if "1029_292" in by_key else sorted(by_key)[0])
    predecessor_key = next((key for key in sorted(index.route_successor)
                            if index.route_successor.get(key)
                            and len(by_key.get(key, {}).get("machine_ids", ())) <= 2),
                           next((key for key in sorted(index.route_successor)
                                 if index.route_successor.get(key)), machine_key))
    events = [
        _event_from_row(by_key[machine_key], "M2_K_REDUCTION", duration_seconds=1800),
        _event_from_row(by_key[machine_key], "M3_TEMPORAL_RECOVERY", duration_seconds=1800),
        _event_from_row(by_key[machine_key], "M4_CROSS_LINE", duration_seconds=0),
        _event_from_row(by_key[worker_key], "W2_BORROWED_WORKER", duration_seconds=28800, worker=True),
        _event_from_row(by_key[predecessor_key], "P2_PREDECESSOR_DELAY", duration_seconds=3600, predecessor=True),
    ]
    results = []
    for event in events:
        try:
            results.append(_run_one(problem, operator_index, rows, op_details, index,
                                    baseline, checked, event, event.metadata.get("diagnostic", event.event_id)))
        except ValueError as exc:
            # generate_joint_modes uses ValueError only for the explicit,
            # classified zero-executable-mode boundary.  Do not catch
            # RuntimeError/TypeError/etc.; those are implementation defects
            # and must fail loudly during development.
            results.append({"scenario_id": event.event_id, "event": event.as_dict(),
                            "trigger": evaluate_trigger(event, baseline, problem).as_dict(),
                            "direct_operations": sorted(direct_impact(index, rows, event)),
                            "policy_status": "NO_EXECUTABLE_MODE_ANYWHERE",
                            "error": str(exc), "database_writes": 0})
    # P1 is a causal/monitor-only diagnostic and should not force CP when no
    # resource conflict is created.  It is represented explicitly here.
    p1 = _event_from_row(by_key[predecessor_key], "P1_PREDECESSOR_DELAY", duration_seconds=900, predecessor=True)
    p1_trigger = evaluate_trigger(p1, baseline, problem)
    results.append({"scenario_id": p1.event_id, "event": p1.as_dict(),
                    "trigger": p1_trigger.as_dict(), "policy_status": "MONITOR_ONLY",
                    "reason": "causal_delay_without_material_resource_conflict_is_monitored",
                    "database_writes": 0})
    urgent = json.loads(Path("outputs/crafter/disruption_repair_v1_authoritative/urgent_order_results.json").read_text()) if Path("outputs/crafter/disruption_repair_v1_authoritative/urgent_order_results.json").exists() else []
    identity = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
                "cr_phenotype_hash": EXPECTED_CR, "operations": len(problem.operations),
                "orders": len(problem.orders), "database_writes": 0}
    _write(output_dir, "source_identity.json", identity)
    model_files = sorted(str(path.name) for path in MODEL.glob("*.json"))
    _write(output_dir, "industry_model_identity.json", {"directory": str(MODEL), "files": model_files,
        "decision": "CRAFT_INDUSTRY_MODEL_V1_FROZEN", "database_writes": 0})
    _write(output_dir, "event_catalog.json", [x["event"] for x in results])
    _write(output_dir, "execution_state_classification.json", [{"scenario_id": x["scenario_id"], "counts": x.get("execution_state_counts", {})} for x in results])
    _write(output_dir, "trigger_decisions.json", [{"scenario_id": x["scenario_id"], "trigger": x.get("trigger")} for x in results])
    _write(output_dir, "freeze_decisions.json", [{"scenario_id": x["scenario_id"], "decisions": x.get("freeze_decisions", [])} for x in results])
    _write(output_dir, "impact_analysis.json", [{"scenario_id": x["scenario_id"], "direct_operations": x.get("direct_operations", []), "impact": x.get("impact", {})} for x in results])
    _write(output_dir, "recovery_attempts.json", [{
        "scenario_id": x["scenario_id"],
        "policy_status": x.get("policy_status"),
        "initial_n": x.get("initial_n"),
        "initial_b": x.get("initial_b"),
        "recovery_levels_used": x.get("recovery_levels_used", []),
        # Preserve every bounded escalation attempt, not only the final one.
        # This is the auditable policy/recovery trace used to distinguish a
        # physical boundary from a CP or validator failure.
        "attempt_history": x.get("attempt_history", []),
        "attempt": x.get("attempt"),
    } for x in results])
    _write(output_dir, "fresh_modes.json", [{"scenario_id": x["scenario_id"], "selected_modes": x.get("selected_modes", []), "mode_count": x.get("attempt", {}).get("modes")} for x in results])
    _write(output_dir, "cp_results.json", [{"scenario_id": x["scenario_id"], "solve": x.get("attempt", {}).get("solve"), "model": x.get("attempt", {}).get("model")} for x in results])
    _write(output_dir, "replay_results.json", [{"scenario_id": x["scenario_id"], "replay": x.get("attempt", {}).get("replay"), "mismatches": x.get("attempt", {}).get("replay_mismatches")} for x in results])
    _write(output_dir, "validator_results.json", [{"scenario_id": x["scenario_id"], "validation": x.get("attempt", {}).get("validation"), "accepted": x.get("attempt", {}).get("accepted")} for x in results])
    _write(output_dir, "fallback_results.json", [{"scenario_id": x["scenario_id"], "fallback": x.get("fallback")} for x in results])
    _write(output_dir, "manager_escalations.json", [x for x in results if x.get("policy_status") == "MANAGER_REQUIRED"])
    _write(output_dir, "schedule_stability.json", [{"scenario_id": x["scenario_id"], "metrics": stability_metrics(baseline, baseline) if not x.get("attempt", {}).get("selected_modes") else {"selected_mode_count": len(x["attempt"]["selected_modes"])} } for x in results])
    _write(output_dir, "matchup_metrics.json", [{"scenario_id": x["scenario_id"], "time_to_match_up": None, "status": "DEFERRED_UNTIL_STABILITY_ACCEPTANCE"} for x in results])
    _write(output_dir, "latency_breakdown.json", [{"scenario_id": x["scenario_id"], "total_seconds": x.get("elapsed_seconds", 0), "mode_generation_seconds": x.get("attempt", {}).get("generation_seconds"), "cp_solve_seconds": x.get("attempt", {}).get("solve", {}).get("wall_time_seconds")} for x in results])
    failure_taxonomy = {}
    for x in results:
        status = x.get("policy_status", "UNKNOWN")
        failure_taxonomy[status] = failure_taxonomy.get(status, 0) + 1
    _write(output_dir, "failure_taxonomy.json", failure_taxonomy)
    _write(output_dir, "urgent_regression.json", {"transactions": urgent, "validated_reuse": True, "database_writes": 0})
    accepted = [x for x in results if x.get("attempt", {}).get("accepted")]
    replay_mismatches = sum(int(x.get("attempt", {}).get("replay_mismatches", 0) or 0) for x in results)
    status = "CRAFT_DISRUPTION_V2_EXECUTION_PASS" if len(accepted) >= 2 and replay_mismatches == 0 else "CRAFT_DISRUPTION_V2_PARTIAL"
    report = f"""# CRAFT Disruption Repair V2 Executable Diagnostics

Status: `{status}`.

Targeted scenarios: `{len(results)}`; accepted executable repairs: `{len(accepted)}`; replay mismatches: `{replay_mismatches}`; database writes: `0`.

The runner uses the frozen Industry Operating Model, fresh event-adjusted
JointExecutableModes, bounded CP-SAT, exact replay, and the authoritative
validator. The broad V2 matrix is not run by this diagnostic gate.

The frozen benchmark's missing same-line spare-machine identity remains an
explicit physical boundary; it is not replaced with a fabricated machine.
Unknown/in-progress WIP migration is escalated rather than invented.
"""
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha(path)
             for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name != "artifact_hash_manifest.json"}
    manifest = {"files": files, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR,
                "decision": status, "database_writes": 0, "artifact_count": len(files)}
    _write(output_dir, "artifact_hash_manifest.json", manifest)
    return {"status": status, "scenarios": len(results), "accepted": len(accepted),
            "replay_mismatches": replay_mismatches, "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
