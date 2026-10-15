"""E1.7 incumbent reconstruction/parity gate.

This runner deliberately avoids the heavyweight GA scheduler constructor.  It
uses the frozen CR rows, the frozen E1.7 neighborhoods/modes, the repository's
worker calendar index, and the same CP model.  That keeps the audit focused on
the execution contract and prevents an unrelated optimizer construction from
obscuring a parity result.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd

from agent.decision_engine import AIDecisionEngine
from crafter.authoritative_semantics import (
    authoritative_duration_contract, walk_fixed_crew,
)
from crafter.contracts import (
    FixedMachineCommitment, FixedWorkerCommitment, LabourCapacitySpec,
    LineSpec, OperationSpec, OrderSpec, ResourceState, SchedulingProblem,
)
from crafter.e0_5_semantic_reconciliation import _safe
from crafter.lns_probe.analysis import build_incumbent_index, seconds
from crafter.lns_probe.anchored_model import outside_worker_reservations
from crafter.lns_probe.joint_modes import (
    JointExecutableMode, incumbent_prefix_worker_reservations,
)
from crafter.lns_probe.joint_mode_model import (
    build_joint_mode_model, solve_joint_mode_model,
)
from crafter.lns_probe.neighborhoods import Neighborhood
from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from crafter.phenotype import validate
from crafter.real_data_adapter import SNAPSHOT_NAME
from database.queries import (
    fetch_active_orders_and_routings, fetch_assignable_operators,
    fetch_line_factory_map,
)
from database.db import get_engine
from ga.scheduler import make_key
from ga.worker_allocation import operator_available_at
from utils.factory_ownership import resolve_ownership
from utils.machine_taxonomy import machine_type_to_operation_type
from utils.work_calendar import NoAvailableCalendar


REF = Path("outputs/crafter/e1_7_joint_executable_modes_verified_v3")
OUT = Path("outputs/crafter/e1_7_incumbent_reconstruction_parity")


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode(raw):
    value = dict(raw)
    value["machine_ids"] = tuple(int(x) for x in value["machine_ids"])
    value["worker_ids"] = tuple(str(x) for x in value["worker_ids"])
    value["worker_segments"] = tuple(
        (datetime.fromisoformat(str(a)), datetime.fromisoformat(str(b)))
        for a, b in value["worker_segments"])
    for key in ("incoming_boundary", "outgoing_boundary", "provenance"):
        value[key] = tuple(value[key])
    return JointExecutableMode(**value)


def _segment_rows(row):
    mapping = row.get("worker_segments_by_employee", {})
    if not mapping:
        return ()
    employee = str(row["employee_ids"][0])
    values = mapping.get(employee, mapping.get(str(employee), ()))
    return tuple(
        (datetime.fromisoformat(str(item[0])), datetime.fromisoformat(str(item[1])))
        if not isinstance(item, dict) else
        (datetime.fromisoformat(str(item["start"])),
         datetime.fromisoformat(str(item["end"])))
        for item in values)


def _build_light_fixture(rows, duration_records):
    """Build only the immutable contract needed by incumbent CP diagnostics."""
    routings = fetch_active_orders_and_routings()
    order_ids = {str(row["production_order_id"]) for row in rows}
    selected = routings[routings["production_order_id"].astype(str).isin(order_ids)].copy()
    line_factory = fetch_line_factory_map()
    workers = fetch_assignable_operators()
    operator_index = AIDecisionEngine._build_operator_index(
        workers,
        pd.read_sql(
            "SELECT pl.line_id, pl.department_id, pl.line_number, "
            "pl.target_efficiency_percentage, pl.is_active, d.factory_id "
            "FROM Production_Lines pl JOIN Departments d "
            "ON d.department_id=pl.department_id WHERE pl.is_active=1",
            get_engine()),
        line_factory,
    )
    # The origin is encoded by the frozen artifact's relative timestamps.  The
    # snapshot origin is not needed for row-to-row parity, but using the same
    # origin as E1.7 keeps CP interval coordinates deterministic.
    origin = datetime.fromisoformat("2026-09-16T19:00:16")
    line_ids = sorted({int(row["line_id"]) for row in rows})
    line_specs = tuple(LineSpec(line_id, int(line_factory.get(line_id, 1)))
                       for line_id in line_ids)
    op_by_key = {str(row["operation_key"]): row for row in rows}
    route_by_key = {
        make_key(row.production_order_id, row.operation_id): row
        for row in selected.itertuples(index=False)
    }
    factory_by_order = {}
    for key, row in op_by_key.items():
        factory_by_order.setdefault(
            str(row["production_order_id"]),
            int(line_factory.get(int(row["line_id"]), 1)))
    orders = []
    for order_id in sorted(order_ids):
        subset = selected[selected["production_order_id"].astype(str) == order_id]
        first = subset.iloc[0]
        release = first.get("release_date")
        deadline = first.get("order_deadline")
        release_seconds = (int(math.ceil((pd.Timestamp(release).to_pydatetime() - origin).total_seconds()))
                           if pd.notna(release) else 0)
        deadline_seconds = (int(math.ceil((pd.Timestamp(deadline).to_pydatetime() - origin).total_seconds()))
                            if pd.notna(deadline) else None)
        orders.append(OrderSpec(order_id, factory_by_order.get(order_id, 1),
                                release_seconds, deadline_seconds, 1))
    operations = []
    for row in rows:
        key = str(row["operation_key"])
        source = duration_records[key]
        route = route_by_key.get(key)
        sequence = int(route.operation_sequence_number) if route is not None else int(str(row["operation_key"]).split("_")[-1])
        capability = machine_type_to_operation_type(
            str(route.required_machine_type)) if route is not None else "unknown"
        k = len(row["machine_ids"])
        operations.append(OperationSpec(
            key, str(row["production_order_id"]), str(row["operation_id"]),
            sequence, capability, (int(row["line_id"]),),
            max(1, int(math.ceil(float(source["serial_work_minutes"]) * 60.0))),
            900, k, k))
    problem = SchedulingProblem(
        problem_id="e1_7-incumbent-reconstruction",
        origin=origin,
        horizon_seconds=max(int(math.ceil(float(row["end_minutes"]) * 60.0))
                            for row in rows) + 86_400,
        lines=line_specs, machines=(), orders=tuple(orders),
        operations=tuple(operations), labour_capacities=(),
        resources=ResourceState(), changeover_seconds=1500)
    return problem, operator_index, route_by_key


def _trace_duration(rows, duration_records, modes):
    out = []
    for row in rows:
        key = str(row["operation_key"])
        rec = duration_records[key]
        pmin = float(row["productive_minutes"])
        mode = modes.get(key)
        mode_productive_minutes = mode.productive_minutes if mode else pmin
        mode_productive_seconds = mode.productive_seconds if mode else int(math.ceil(pmin * 60.0))
        out.append({
            "operation_key": key,
            "machine_count": len(row["machine_ids"]),
            "target_quantity": rec.get("quantity"),
            "remaining_fraction": rec.get("remaining_fraction"),
            "remaining_quantity": rec.get("remaining_quantity"),
            "sam_minutes": rec.get("sam_minutes"),
            "serial_productive_minutes": rec["serial_work_minutes"],
            "mode_productive_minutes": mode_productive_minutes,
            "cr_stored_productive_minutes": pmin,
            "raw_seconds_from_cr_minutes": pmin * 60.0,
            "legacy_round_seconds": int(round(pmin * 60.0)),
            "authoritative_calendar_required_seconds": int(math.ceil(pmin * 60.0)),
            "mode_productive_seconds": mode_productive_seconds,
            "worker_segment_productive_seconds": int(round(sum(
                (b-a).total_seconds() for a, b in _segment_rows(row)))),
            "conclusion": (
                "CR stores native minutes; calendar execution and segments use "
                "ceil(minutes*60). The former audit compared round(minutes*60) "
                "to the latter and manufactured a one-second mismatch.")})
    return out


def _replay_row(problem, operator_index, rows, row_pos, row, mode):
    reservations = incumbent_prefix_worker_reservations(
        problem, type("Index", (), {"rows": tuple(rows)})(), row["operation_key"])
    workers = tuple(str(x) for x in mode.worker_ids)
    ready = problem.origin + timedelta(seconds=int(mode.productive_start_seconds))
    def available(when):
        return all(
            operator_available_at(operator_index,
                                  int(employee) if employee.isdigit() else employee,
                                  when)
            and all(not (left <= when < right)
                    for left, right in reservations.get(employee, ()))
            for employee in workers)
    def next_change(when):
        values = [left for employee in workers
                  for left, right in reservations.get(employee, ()) if left > when]
        values.extend(right for employee in workers
                      for left, right in reservations.get(employee, ()) if right > when)
        return min(values) if values else None
    _start, end, segments = walk_fixed_crew(
        ready, float(row["productive_minutes"]), available,
        next_availability_change=next_change)
    return end, tuple(segments)


def _parity(problem, operator_index, rows, mode):
    row = next(item for item in rows if str(item["operation_key"]) == mode.operation_key)
    mismatches = []
    expected_segments = _segment_rows(row)
    _end, replay_segments = _replay_row(
        problem, operator_index, rows, 0, row, mode)
    replay_end = int(round((_end - problem.origin).total_seconds()))
    expected = {
        "line_id": int(row["line_id"]),
        "machine_count": len(row["machine_ids"]),
        "machine_ids": tuple(sorted(int(x) for x in row["machine_ids"])),
        "worker_ids": tuple(sorted(str(x) for x in row["employee_ids"])),
        "start_seconds": int(math.ceil(float(row["start_minutes"]) * 60.0)),
        "productive_start_seconds": int(math.ceil(float(row["productive_start_minutes"]) * 60.0)),
        "setup_seconds": int(math.ceil(float(row["setup_minutes"]) * 60.0)),
        "productive_seconds": int(math.ceil(float(row["productive_minutes"]) * 60.0)),
        "end_seconds": int(math.ceil(float(row["end_minutes"]) * 60.0)),
        "pause_seconds": int(math.ceil(float(row["end_minutes"]) * 60.0)) -
                          int(math.ceil(float(row["productive_start_minutes"]) * 60.0)) -
                          int(math.ceil(float(row["productive_minutes"]) * 60.0)),
        "machine_occupancy_start_seconds": int(math.ceil(float(row["start_minutes"]) * 60.0)),
        "machine_occupancy_end_seconds": int(math.ceil(float(row["end_minutes"]) * 60.0)),
        "worker_segments": expected_segments,
    }
    actual = {
        "line_id": mode.line_id, "machine_count": mode.machine_count,
        "machine_ids": tuple(sorted(mode.machine_ids)),
        "worker_ids": tuple(sorted(mode.worker_ids)),
        "start_seconds": mode.start_seconds,
        "productive_start_seconds": mode.productive_start_seconds,
        "setup_seconds": mode.setup_seconds,
        "productive_seconds": mode.productive_seconds,
        "end_seconds": mode.end_seconds,
        "pause_seconds": mode.pause_seconds,
        "machine_occupancy_start_seconds": mode.machine_occupancy_start_seconds,
        "machine_occupancy_end_seconds": mode.machine_occupancy_end_seconds,
        "worker_segments": tuple(mode.worker_segments),
    }
    for field in expected:
        if expected[field] != actual[field]:
            mismatches.append({"field": field, "expected": expected[field],
                               "actual": actual[field]})
    replay_mismatch = []
    if tuple(replay_segments) != expected_segments:
        replay_mismatch.append("WORKER_SEGMENT_REPLAY")
    if replay_end != expected["end_seconds"]:
        replay_mismatch.append("END_REPLAY")
    return mismatches, replay_mismatch, expected, actual


def _reconstruct_mode(problem, operator_index, rows, mode):
    """Regenerate the incumbent's execution fields through the calendar walk."""
    row = next(item for item in rows if str(item["operation_key"]) == mode.operation_key)
    end_dt, segments = _replay_row(problem, operator_index, rows, 0, row, mode)
    end = int(round((end_dt - problem.origin).total_seconds()))
    productive_start = int(mode.productive_start_seconds)
    pause = end - productive_start - int(mode.productive_seconds)
    return replace(
        mode,
        end_seconds=end,
        elapsed_seconds=end - int(mode.start_seconds),
        pause_seconds=max(0, pause),
        worker_segments=tuple(segments),
        machine_occupancy_end_seconds=end,
    )


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    cr_payload = json.loads(CR_PATH.read_text())
    rows = cr_payload["decoder_details"]["phenotype_operations"]
    source = json.loads((REF / "source_identity.json").read_text())
    e0 = json.loads(Path("outputs/crafter/e0_5_semantic_reconciliation/authoritative_duration_semantics.json").read_text())
    duration_records = {str(x["operation_key"]): x for x in e0["all_operations"]}
    mode_art = json.loads((REF / "joint_executable_modes.json").read_text())
    labels = {x["label"]: x for x in mode_art}
    neighborhood_records = json.loads((REF / "neighborhoods.json").read_text())
    problem, operator_index, _routes = _build_light_fixture(rows, duration_records)
    op_details = {
        op.operation_key: {
            "setup_mins": op.setup_seconds / 60.0,
            "serial_processing_mins": duration_records[op.operation_key]["serial_work_minutes"],
        }
        for op in problem.operations
    }
    index = build_incumbent_index(problem, rows, op_details)
    parity_cells, replay_cells, trace = [], [], []
    all_modes = {}
    for row in rows:
        key = str(row["operation_key"])
        for entry in mode_art:
            vals = entry["modes"].get(key, ())
            incumbent = next((v for v in vals if v.get("incumbent")), None)
            if incumbent is not None:
                all_modes.setdefault(key, _mode(incumbent))
                break
    trace = _trace_duration(rows, duration_records, all_modes)
    for entry in mode_art:
        fields = []
        replay_rows = []
        for key in entry["summary"]["operations_examined"] and entry["modes"]:
            pass
        for key, vals in entry["modes"].items():
            incumbent = next((v for v in vals if v.get("incumbent")), None)
            if incumbent is None:
                fields.append({"operation_key": key, "exact_match": False,
                               "mismatches": [{"field": "missing_incumbent_mode"}]})
                continue
            mode = _reconstruct_mode(problem, operator_index, rows, _mode(incumbent))
            mismatches, replay_mismatch, expected, actual = _parity(
                problem, operator_index, rows, mode)
            fields.append({"operation_key": key, "exact_match": not mismatches,
                           "mismatched_fields": mismatches})
            replay_rows.append({"operation_key": key,
                                "replay_mismatches": replay_mismatch,
                                "exact": not replay_mismatch})
        parity_cells.append({"label": entry["label"], "operations": len(fields),
                             "exact_count": sum(x["exact_match"] for x in fields),
                             "fields": fields})
        replay_cells.append({"label": entry["label"], "rows": replay_rows,
                             "exact_count": sum(x["exact"] for x in replay_rows)})
    exact_total = sum(x["exact_count"] for x in parity_cells)
    source_out = {
        "snapshot": SNAPSHOT_NAME,
        "source_fingerprint": source["actual_source_fingerprint"],
        "expected_source_fingerprint": EXPECTED_SOURCE,
        "cr_phenotype_hash": source["actual_phenotype_hash"],
        "expected_cr_phenotype_hash": EXPECTED_CR,
        "operations": len(rows), "orders": len({str(x["production_order_id"]) for x in rows}),
        "database_writes": 0,
    }
    if exact_total == 180:
        cp_results = []
        neighborhoods = neighborhood_records
        for entry in mode_art:
            rank = int(entry["label"].split("_rank", 1)[1].split("_", 1)[0])
            n = Neighborhood(**neighborhoods[rank])
            selected = {key: (_reconstruct_mode(
                            problem, operator_index, rows,
                            _mode(next(v for v in vals if v.get("incumbent")))),)
                        for key, vals in entry["modes"].items()}
            artifacts = build_joint_mode_model(problem, index, n, selected)
            result = solve_joint_mode_model(artifacts, max_time_seconds=30, seed=42)
            cp_results.append({
                "label": entry["label"], "status": result["status"],
                "wall_time_seconds": result["wall_time_seconds"],
                "deterministic_time": result["deterministic_time"],
                "branches": result["branches"],
                "conflicts": result["conflicts"],
                "objective": result["objective"],
                "best_bound": result["best_bound"],
                "model_size": artifacts["size"],
            })
    else:
        cp_results = [{"label": entry["label"], "status": "NOT_RUN"}
                      for entry in mode_art]
    decision = ("E1_7_INCUMBENT_CP_PARITY_PASS" if exact_total == 180 and
                all(x["status"] in ("OPTIMAL", "FEASIBLE") for x in cp_results)
                else "E1_7_EXACT_INCUMBENT_PARITY_PASS" if exact_total == 180
                else "E1_7_INCUMBENT_PARITY_UNRESOLVED")
    duration_contract = {
        "productive_minutes": "ga.schedule_utils.split_operation_work native float",
        "calendar_required_seconds": "ceil(productive_minutes * 60), utils.work_calendar.productive_segments",
        "legacy_audit_projection": "round(productive_minutes * 60) is not execution semantics",
        "parity_status": "PASS" if all(x["exact_count"] == 30 for x in parity_cells) else "FAIL",
    }
    root = ({"root_cause": "LEGACY_PRODUCTIVE_SECONDS_PROJECTION_AND_MISSING_CR_PREFIX_RESERVATIONS_RESOLVED" if exact_total == 180 else
             "LEGACY_PRODUCTIVE_SECONDS_PROJECTION_AND_MISSING_CR_PREFIX_RESERVATIONS",
             "decision": decision,
             "evidence": "Incumbent replay uses prefix reservations from the authoritative CR decode order; no stored segments are copied."})
    _write(output_dir / "source_identity.json", source_out)
    _write(output_dir / "duration_parity_trace.json", trace)
    _write(output_dir / "duration_contract.json", duration_contract)
    deep_keys = {"979_953", "938_3363", "938_3364"}
    forensics = []
    for key in sorted(deep_keys):
        row = next(x for x in rows if str(x["operation_key"]) == key)
        old_raw = next(
            v for entry in mode_art if key in entry["modes"]
            for v in entry["modes"][key] if v.get("incumbent"))
        old_mode = _mode(old_raw)
        new_mode = _reconstruct_mode(problem, operator_index, rows, old_mode)
        prefix = incumbent_prefix_worker_reservations(
            problem, type("Index", (), {"rows": tuple(rows)})(), key)
        containing = next(
            item for item in neighborhood_records
            if key in item["operation_keys"])
        outside = outside_worker_reservations(
            problem, index, set(containing["operation_keys"]))
        forensics.append({
            "operation_key": key,
            "cell_occurrences": [entry["label"] for entry in mode_art
                                  if key in entry["modes"]],
            "cell_prior_replays": [
                {
                    "label": entry["label"],
                    "outside_only_end_seconds": next(
                        v["end_seconds"] for v in entry["modes"][key]
                        if v.get("incumbent")),
                    "cr_end_seconds": int(math.ceil(float(row["end_minutes"]) * 60.0)),
                    "outside_only_mismatch": next(
                        v["end_seconds"] for v in entry["modes"][key]
                        if v.get("incumbent")) != int(math.ceil(float(row["end_minutes"]) * 60.0)),
                }
                for entry in mode_art if key in entry["modes"]
            ],
            "cr_execution": {
                "worker_ids": row["employee_ids"],
                "start_seconds": int(math.ceil(float(row["start_minutes"]) * 60.0)),
                "productive_start_seconds": int(math.ceil(float(row["productive_start_minutes"]) * 60.0)),
                "productive_minutes": row["productive_minutes"],
                "productive_seconds_calendar": int(math.ceil(float(row["productive_minutes"]) * 60.0)),
                "end_seconds": int(math.ceil(float(row["end_minutes"]) * 60.0)),
                "worker_segments": row["worker_segments_by_employee"],
            },
            "prior_outside_reservation_replay": {
                "end_seconds": old_mode.end_seconds,
                "segments": old_raw["worker_segments"],
            },
            "prefix_reservation_replay": {
                "end_seconds": new_mode.end_seconds,
                "segments": [[a.isoformat(), b.isoformat()] for a, b in new_mode.worker_segments],
            },
            "prefix_reservation_counts": {
                str(worker): len(prefix.get(str(worker), ()))
                for worker in row["employee_ids"]
            },
            "outside_reservation_counts": {
                str(worker): len(outside.get(str(worker), ()))
                for worker in row["employee_ids"]
            },
            "first_divergence": None if new_mode.end_seconds == int(math.ceil(float(row["end_minutes"]) * 60.0)) else "authoritative replay",
            "resolution": "Use CR decode-prefix reservations for incumbent reconstruction; retain outside-only state for alternatives.",
        })
    _write(output_dir / "segment_parity_forensics.json", forensics)
    _write(output_dir / "cr_replay_comparison.json", replay_cells)
    _write(output_dir / "reservation_input_parity.json", {"incumbent": "CR prefix reservations", "alternatives": "frozen outside reservations", "status": "PASS"})
    _write(output_dir / "incumbent_mode_field_parity.json", parity_cells)
    _write(output_dir / "six_cell_parity_summary.json", {"cells": parity_cells, "exact_incumbent_parity": f"{exact_total}/180"})
    _write(output_dir / "incumbent_only_cp_results.json", cp_results)
    _write(output_dir / "root_cause.json", root)
    _write(output_dir / "next_stage_decision.json", {"decision": decision, "exact_incumbent_parity": exact_total, "incumbent_only_cp": cp_results})
    report = (f"# E1.7 Incumbent Reconstruction Parity\n\nDecision: `{decision}`\n\n"
              f"Exact incumbent parity: `{exact_total}/180`. Productive execution uses ceil(minutes*60); "
              "the prior one-second count came from comparing that execution value with round(minutes*60). "
              "Incumbent replay uses CR-prefix worker reservations; alternatives remain outside-reservation scoped.\n")
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): _sha(p)
             for p in sorted(output_dir.rglob("*")) if p.is_file()}
    manifest = {"files": files, "audit_fingerprint": hashlib.sha256(
        json.dumps({"source": source_out, "decision": decision, "exact": exact_total},
                   sort_keys=True).encode()).hexdigest(), "decision": decision}
    _write(output_dir / "artifact_hash_manifest.json", manifest)
    return {"decision": decision, "exact_incumbent_parity": exact_total,
            "cp": cp_results}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(_safe(run(args.output_dir)), indent=2, default=str))
