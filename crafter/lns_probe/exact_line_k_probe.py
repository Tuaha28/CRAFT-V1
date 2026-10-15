"""E1.3.5 exact one-operation line/k realizability probe.

This module deliberately does not build a CP-SAT model.  It takes the frozen
CR phenotype, removes one operation from the resource calendars, and probes a
small deterministic set of event-aligned starts.  A candidate is accepted
only when the authoritative worker allocator and exact physical-machine
boundary checks agree on one common execution envelope.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds,
    authoritative_operation_work_minutes,
    authoritative_productive_seconds,
    authoritative_setup_seconds,
)
from crafter.contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from crafter.phenotype import validate
from ga.worker_allocation import allocate_crew

from .analysis import build_incumbent_index, seconds
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .transaction import frozen_incumbent, _outside_worker_state


E13_DIR = Path("outputs/crafter/e1_3_freedom_audit")
OUTPUT_DIR = Path("outputs/crafter/e1_3_5_exact_line_k_probe")


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _write_json(directory: Path, name: str, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True,
                         default=str) + "\n"
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_text(directory: Path, name: str, value: str):
    directory.mkdir(parents=True, exist_ok=True)
    payload = value if value.endswith("\n") else value + "\n"
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _outside_machine_reservations(problem, index, mutable_key):
    result = defaultdict(list)
    for row in index.rows:
        if str(row["operation_key"]) == mutable_key:
            continue
        start = seconds(row["start_minutes"])
        end = seconds(row["end_minutes"])
        for machine_id in row.get("machine_ids", ()):
            result[int(machine_id)].append((
                start, end, str(row["production_order_id"]),
                str(row["operation_key"])))
    for fixed in problem.resources.fixed_machine_commitments:
        result[int(fixed.machine_id)].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            str(fixed.production_order_id),
            f"fixed:{fixed.production_order_id}"))
    for intervals in result.values():
        intervals.sort()
    return result


def _machine_accepts(intervals, start, end, order_id):
    for left, right, previous_order, _key in intervals:
        if start < right and end > left:
            return False, "overlap"
        if right <= start and right + authoritative_changeover_seconds(
                previous_order, order_id) > start:
            return False, "incoming_transition"
        if end <= left and end + authoritative_changeover_seconds(
                order_id, previous_order) > left:
            return False, "outgoing_transition"
    return True, None


def _candidate_machine_pool(problem, operation, line_id):
    factory_id = problem.order_by_id[operation.order_id].factory_id
    return tuple(sorted(
        machine.machine_id for machine in problem.machines
        if machine.is_usable and machine.factory_id == factory_id
        and machine.line_id == int(line_id)
        and machine.supports(operation.required_capability)))


def _routing_bounds(problem, index, key):
    operation = problem.operation_by_key[key]
    predecessor = index.route_predecessor.get(key)
    successor = index.route_successor.get(key)
    release = max(0, int(problem.order_by_id[operation.order_id].release_seconds))
    predecessor_end = (seconds(index.by_key[predecessor]["end_minutes"])
                       if predecessor else release)
    successor_start = (seconds(index.by_key[successor]["start_minutes"])
                       if successor else problem.horizon_seconds)
    return {
        "release_seconds": release,
        "predecessor": predecessor,
        "predecessor_end_seconds": predecessor_end,
        "successor": successor,
        "successor_start_seconds": successor_start,
        "earliest_start_seconds": max(release, predecessor_end),
    }


def _duration_record(fixture, key, candidate_k, candidate_line):
    operation = fixture.problem.operation_by_key[key]
    detail = fixture.scheduler.op_details[key]
    source_row = fixture.df_routings[
        (fixture.df_routings["production_order_id"].astype(str)
         == str(operation.order_id))
        & (fixture.df_routings["operation_id"].astype(str)
           == str(operation.operation_id))
    ].iloc[0]
    quantity = int(source_row.get("target_quantity", 1) or 1)
    fraction = source_row.get("remaining_frac", 1.0)
    if fraction is None or pd.isna(fraction):
        fraction = 1.0
    fraction = min(max(float(fraction), 0.0), 1.0)
    remaining_quantity = max(1, int(round(quantity * fraction)))
    sam = float(source_row.get("sam", 30) or 30)
    if sam <= 0:
        sam = 30.0
    active = fixture.df_lines[fixture.df_lines["is_active"] == 1]
    mean_efficiency = float(active["target_efficiency_percentage"].mean()) / 100.0
    calibration = float(fixture.scheduler.duration_calibration_factor)
    serial_formula = calibration * sam * remaining_quantity / mean_efficiency
    serial_authoritative = float(detail["serial_processing_mins"])
    setup_minutes, productive_minutes = authoritative_operation_work_minutes(
        detail, int(candidate_k))
    productive_seconds = authoritative_productive_seconds(detail, int(candidate_k))
    line = fixture.problem.line_by_id[int(candidate_line)]
    line_efficiency = None
    line_rows = fixture.df_lines[fixture.df_lines["line_id"] == int(candidate_line)]
    if not line_rows.empty:
        line_efficiency = float(line_rows.iloc[0]["target_efficiency_percentage"])
    return {
        "operation_key": key,
        "line_id": int(candidate_line),
        "machine_count": int(candidate_k),
        "target_quantity": quantity,
        "remaining_fraction": fraction,
        "remaining_quantity": remaining_quantity,
        "sam": sam,
        "duration_calibration_factor": calibration,
        "mean_active_line_efficiency": mean_efficiency,
        "candidate_line_target_efficiency_percentage": line_efficiency,
        "serial_productive_minutes_formula": serial_formula,
        "serial_productive_minutes_authoritative": serial_authoritative,
        "serial_duration_formula_error_minutes": serial_formula - serial_authoritative,
        "setup_minutes": float(setup_minutes),
        "setup_seconds": authoritative_setup_seconds(detail),
        "productive_minutes": float(productive_minutes),
        "productive_seconds": productive_seconds,
        "duration_semantics": "authoritative split_operation_work + ceil(productive_minutes*60)",
        "line_efficiency_note": (
            "Current authoritative scheduler calibrates serial work with the mean active-line "
            "efficiency; candidate line efficiency is recorded but does not independently "
            "rescale duration."),
        "line_name": line.name,
    }


def _event_starts(fixture, index, key, line_id, candidate, machine_pool,
                  machine_reservations, worker_reservations, duration):
    bounds = _routing_bounds(fixture.problem, index, key)
    detail = fixture.scheduler.op_details[key]
    setup = authoritative_setup_seconds(detail)
    incumbent = seconds(index.by_key[key]["start_minutes"])
    values = {
        bounds["earliest_start_seconds"], incumbent,
        int(candidate.get("estimated_start_seconds") or 0),
    }
    # Incoming machine events are exact lower-bound candidates.  Outgoing
    # boundaries are tested after the authoritative worker walk, so they are
    # included as event points rather than approximated with a fixed suffix.
    for machine_id in machine_pool:
        for left, right, previous_order, _other in machine_reservations.get(
                machine_id, ()):
            values.add(max(0, right + authoritative_changeover_seconds(
                previous_order, fixture.problem.operation_by_key[key].order_id)))
            values.add(max(0, left - setup - duration["productive_seconds"]))
    for intervals in worker_reservations.values():
        for left, right in intervals:
            values.add(max(0, int(round(
                (right - fixture.problem.origin).total_seconds())) - setup))
            values.add(max(0, int(round(
                (left - fixture.problem.origin).total_seconds())) - setup))
    low = bounds["earliest_start_seconds"]
    high = bounds["successor_start_seconds"]
    filtered = sorted({int(v) for v in values if low <= int(v) <= high})
    # Keep all early events and a bounded deterministic tail.  The exact
    # allocator/calendar walk handles intra-event pauses; no second grid is
    # searched.
    if len(filtered) > 96:
        filtered = sorted(set(filtered[:64] + filtered[-32:]))
    return filtered


def _copy_worker_reservations(reservations):
    return {str(employee): list(intervals)
            for employee, intervals in reservations.items()}


def _machine_selection(problem, operation, line_id, candidate_k, start, end,
                       machine_reservations, incumbent_ids):
    pool = _candidate_machine_pool(problem, operation, line_id)
    feasible, reasons = [], Counter()
    for machine_id in pool:
        ok, reason = _machine_accepts(
            machine_reservations.get(machine_id, ()), start, end,
            operation.order_id)
        if ok:
            feasible.append(machine_id)
        else:
            reasons[reason] += 1
    feasible.sort(key=lambda machine_id: (
        0 if machine_id in set(incumbent_ids) else 1, machine_id))
    if len(feasible) < int(candidate_k):
        return None, {
            "reason": "insufficient physical machines",
            "candidate_machine_ids": list(pool),
            "available_machine_ids": feasible,
            "required_machine_count": int(candidate_k),
            "machine_failure_breakdown": dict(reasons),
        }
    return tuple(feasible[:int(candidate_k)]), None


def _probe_candidate(fixture, index, key, action_class, candidate,
                     baseline_rows, machine_cache, worker_base):
    operation = fixture.problem.operation_by_key[key]
    row = index.by_key[key]
    candidate_line = int(candidate["line_id"])
    candidate_k = int(candidate["k"])
    incumbent_ids = tuple(int(value) for value in row["machine_ids"])
    duration = _duration_record(fixture, key, candidate_k, candidate_line)
    machine_reservations = machine_cache[key]
    worker_reservations = worker_base[key]
    starts = _event_starts(
        fixture, index, key, candidate_line, candidate, _candidate_machine_pool(
            fixture.problem, operation, candidate_line), machine_reservations,
        worker_reservations, duration)
    bounds = _routing_bounds(fixture.problem, index, key)
    failures = []
    successes = []
    worker_successes = 0
    machine_failures = 0
    priority = float(fixture.problem.order_by_id[operation.order_id].priority_weight > 1)
    for start in starts:
        ready = fixture.problem.origin + timedelta(
            seconds=int(start + duration["setup_seconds"]))
        worker = allocate_crew(
            fixture.operator_index, operation.required_capability,
            candidate_line, candidate_k, ready,
            duration["productive_minutes"],
            _copy_worker_reservations(worker_reservations), None,
            priority=priority, state_is_normalized=True)
        if worker.get("shortage") is not None:
            shortage = worker["shortage"]
            failures.append({
                "start_seconds": start,
                "reason_class": "workers",
                "reason": shortage.get("limiting_factor", "worker shortage"),
                "detail": shortage,
            })
            continue
        worker_successes += 1
        segments = tuple(worker.get("segments", ()))
        productive_start = (int(round((segments[0][0] - fixture.problem.origin).total_seconds()))
                            if segments else start + duration["setup_seconds"])
        end = int(round((worker["end"] - fixture.problem.origin).total_seconds()))
        if start < bounds["earliest_start_seconds"]:
            failures.append({"start_seconds": start, "reason_class": "precedence",
                             "reason": "PRECEDENCE_BLOCKED"})
            continue
        if end > bounds["successor_start_seconds"]:
            failures.append({"start_seconds": start, "reason_class": "successor_boundary",
                             "reason": "SUCCESSOR_BOUNDARY_BLOCKED",
                             "end_seconds": end,
                             "successor_start_seconds": bounds["successor_start_seconds"]})
            continue
        if end > fixture.problem.horizon_seconds:
            failures.append({"start_seconds": start, "reason_class": "other",
                             "reason": "horizon boundary", "end_seconds": end})
            continue
        machines, machine_failure = _machine_selection(
            fixture.problem, operation, candidate_line, candidate_k, start, end,
            machine_reservations, incumbent_ids)
        if machine_failure is not None:
            machine_failures += 1
            machine_failure.update({"start_seconds": start, "end_seconds": end,
                                    "reason_class": "machines"})
            failures.append(machine_failure)
            continue
        successes.append({
            "status": "JOINT_EXACT_REALIZABLE",
            "action_class": action_class,
            "operation_key": key,
            "line_id": candidate_line,
            "machine_count": candidate_k,
            "machine_ids": list(machines),
            "worker_ids": [str(value) for value in worker["employees"]],
            "worker_segments": [
                {"start": left, "end": right}
                for left, right in segments],
            "start_seconds": start,
            "productive_start_seconds": productive_start,
            "end_seconds": end,
            "setup_seconds": duration["setup_seconds"],
            "productive_seconds": duration["productive_seconds"],
            "elapsed_seconds": end - start,
            "pause_seconds": max(0, end - productive_start - duration["productive_seconds"]),
            "candidate": candidate,
            "bounds": bounds,
        })
    if not successes:
        reason_counts = Counter(item.get("reason_class", "other") for item in failures)
        return {
            "status": "NOT_EXACT_REALIZABLE",
            "action_class": action_class,
            "operation_key": key,
            "line_id": candidate_line,
            "machine_count": candidate_k,
            "candidate": candidate,
            "duration": duration,
            "bounds": bounds,
            "event_start_count": len(starts),
            "failure_causes": dict(reason_counts),
            "failure_witnesses": failures[:12],
            "machine_status": (
                "MACHINE_NOT_PROVEN" if machine_failures else
                "MACHINE_NOT_REACHED"),
            "worker_status": (
                "WORKER_REALIZABLE" if worker_successes else
                "WORKER_NOT_PROVEN"),
        }
    return min(successes, key=lambda item: (
        item["end_seconds"], item["start_seconds"], item["machine_ids"],
        item["worker_ids"])) | {"duration": duration}


def _candidate_phenotype(problem, baseline, operation, result):
    by_key = {item.assignment.operation_key: item for item in baseline.operations}
    old = by_key[result["operation_key"]]
    assignment = replace(
        old.assignment,
        line_id=int(result["line_id"]),
        machine_ids=tuple(int(value) for value in result["machine_ids"]),
        machine_count=int(result["machine_count"]),
        start_seconds=int(result["start_seconds"]),
        productive_start_seconds=int(result["productive_start_seconds"]),
        end_seconds=int(result["end_seconds"]),
        setup_seconds=int(result["setup_seconds"]),
        productive_seconds=int(result["productive_seconds"]),
    )
    segments = tuple((
        datetime.fromisoformat(str(pair["start"])) if isinstance(pair["start"], str)
        else pair["start"],
        datetime.fromisoformat(str(pair["end"])) if isinstance(pair["end"], str)
        else pair["end"],
    ) for pair in result["worker_segments"])
    replacement = PhenotypeOperation(
        assignment=assignment,
        employee_ids=tuple(result["worker_ids"]),
        worker_segments=tuple((employee, segments)
                              for employee in result["worker_ids"]),
        sourcing_tier=str(result.get("worker_tier", "probe")),
    )
    return CrafterPhenotype(
        CrafterStatus.WORKER_FEASIBLE,
        tuple(replacement if item.assignment.operation_key == result["operation_key"]
              else item for item in baseline.operations),
    )


def validate_exact_result(fixture, baseline, result):
    if result.get("status") != "JOINT_EXACT_REALIZABLE":
        return {"status": "NOT_VALIDATED", "valid": False}, None
    candidate = _candidate_phenotype(
        fixture.problem, baseline,
        fixture.problem.operation_by_key[result["operation_key"]], result)
    validated = validate(fixture.problem, candidate, fixture.operator_index)
    report = validated.validation_report or {}
    return {
        "status": validated.status.value,
        "valid": bool(report.get("valid", False)),
        "violations": report.get("violation_count", 0),
        "phenotype_hash": validated.phenotype_hash,
        "metrics": dict(validated.comparison_metrics),
    }, validated


def select_probe_operations(e13_dir=E13_DIR, limit=20):
    payload = json.loads((Path(e13_dir) / "top_line_k_candidates.json").read_text())
    selected, seen = [], set()
    for rank, item in enumerate(payload["top"]):
        key = item["operation_key"]
        if key in seen:
            continue
        seen.add(key)
        selected.append({
            "rank": rank,
            "operation_key": key,
            "order_id": item["order_id"],
            "delivery_pressure": item["delivery_pressure"],
            "score": item["score"],
            "selection_rule": "top unique LINE_K candidates by E1.3 score; ties already deterministic",
        })
        if len(selected) >= int(limit):
            break
    return selected


def build_candidates(e13_dir, selected):
    root = Path(e13_dir)
    lines = {x["operation_key"]: x for x in json.loads((root / "line_freedom.json").read_text())}
    ks = {x["operation_key"]: x for x in json.loads((root / "k_freedom.json").read_text())}
    lks = {x["operation_key"]: x for x in json.loads((root / "line_k_opportunities.json").read_text())}
    output = {}
    for selected_row in selected:
        key = selected_row["operation_key"]
        line_row, k_row, lk_row = lines[key], ks[key], lks[key]
        incumbent_line = int(line_row["incumbent_line_id"])
        incumbent_k = int(line_row["incumbent_k"])
        incumbent_screen = next(
            (item.get("same_k_screen") for item in line_row["line_estimates"]
             if item["is_incumbent"] and item.get("same_k_screen") is not None),
            None)
        incumbent_screen_finish = (
            incumbent_screen.get("estimated_finish_seconds")
            if incumbent_screen else None)
        line_options = []
        for item in line_row["line_estimates"]:
            screen = item.get("same_k_screen")
            if item["is_incumbent"] or screen is None:
                continue
            line_options.append({
                "line_id": int(item["line_id"]), "k": incumbent_k,
                "estimated_start_seconds": screen["earliest_start_seconds"],
                "estimated_finish_seconds": screen["estimated_finish_seconds"],
                "estimated_gain_seconds": max(
                    0, (incumbent_screen_finish or 0)
                    - (screen["estimated_finish_seconds"] or 0)),
                "source": "E1.3 line_freedom same-k screen",
            })
        line_options.sort(key=lambda x: (
            x["estimated_finish_seconds"], x["line_id"]))
        k_options = []
        for item in k_row["k_estimates"]:
            if int(item["k"]) == incumbent_k:
                continue
            k_options.append({
                "line_id": incumbent_line, "k": int(item["k"]),
                "estimated_start_seconds": item["earliest_start_seconds"],
                "estimated_finish_seconds": item["estimated_finish_seconds"],
                "estimated_gain_seconds": max(
                    0, (k_row["incumbent_estimated_finish_seconds"] or 0)
                    - (item["estimated_finish_seconds"] or 0)),
                "source": "E1.3 k_freedom screened k estimate",
            })
        k_options.sort(key=lambda x: (x["estimated_finish_seconds"], x["k"]))
        lk_options = []
        seen_lk = set()
        for item in lk_row["top_promising_combinations"]:
            pair = (int(item["line_id"]), int(item["k"]))
            if pair in seen_lk or pair == (incumbent_line, incumbent_k):
                continue
            seen_lk.add(pair)
            lk_options.append({
                "line_id": pair[0], "k": pair[1],
                "estimated_start_seconds": item["earliest_start_seconds"],
                "estimated_finish_seconds": item["estimated_finish_seconds"],
                "estimated_gain_seconds": max(
                    0, (lk_row["incumbent"]["estimated_finish_seconds"] or 0)
                    - (item["estimated_finish_seconds"] or 0)),
                "source": "E1.3 top promising line+k combination",
            })
        output[key] = {
            "incumbent": {"line_id": incumbent_line, "k": incumbent_k},
            "LINE_ONLY": line_options[:3],
            "K_ONLY": k_options,
            "LINE_K": lk_options[:5],
        }
    return output


def action_summary(results, action_class):
    rows = [x for x in results if x["action_class"] == action_class]
    exact = [x for x in rows if x.get("exact_status") == "EXACT_REALIZABLE"]
    useful = [x for x in exact if x.get("exact_useful")]
    gains = sorted(x["exact_finish_gain_seconds"] for x in useful)
    return {
        "action_class": action_class,
        "operations_tested": len({x["operation_key"] for x in rows}),
        "alternatives_tested": len(rows),
        "exact_machine_realizability_rate": (
            sum(x.get("machine_status") == "MACHINE_REALIZABLE" for x in rows)
            / len(rows) if rows else 0),
        "exact_worker_realizability_rate": (
            sum(x.get("worker_status") == "WORKER_REALIZABLE" for x in rows)
            / len(rows) if rows else 0),
        "joint_exact_realizability_rate": len(exact) / len(rows) if rows else 0,
        "operations_with_exact_realizable_candidate": len({x["operation_key"] for x in exact}),
        "operations_with_exact_useful_candidate": len({x["operation_key"] for x in useful}),
        "median_exact_useful_finish_gain_seconds": (
            gains[len(gains)//2] if gains else 0),
        "maximum_exact_useful_finish_gain_seconds": max(gains, default=0),
        "failure_causes": dict(Counter(
            cause for x in rows if x.get("exact_status") != "EXACT_REALIZABLE"
            for cause in x.get("failure_causes", {}))),
    }


def run(output_dir=OUTPUT_DIR):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    fixture = __import__("crafter.real_data_adapter", fromlist=["build_real_window_fixture"]).build_real_window_fixture(order_limit=50)
    payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = payload["decoder_details"]["phenotype_operations"]
    baseline = frozen_incumbent(fixture.problem, rows)
    validated_baseline = validate(fixture.problem, baseline, fixture.operator_index)
    if not (
        fixture.source_fingerprint == EXPECTED_SOURCE
        and validated_baseline.phenotype_hash == EXPECTED_CR
        and validated_baseline.status == CrafterStatus.VALIDATED_FEASIBLE
        and validated_baseline.validation_report["valid"]
    ):
        raise RuntimeError("E1.3.5 frozen source or CR phenotype mismatch")
    index = build_incumbent_index(
        fixture.problem, rows, fixture.scheduler.op_details)
    selected = select_probe_operations()
    candidates = build_candidates(E13_DIR, selected)
    source_identity = {
        "snapshot": fixture.source_metadata["snapshot_name"],
        "source_fingerprint": fixture.source_fingerprint,
        "expected_source_fingerprint": EXPECTED_SOURCE,
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations),
        "cr_phenotype_hash": validated_baseline.phenotype_hash,
        "expected_cr_phenotype_hash": EXPECTED_CR,
        "validator_status": validated_baseline.status.value,
        "validator_valid": validated_baseline.validation_report["valid"],
        "database_writes": 0,
        "outside_operations_frozen": 430,
    }
    _write_json(output_dir, "source_identity.json", source_identity)
    _write_json(output_dir, "selected_probe_operations.json", {
        "selection_rule": "top 20 unique LINE_K candidates from frozen E1.3 top list by existing score",
        "operations": selected,
    })
    _write_json(output_dir, "candidate_alternatives.json", candidates)

    machine_cache, worker_base = {}, {}
    for key in candidates:
        machine_cache[key] = _outside_machine_reservations(
            fixture.problem, index, key)
        worker_base[key] = _outside_worker_state(
            fixture.problem, index, {key})

    duration_records = []
    machine_records, worker_records, joint_records = [], [], []
    all_results = []
    for selected_row in selected:
        key = selected_row["operation_key"]
        for action_class in ("LINE_ONLY", "K_ONLY", "LINE_K"):
            for ordinal, candidate in enumerate(candidates[key][action_class]):
                duration = _duration_record(
                    fixture, key, candidate["k"], candidate["line_id"])
                duration_records.append({
                    "operation_key": key, "action_class": action_class,
                    "candidate_ordinal": ordinal, "candidate": candidate,
                    "duration": duration,
                })
                result = _probe_candidate(
                    fixture, index, key, action_class, candidate, rows,
                    machine_cache, worker_base)
                if result.get("status") == "JOINT_EXACT_REALIZABLE":
                    result["exact_status"] = "EXACT_REALIZABLE"
                    result["machine_status"] = "MACHINE_REALIZABLE"
                    result["worker_status"] = "WORKER_REALIZABLE"
                    result["screen_vs_exact"] = {
                        "estimated_start_seconds": candidate.get("estimated_start_seconds"),
                        "estimated_finish_seconds": candidate.get("estimated_finish_seconds"),
                        "estimated_gain_seconds": candidate.get("estimated_gain_seconds", 0),
                        "exact_start_seconds": result["start_seconds"],
                        "exact_finish_seconds": result["end_seconds"],
                        "exact_finish_gain_seconds": max(
                            0, seconds(index.by_key[key]["end_minutes"])
                            - result["end_seconds"]),
                    }
                    result["exact_finish_gain_seconds"] = result["screen_vs_exact"]["exact_finish_gain_seconds"]
                    result["exact_start_gain_seconds"] = max(
                        0, seconds(index.by_key[key]["start_minutes"])
                        - result["start_seconds"])
                    result["exact_useful"] = result["exact_finish_gain_seconds"] > 0
                    validation, validated = validate_exact_result(
                        fixture, validated_baseline, result)
                    result["validation"] = validation
                    result["exact_status"] = (
                        "EXACT_REALIZABLE" if validation["valid"]
                        else "NOT_VALIDATED")
                    result["worker_tier"] = "authoritative_allocate_crew"
                    if validation["valid"]:
                        result["exact_useful"] = result["exact_finish_gain_seconds"] > 0
                else:
                    result["exact_status"] = "NOT_EXACT_REALIZABLE"
                    result.setdefault("machine_status", "MACHINE_NOT_PROVEN")
                    result.setdefault("worker_status", "WORKER_NOT_PROVEN")
                    result["exact_useful"] = False
                all_results.append(result)

    for result in all_results:
        machine_records.append({
            "operation_key": result["operation_key"],
            "action_class": result["action_class"],
            "status": result.get("machine_status", "MACHINE_NOT_PROVEN"),
            "machine_ids": result.get("machine_ids", []),
            "failure_causes": result.get("failure_causes", {}),
        })
        worker_records.append({
            "operation_key": result["operation_key"],
            "action_class": result["action_class"],
            "status": result.get("worker_status", "WORKER_NOT_PROVEN"),
            "worker_ids": result.get("worker_ids", []),
            "failure_causes": result.get("failure_causes", {}),
        })
        joint_records.append(result)

    summaries = {action: action_summary(all_results, action)
                 for action in ("LINE_ONLY", "K_ONLY", "LINE_K")}
    _write_json(output_dir, "duration_recomputations.json", duration_records)
    _write_json(output_dir, "machine_realizability.json", machine_records)
    _write_json(output_dir, "worker_realizability.json", worker_records)
    _write_json(output_dir, "joint_execution_results.json", joint_records)
    _write_json(output_dir, "screen_vs_exact_accuracy.json", [
        {k: row[k] for k in row if k in {
            "operation_key", "action_class", "candidate", "exact_status",
            "screen_vs_exact", "failure_causes", "exact_useful"}}
        for row in all_results])
    for action in ("LINE_ONLY", "K_ONLY", "LINE_K"):
        _write_json(output_dir, f"{action.lower()}_summary.json", summaries[action])
    failure_causes = Counter(
        cause for row in all_results if row.get("exact_status") != "EXACT_REALIZABLE"
        for cause in row.get("failure_causes", {}))
    _write_json(output_dir, "failure_causes.json", dict(failure_causes))

    top_scores = {row["operation_key"]: row["score"] for row in selected}
    useful_by_key = defaultdict(list)
    for row in all_results:
        if row.get("exact_useful") and row.get("validation", {}).get("valid"):
            useful_by_key[row["operation_key"]].append(row)
    graph_signal = {
        "status": "GRAPH_SIGNAL_PROMISING" if useful_by_key else "GRAPH_SIGNAL_INCONCLUSIVE",
        "selected_operations": len(selected),
        "operations_with_exact_realizable": len({row["operation_key"] for row in all_results if row.get("exact_status") == "EXACT_REALIZABLE"}),
        "operations_with_exact_useful": len(useful_by_key),
        "graph_score_to_exact_useful": [
            {"operation_key": key, "graph_score": top_scores[key],
             "best_exact_gain_seconds": max(
                 row["exact_finish_gain_seconds"] for row in values)}
            for key, values in sorted(useful_by_key.items())],
        "disclosure": "Small deterministic probe; no statistical superiority claim.",
    }
    _write_json(output_dir, "graph_signal_evaluation.json", graph_signal)

    useful_counts = {action: summaries[action]["operations_with_exact_useful_candidate"]
                     for action in summaries}
    if not any(useful_counts.values()):
        next_stage = "NO_RESOURCE_FREEDOM_PROVEN"
    else:
        best_action = max(
            summaries,
            key=lambda action: (
                useful_counts[action],
                summaries[action]["maximum_exact_useful_finish_gain_seconds"],
                -summaries[action]["alternatives_tested"],
            ))
        next_stage = {
            "LINE_ONLY": "PROCEED_WITH_LINE_ONLY",
            "K_ONLY": "PROCEED_WITH_K_ONLY",
            "LINE_K": "PROCEED_WITH_LINE_K",
        }[best_action]
    _write_json(output_dir, "next_stage_decision.json", {
        "decision": next_stage,
        "action_class_useful_operation_counts": useful_counts,
        "selection_priority": [
            "high-impact operations with exact useful alternatives",
            "exact finish gain", "machine feasibility", "worker feasibility",
            "model complexity", "schedule churn"],
    })

    hashes = {}
    for path in sorted(output_dir.glob("*.json")):
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = (
        "# CRAFT E1.3.5 Exact Line/K Probe\n\n"
        f"- Source fingerprint: `{fixture.source_fingerprint}`\n"
        f"- CR phenotype: `{validated_baseline.phenotype_hash}`\n"
        f"- Probe operations: {len(selected)}\n"
        f"- LINE_ONLY exact useful operations: {summaries['LINE_ONLY']['operations_with_exact_useful_candidate']}\n"
        f"- K_ONLY exact useful operations: {summaries['K_ONLY']['operations_with_exact_useful_candidate']}\n"
        f"- LINE_K exact useful operations: {summaries['LINE_K']['operations_with_exact_useful_candidate']}\n"
        f"- Next decision: `{next_stage}`\n\n"
        "All 430 non-tested operations remained frozen. No candidate was committed.\n"
    )
    hashes["final_report.md"] = _write_text(output_dir, "final_report.md", report)
    manifest = {
        "files": hashes,
        "source_fingerprint": fixture.source_fingerprint,
        "phenotype_hash": validated_baseline.phenotype_hash,
        "probe_operations": len(selected),
        "database_writes": 0,
    }
    _write_json(output_dir, "artifact_hash_manifest.json", manifest)
    return {"output_dir": str(output_dir), "next_stage": next_stage,
            "summaries": summaries, "graph_signal": graph_signal}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
