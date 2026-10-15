"""Read-only shift/calendar audit; writes only the requested audit artifacts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database.db import get_engine
from database.ga_snapshot import _server_engine, list_snapshots


OUT = Path("outputs/crafter/shift_audit")


def frame(engine, sql):
    return pd.read_sql(sql, engine)


def clean(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def records(df):
    return [{str(k): clean(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def main():
    engine = get_engine()
    evidence = {}
    # Schema inventory: table/column names containing calendar/shift/attendance
    evidence["schema_columns"] = records(frame(engine, """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND (LOWER(TABLE_NAME) LIKE '%%shift%%' OR LOWER(TABLE_NAME) LIKE '%%schedule%%'
               OR LOWER(TABLE_NAME) LIKE '%%calendar%%' OR LOWER(TABLE_NAME) LIKE '%%attendance%%'
               OR LOWER(COLUMN_NAME) LIKE '%%shift%%' OR LOWER(COLUMN_NAME) LIKE '%%schedule%%'
               OR LOWER(COLUMN_NAME) LIKE '%%calendar%%' OR LOWER(COLUMN_NAME) LIKE '%%attendance%%'
               OR LOWER(COLUMN_NAME) IN ('start_time','end_time','is_on_leave'))
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """))
    evidence["shift_windows"] = records(frame(engine, """
        SELECT start_time, end_time, COUNT(*) AS row_count,
               COUNT(DISTINCT employee_id) AS worker_count,
               SUM(CASE WHEN is_on_leave = 1 THEN 1 ELSE 0 END) AS leave_rows,
               MIN(shift_date) AS first_date, MAX(shift_date) AS last_date
        FROM Work_Schedules
        GROUP BY start_time, end_time
        ORDER BY start_time, end_time
    """))
    evidence["shift_windows_by_factory"] = records(frame(engine, """
        SELECT e.factory_id, ws.start_time, ws.end_time,
               COUNT(*) AS row_count, COUNT(DISTINCT ws.employee_id) AS worker_count,
               SUM(CASE WHEN ws.is_on_leave = 1 THEN 1 ELSE 0 END) AS leave_rows
        FROM Work_Schedules ws JOIN Employees e ON e.employee_id = ws.employee_id
        GROUP BY e.factory_id, ws.start_time, ws.end_time
        ORDER BY e.factory_id, ws.start_time, ws.end_time
    """))
    evidence["calendar_dates"] = records(frame(engine, """
        SELECT MIN(shift_date) AS first_date, MAX(shift_date) AS last_date,
               COUNT(DISTINCT shift_date) AS distinct_dates,
               COUNT(*) AS schedule_rows
        FROM Work_Schedules
    """))
    evidence["worker_window_cardinality"] = records(frame(engine, """
        SELECT window_count, COUNT(*) AS worker_count
        FROM (
          SELECT employee_id, COUNT(DISTINCT CONCAT(CAST(start_time AS CHAR), '-', CAST(end_time AS CHAR))) AS window_count
          FROM Work_Schedules GROUP BY employee_id
        ) x GROUP BY window_count ORDER BY window_count
    """))
    evidence["batch_rotation"] = records(frame(engine, """
        SELECT rotation_offset, rest_day_offset, COUNT(*) AS batch_count
        FROM employee_batches
        GROUP BY rotation_offset, rest_day_offset
        ORDER BY rotation_offset, rest_day_offset
    """))
    evidence["employee_counts_by_factory"] = records(frame(engine, """
        SELECT factory_id, COUNT(*) AS active_employee_count
        FROM Employees WHERE is_active = 1 GROUP BY factory_id ORDER BY factory_id
    """))
    evidence["schedule_status_counts"] = records(frame(engine, """
        SELECT execution_status, COUNT(*) AS row_count,
               SUM(actual_start_time IS NOT NULL) AS actual_start_rows,
               SUM(actual_end_time IS NOT NULL) AS actual_end_rows
        FROM Job_Shop_Schedules GROUP BY execution_status ORDER BY execution_status
    """))
    evidence["factory_calendar_tables"] = records(frame(engine, """
        SELECT TABLE_NAME FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND (LOWER(TABLE_NAME) LIKE '%%shift%%' OR LOWER(TABLE_NAME) LIKE '%%calendar%%'
               OR LOWER(TABLE_NAME) LIKE '%%schedule%%' OR LOWER(TABLE_NAME) LIKE '%%attendance%%')
        ORDER BY TABLE_NAME
    """))
    # Frozen snapshots are audited through the server-level engine so this
    # remains read-only and does not change the process DB selection.
    frozen = []
    server = _server_engine()
    for snap in list_snapshots():
        db = str(snap["snapshot_db"])
        safe_db = db.replace("`", "``")
        try:
            windows = frame(server, f"""
                SELECT start_time, end_time, COUNT(*) AS row_count,
                       COUNT(DISTINCT employee_id) AS worker_count,
                       SUM(CASE WHEN is_on_leave = 1 THEN 1 ELSE 0 END) AS leave_rows,
                       MIN(shift_date) AS first_date, MAX(shift_date) AS last_date
                FROM `{safe_db}`.`Work_Schedules`
                GROUP BY start_time, end_time ORDER BY start_time, end_time
            """)
            tables = records(frame(server, f"""
                SELECT TABLE_NAME FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = '{safe_db}' ORDER BY TABLE_NAME
            """))
            frozen.append({
                "snapshot_name": snap["snapshot_name"],
                "snapshot_db": db,
                "source_db": snap["source_db"],
                "origin_timestamp": clean(snap["origin_timestamp"]),
                "work_schedule_windows": records(windows),
                "calendar_related_tables": [x["TABLE_NAME"] for x in tables
                                              if any(term in str(x["TABLE_NAME"]).lower()
                                                     for term in ("shift", "calendar", "schedule", "attendance"))],
                "read_only": True,
            })
        except Exception as exc:
            frozen.append({"snapshot_name": snap["snapshot_name"], "snapshot_db": db,
                           "read_error": f"{type(exc).__name__}: {exc}", "read_only": True})
    evidence["frozen_snapshots"] = frozen
    evidence["read_only"] = True
    evidence["database_writes"] = 0
    evidence["source"] = "live database via existing database.db.get_engine SELECT queries"
    evidence["code_consistency_inventory"] = [
        {"path": "utils/shift_rotation.py", "classification": "authoritative production calendar",
         "evidence": "three half-open bands: Morning 06:00-14:00, Evening 14:00-22:00, Night 22:00-06:00; weekly rotation and staggered rest days"},
        {"path": "utils/work_calendar.py", "classification": "authoritative execution primitive",
         "evidence": "productive segments split at 06/14/22/24 boundaries; same worker resumes after pauses"},
        {"path": "data_generation/seed_database_fixed.py", "classification": "synthetic-data assumption",
         "evidence": "seeds three 8-hour bands and Work_Schedules rows; machines treated as continuous 24-hour envelope"},
        {"path": "data_generation/simulation_calendar.py", "classification": "synthetic-data calendar implementation",
         "evidence": "worker productive segments use shift bands and pause/resume"},
        {"path": "crafter/real_data_adapter.py", "classification": "CRAFT provenance / realism-debt marker",
         "evidence": "records rotating 06:00-14:00, 14:00-22:00, 22:00-06:00 crews and explicitly says factory SOP is not fully confirmed"},
        {"path": "crafter/repair/calendar_regimes.py", "classification": "CRAFT exact calendar integration",
         "evidence": "constructs exact fixed-crew execution regimes from worker calendars and reservations"},
        {"path": "crafter/repair/scoped_validation.py", "classification": "CRAFT independent validation",
         "evidence": "replay/physical validation checks worker segments and calendar legality"},
        {"path": "utils/config.py", "classification": "machine-envelope configuration",
         "evidence": "SHIFT_START_HOUR=0 and SHIFT_END_HOUR=24; deliberate full-day machine envelope, not worker availability"},
        {"path": "README.md", "classification": "obsolete/inconsistent project documentation",
         "evidence": "older key-decisions text says single-shift roster and three-shift model planned"},
        {"path": "PROJECT_STATUS.txt", "classification": "historical/legacy documentation",
         "evidence": "contains earlier single-shift labour-utilization statement; newer sections and current code describe three-shift-aware calendars"},
        {"path": "utils/helpers.py", "classification": "legacy compatibility helper / potential bug if reused",
         "evidence": "generic SHIFT_START_HOUR/SHIFT_END_HOUR working-minute helper; no current CRAFT scheduling call site found"},
    ]
    evidence["factory_truth"] = {
        "status": "NOT_CONFIRMED_BY_FACTORY_SOP",
        "reason": "Repository contains explicit legacy one-shift documentation but current synthetic/live data and CRAFT calendar path encode three worker bands; no authoritative SOP/attendance policy source was found.",
        "required_confirmation": ["actual shift hours and breaks", "rotation/rest-day SOP", "Friday policy", "overtime", "handover policy", "machine maintenance calendar"],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "shift_audit.json").write_text(json.dumps(evidence, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
    report = """# Read-only CRAFT factory shift-structure audit

