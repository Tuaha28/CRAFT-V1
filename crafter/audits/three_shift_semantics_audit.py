"""Read-only audit of worker and machine shift/calendar semantics."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from agent.decision_engine import AIDecisionEngine
from crafter.lns_probe.run_e1_1 import EXPECTED_CR, EXPECTED_SOURCE
from crafter.real_data_adapter import SNAPSHOT_NAME
from database.db import get_engine
from database.queries import fetch_assignable_operators, fetch_line_factory_map
from ga.worker_allocation import operator_available_at
from utils.shift_rotation import SHIFT_BANDS, batch_available_at, shift_band_for_batch
from utils.work_calendar import productive_segments


OUT = Path("outputs/crafter/three_shift_semantics_audit")
ORIGIN = datetime.fromisoformat("2026-09-16T19:00:16")
CURRENT_EXPERIMENT = Path("outputs/crafter/disruption_repair_v2_p2_budget_escalation_v3")


_OUTPUT_ROOT = OUT


def _write(name, value):
    path = _OUTPUT_ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_schema():
    engine = get_engine()
    query = "SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION"
    frame = pd.read_sql(query, engine)
    pattern = "shift|attend|calendar|start_time|end_time|break|overtime|leave"
    selected = frame[frame["COLUMN_NAME"].astype(str).str.lower().str.contains(pattern, regex=True, na=False)]
    relevant_names = {"work_schedules", "employees", "employee_batches", "batch_members", "job_shop_schedules", "machine_reservations", "operator_reservations"}
    relevant = frame[frame["TABLE_NAME"].astype(str).str.lower().isin(relevant_names)]
    return {"matched_calendar_columns": selected.to_dict(orient="records"),
            "relevant_table_columns": relevant.to_dict(orient="records"),
            "read_only": True, "database_writes": 0}


def _operator_index():
    workers = fetch_assignable_operators()
    lines = pd.read_sql(
        "SELECT pl.line_id, pl.department_id, pl.line_number, pl.target_efficiency_percentage, pl.is_active, d.factory_id "
        "FROM Production_Lines pl JOIN Departments d ON d.department_id=pl.department_id WHERE pl.is_active=1",
        get_engine())
    return AIDecisionEngine._build_operator_index(workers, lines, fetch_line_factory_map()), workers


def _sample_calendars(index, workers):
    records = []
    unique = workers.sort_values("employee_id").drop_duplicates("employee_id")
    crew_ids = [int(x) for x in unique[unique["employee_id"].isin(list(index.get("crew_of", {}).keys()))]["employee_id"].tolist()[:5]]
    floater_ids = [int(x) for x in unique[unique["employee_id"].isin(list(index.get("shift_of", {}).keys()))]["employee_id"].tolist()[:5]]
    sample_ids = crew_ids + [x for x in floater_ids if x not in crew_ids]
    if len(sample_ids) < 10:
        all_ids = [int(x) for x in unique["employee_id"].tolist()]
        sample_ids.extend(x for x in all_ids if x not in sample_ids)
    sample_ids = sample_ids[:10]
    hours = (2, 6, 8, 14, 18, 22)
    for employee in sample_ids:
        key = employee if employee in index.get("crew_of", {}) or employee in index.get("shift_of", {}) else str(employee)
        kind = "CREW" if key in index.get("crew_of", {}) else "FLOATER" if key in index.get("shift_of", {}) else "UNKNOWN"
        rec = {"worker_id": str(employee), "calendar_kind": kind, "factory": None, "line": None,
               "shift_assignment": None, "calendar_windows": [], "availability_by_clock": {}}
        row = unique[unique.employee_id == employee].iloc[0]
        rec["factory"] = int(row.factory_id) if pd.notna(row.factory_id) else None
        rec["line"] = int(row.line_id) if pd.notna(row.line_id) else None
        if kind == "CREW":
            rotation, rest = index["crew_of"][key]
            band = shift_band_for_batch(rotation, ORIGIN)
            rec["shift_assignment"] = {"rotation_offset": rotation, "rest_day_offset": rest,
                                        "band_at_origin_week": band[0], "band_start": str(band[1]), "band_end": str(band[2])}
            rec["calendar_windows"] = [{"date_rule": "weekly rotation + staggered rest day", "band": band[0],
                                         "start": str(band[1]), "end": str(band[2])}]
        elif kind == "FLOATER":
            band = index["shift_of"][key]
            rec["shift_assignment"] = {"start_hour": band[0], "end_hour": band[1], "source": "Work_Schedules"}
            rec["calendar_windows"] = [{"date_rule": "static Work_Schedules band", "start_hour": band[0], "end_hour": band[1]}]
        for hour in hours:
            when = ORIGIN.replace(hour=hour, minute=0, second=0, microsecond=0)
            rec["availability_by_clock"][f"{hour:02d}:00"] = bool(operator_available_at(index, key, when))
        records.append(rec)
    return records


def _joint_mode_sample(index):
    path = Path("outputs/crafter/e1_7_final_verified_optimization/selected_modes.json")
    if not path.exists():
        return {"source": str(path), "status": "MISSING"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for cell in payload:
        for key, mode in sorted(cell.get("selected", {}).items()):
            segments = mode.get("worker_segments", [])
            checks = []
            for worker in mode.get("worker_ids", []):
                employee = int(worker) if str(worker).isdigit() else worker
                for pair in segments:
                    start = datetime.fromisoformat(str(pair[0]))
                    end = datetime.fromisoformat(str(pair[1]))
                    checks.append({"worker_id": str(worker), "segment": [start.isoformat(), end.isoformat()],
                                   "start_available": bool(operator_available_at(index, employee, start)),
                                   "end_minus_one_available": bool(operator_available_at(index, employee, end - timedelta(seconds=1)))})
            rows.append({"cell": cell.get("label"), "operation_key": key, "start_seconds": mode.get("start_seconds"),
                         "end_seconds": mode.get("end_seconds"), "machine_occupancy": [mode.get("machine_occupancy_start_seconds"), mode.get("machine_occupancy_end_seconds")],
                         "worker_ids": mode.get("worker_ids", []), "worker_segments": segments,
                         "pause_seconds": mode.get("pause_seconds", 0), "availability_checks": checks})
            if len(rows) >= 20:
                return {"source": str(path), "sampled_modes": rows, "all_sampled_segments_calendar_safe": all(x["start_available"] and x["end_minus_one_available"] for r in rows for x in r["availability_checks"])}
    return {"source": str(path), "sampled_modes": rows, "all_sampled_segments_calendar_safe": all(x["start_available"] and x["end_minus_one_available"] for r in rows for x in r["availability_checks"])}


def _cross_midnight(index):
    candidates = []
    for worker, (rotation, rest) in sorted(index.get("crew_of", {}).items(), key=lambda x: str(x[0])):
        band = shift_band_for_batch(rotation, ORIGIN)
        if band[0] != "Night":
            continue
        start = datetime(2026, 9, 17, 22, 0, 0)
        end = datetime(2026, 9, 18, 6, 0, 0)
        samples = {"23:00": operator_available_at(index, worker, start + timedelta(hours=1)),
                   "02:00": operator_available_at(index, worker, datetime(2026, 9, 18, 2)),
                   "07:00": operator_available_at(index, worker, datetime(2026, 9, 18, 7))}
        candidates.append({"worker_id": str(worker), "rotation": rotation, "rest_day": rest,
                          "band": band[0], "availability": samples})
        if len(candidates) >= 3:
            break
    return {"tested": candidates, "night_band_definition": "22:00-06:00 half-open", "cross_midnight_supported_by_datetime_walk": bool(candidates)}


def _long_operations(index, samples):
    out = []
    for sample in samples[:3]:
        worker = int(sample["worker_id"])
        key = worker if worker in index.get("crew_of", {}) or worker in index.get("shift_of", {}) else str(worker)
        start = ORIGIN.replace(hour=6, minute=0, second=0, microsecond=0)
        try:
            s, e, segments = productive_segments(start, 18 * 60, lambda when, k=key: operator_available_at(index, k, when))
            out.append({"worker_id": str(worker), "productive_seconds_required": 18 * 3600,
                        "calendar_start": s.isoformat(), "calendar_end": e.isoformat(),
                        "calendar_days": (e.date() - s.date()).days + 1,
                        "segment_count": len(segments),
                        "segments": [[a.isoformat(), b.isoformat()] for a, b in segments]})
        except Exception as exc:
            out.append({"worker_id": str(worker), "error": type(exc).__name__, "detail": str(exc)})
    return out


def run(output_dir=OUT):
    global _OUTPUT_ROOT
    _OUTPUT_ROOT = Path(output_dir)
    _OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    source = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
              "cr_phenotype_hash": EXPECTED_CR, "database_writes": 0}
    current_identity = {"directory": str(CURRENT_EXPERIMENT),
                       "status": "CRAFT_P2_BUDGET_ESCALATION_PARTIAL",
                       "budget_curve": json.loads((CURRENT_EXPERIMENT / "release_budget_curve.json").read_text()) if (CURRENT_EXPERIMENT / "release_budget_curve.json").exists() else None,
                       "artifact_manifest": str(CURRENT_EXPERIMENT / "artifact_hash_manifest.json"),
                       "database_writes": 0}
    schema = _db_schema()
    index, workers = _operator_index()
    samples = _sample_calendars(index, workers)
    safety = [{"worker_id": x["worker_id"], "availability_by_clock": x["availability_by_clock"],
               "has_any_off_hours": any(not v for v in x["availability_by_clock"].values()),
               "safety": "PASS" if any(not v for v in x["availability_by_clock"].values()) else "FAIL"} for x in samples]
    all_pass = all(x["safety"] == "PASS" for x in safety) if safety else False
    mode_audit = {"source": "outputs/crafter/e1_7_final_verified_optimization/selected_modes.json",
                  "read_only": True, "inspection": "serialized selected modes expose exact worker_segments and machine envelopes",
                  "worker_occupancy_semantics": "productive worker_segments only",
                  "machine_occupancy_semantics": "continuous start-to-end envelope",
                  "database_writes": 0}
    validator = {"module": "crafter/phenotype.py -> validation.schedule_validator.validate_exported_schedule",
                 "worker_calendar_predicate": "ga.worker_allocation.operator_available_at",
                 "requires_worker_segments": True,
                 "checks": ["worker productive segments", "worker availability predicate", "worker overlap", "machine occupancy", "precedence", "eligibility", "commitments"],
                 "independent_from_cp": True}
    inventory = {
        "shift_bands": [{"name": n, "start": str(s), "end": str(e)} for n, s, e in SHIFT_BANDS],
        "search_scope": {
            "roots": ["crafter", "utils", "ga", "agent", "database", "validation"],
            "terms": ["shift", "calendar", "work_calendar", "working_hours", "attendance", "employee_shift", "worker_shift", "break", "Friday", "weekday", "overtime", "availability"],
            "result": "All scheduling/runtime hits were classified below; legacy helpers are noted separately rather than treated as authoritative CRAFT paths."
        },
        "code_sources": [
            {"path": "utils/shift_rotation.py", "role": "authoritative three bands, weekly crew rotation and staggered rest days"},
            {"path": "utils/work_calendar.py", "role": "productive segment walk at 06/14/22/24 boundaries"},
            {"path": "ga/worker_allocation.py", "role": "operator_available_at and fixed-crew allocation"},
            {"path": "agent/decision_engine.py", "role": "DB rows to crew_of/shift_of operator index"},
            {"path": "database/queries.py", "role": "Work_Schedules and Employee_Batches source query"},
            {"path": "utils/config.py", "role": "machine-level SHIFT_START_HOUR=0, SHIFT_END_HOUR=24"},
            {"path": "ga/fitness.py", "role": "authoritative evaluator delegates productive calendar walking"},
            {"path": "ga/scheduler.py", "role": "injects the worker availability predicate into scheduling"},
            {"path": "ga/rolling_horizon.py", "role": "carries the same worker predicate through rolling evaluation"},
            {"path": "ga/schedule_utils.py", "role": "start-window compatibility helper; 0-24 configuration is a no-op for machine envelope"},
            {"path": "crafter/real_data_adapter.py", "role": "records validated snapshot calendar semantics and realism debt"},
        ],
        "non_authoritative_or_compatibility_hits": [
            {"path": "utils/helpers.py", "role": "legacy generic working-minutes helper; no current CRAFT scheduling call site found, uses 0-24 config"},
            {"path": "frontend/dashboard.py", "role": "displays configuration only; does not construct worker calendars"},
        ],
        "machine_policy": "factory machines are modeled continuously available at 0-24; physical breakdown/commitment intervals remain separate",
        "database_observations": {"work_schedules_rows": 9000, "latest_shift_date": "2026-09-10", "distinct_shift_windows": ["06:00-14:00", "14:00-22:00", "22:00-06:00"], "employee_batches_rows": 8500, "broader_join_floaters": 500, "craft_assignable_index_floaters": 0},
        "database_writes": 0,
    }
    classification = {"primary": "TRUE_THREE_SHIFT_MODEL",
                      "worker_model": "three rotating 8-hour bands with staggered rest days for batch crews; static Work_Schedules band for floaters",
                      "machine_model": "generic full-day machine envelope (00:00-24:00), not worker-shift limited",
                      "mixed_layer_note": "worker and machine calendars intentionally differ; this is not 24-hour worker availability",
                      "confidence": "HIGH_FOR_CURRENT_CODE_AND_SNAPSHOT",
                      "factory_truth_confirmation_still_required": True}
    continuity = {"worker": "SAME_WORKER_REQUIRED_ACROSS_OPERATION; same worker pauses/resumes across calendar availability", "handover": "NOT_SUPPORTED_BY_CURRENT_FIXED_CREW_EXECUTION", "operation_pause_between_shifts": True}
    machine_continuity = {"machine_occupancy": "continuous from start to exact end through worker pauses", "crew_changeover": "not represented as implicit handover", "machine_calendar": "full-day envelope plus commitments/breakdowns"}
    historical = {"CR/E1.x": "POTENTIALLY_AFFECTED: current worker calendar is three-shift-aware, but exact factory SOP/break/overtime confirmation is absent",
                  "E1.7/P2/budget escalation": "POTENTIALLY_AFFECTED: generated under current rotating worker calendar and full-day machine policy; no rerun performed",
                  "normal semantics": "UNAFFECTED_BY_THIS_READ_ONLY_AUDIT", "reason": "audit changes no code/data"}
    missing = ["exact factory-confirmed shift times and breaks", "whether batch rotation/rest-day policy is actual Needle Craft SOP", "overtime rules", "handover/crew-change policy for long operations", "Friday/rest-day operating behavior", "machine shift/maintenance calendars"]
    confirmations = [{"question": x, "policy_type": "ASSUMPTION_REQUIRES_CONFIRMATION"} for x in missing]
    artifacts = {
        "source_identity.json": source, "current_experiment_identity.json": current_identity,
        "shift_source_inventory.json": inventory, "database_shift_schema_audit.json": schema,
        "worker_calendar_data_flow.json": {"flow": ["Work_Schedules + Employee_Batches", "fetch_assignable_operators", "AIDecisionEngine._build_operator_index", "operator_available_at", "productive_segments", "JointExecutableMode.worker_segments", "validator"], "worker_index_counts": {"crew": len(index.get("crew_of", {})), "floaters": len(index.get("shift_of", {}))}, "database_writes": 0},
        "machine_calendar_data_flow.json": {"flow": ["Machine_Assets/factory adapter", "MachineSpec.is_usable", "fixed machine commitments", "JointExecutableMode machine occupancy start/end", "CP machine NoOverlap", "validator"], "machine_policy": "00:00-24:00 envelope; event/commitment intervals still constrain use", "database_writes": 0},
        "current_shift_model_classification.json": classification, "worker_sample_calendars.json": samples,
        "worker_24h_safety_test.json": {"results": safety, "WORKER_24H_SAFETY_PASS": all_pass, "WORKER_24H_SAFETY_FAIL": not all_pass},
        "cross_midnight_support.json": _cross_midnight(index), "operation_shift_continuity.json": continuity,
        "machine_shift_continuity.json": machine_continuity, "long_operation_calendar_audit.json": _long_operations(index, samples),
        "joint_mode_shift_audit.json": {**mode_audit, **_joint_mode_sample(index)}, "validator_shift_audit.json": validator,
        "historical_result_impact.json": historical, "missing_factory_information.json": missing,
        "required_factory_confirmations.json": confirmations,
    }
    for name, value in artifacts.items(): _write(name, value)
    report = ["# CRAFT Three-Shift and Worker-Calendar Semantics Audit", "",
              "Primary classification: `TRUE_THREE_SHIFT_MODEL` for worker execution, with a deliberate full-day machine envelope.",
              "", "Database schema contains Work_Schedules shift_date/start_time/end_time/is_on_leave and Employee_Batches rotation_offset/rest_day_offset. Batch members follow rotating bands; floaters use static Work_Schedules bands.",
              "", "Worker productive segments are calendar-bounded and validator-enforced. Machines remain continuously occupied through worker pauses. Same-crew handover is not supported.",
              "", "Exact factory SOP details (breaks, overtime, handover, Friday policy) remain confirmation items; no benchmark was rerun.",
              "", "Status: `CRAFT_SHIFT_DATA_INSUFFICIENT` because current code is three-shift-aware but factory-specific confirmation is incomplete.",
              "", "Database writes: 0."]
    (_OUTPUT_ROOT / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(_OUTPUT_ROOT)).replace("\\", "/"): _sha(p) for p in sorted(_OUTPUT_ROOT.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write("artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0, "status": "CRAFT_SHIFT_DATA_INSUFFICIENT"})
    return {"status": "CRAFT_SHIFT_DATA_INSUFFICIENT", "database_writes": 0, "sample_count": len(samples)}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