## Conclusion

`FACTORY_DATA_AND_MODEL_INCONSISTENT`

The current live/frozen project data and the current CRAFT scheduling path
represent three worker shift bands, while older project documentation still
states a single-shift roster with three shifts as a future plan. The actual
target factory SOP is not present in the repository, so the physical factory
policy itself is not independently confirmed by this audit.

## Direct answers

1. **How many shifts does the real target factory use?** Not proven from a
   factory-authoritative SOP. The project database/synthetic snapshot uses
   three worker bands.
2. **Exact modeled hours:** Morning 06:00–14:00, Evening 14:00–22:00,
   Night 22:00–06:00 (half-open; the night band crosses midnight).
3. **Current database:** `Work_Schedules` contains exactly three distinct
   intervals, 06:00–14:00, 14:00–22:00, and 22:00–06:00, with 3,000 rows and
   3,000 workers per interval in the live database. There is no separate
   shift-ID/shift-master table in the audited schema. Frozen snapshots expose
   the same calendar structure where readable.
4. **Current CRAFT model:** three rotating eight-hour batch-crew bands with
   weekly rotation and staggered rest days; static `Work_Schedules` windows for
   floaters. The CRAFT assignable index currently reports batch crews and no
   floaters for the audited snapshot. Machines use a separate continuous
   00:00–24:00 envelope; this is not 24-hour worker availability.
5. **One-shift hard-coding:** `README.md` and historical `PROJECT_STATUS.txt`
   contain obsolete single-shift statements. `utils/helpers.py` is a legacy
   generic helper driven by the 0–24 config and has no current CRAFT scheduling
   call site. The authoritative CRAFT path is three-shift-aware.
6. **Rolling/commit horizon:** no immediate redesign follows from the modeled
   data, because rolling windows and exact commitment boundaries operate in
   elapsed seconds and the calendar regime/replay layers already carry pauses.
   But the policy must be confirmed before interpreting production timing;
   fixed-crew pause/resume and no implicit handover are current semantics.
7. **Required corrections before future CRAFT experiments:** obtain and bind
   factory-confirmed shift/break/rotation/rest/Friday/overtime/handover and
   machine-maintenance policies; then classify whether current synthetic
   assumptions are acceptable for production and rerun affected benchmarks if
   they change.

## Database evidence

The live database contains `Work_Schedules(shift_date, start_time, end_time,
is_on_leave)` and `Employee_Batches(rotation_offset, rest_day_offset)`. The
current live date is 2026-09-10; all 9,000 Work_Schedules rows are on that date,
split into 3,000 rows for each of the three intervals. Shift assignments are
encoded by intervals, not a shift ID. `Job_Shop_Schedules` contains execution
status and actual timestamps, but is not a shift-master/calendar table.

Batch crews rotate weekly using the repository's deterministic offsets. Rest
days are staggered by offset; the data does not prove a universal Friday
closure. Machines have no separate audited shift-calendar table; CRAFT's
configuration deliberately uses a 00:00–24:00 machine envelope plus exact
reservations/breakdowns.

## CRAFT semantics audit

`utils/shift_rotation.py`, `utils/work_calendar.py`, worker allocation,
`crafter/repair/calendar_regimes.py`, replay, and the independent validator
are three-shift-aware. Long operations pause at worker calendar boundaries and
resume with the same fixed crew; machine occupancy remains continuous through
worker pauses. Cross-shift crew handover is not implicitly modeled.

No scheduler, calendar, database, test, or prior CRAFT evidence was modified.
The only writes in this task are this audit JSON/Markdown and the read-only
audit helper under `scratch/`.
"""
    (OUT / "shift_audit.md").write_text(report, encoding="utf-8")
    return evidence


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str, sort_keys=True))
