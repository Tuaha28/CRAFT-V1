"""Read-only unit-contract checks against the live authoritative database."""

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from utils.derived_constants import load_derived_constants
from utils.machine_taxonomy import machine_type_to_operation_type


def segment_minutes(raw):
    segments = json.loads(raw)
    return sum(
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60.0
        for start, end in segments
    )


def main():
    engine = get_engine()
    calibration = load_derived_constants(engine=engine)
    with engine.connect() as conn:
        line_eff = pd.read_sql(text(
            "SELECT AVG(target_efficiency_percentage) AS mean_pct, "
            "MIN(target_efficiency_percentage) AS min_pct, "
            "MAX(target_efficiency_percentage) AS max_pct "
            "FROM Production_Lines WHERE is_active=1"
        ), conn).iloc[0]
        samples = pd.read_sql(text("""
            SELECT j.schedule_item_id, j.production_order_id, j.operation_id,
                   j.lot_quantity, j.planned_start_time, j.planned_end_time,
                   j.planned_work_segments, ro.standard_allowed_minutes AS sam,
                   ro.required_machine_type, j.employee_id
            FROM Job_Shop_Schedules j
            JOIN Routing_Operations ro ON ro.operation_id=j.operation_id
            WHERE j.planned_work_segments IS NOT NULL
            ORDER BY j.schedule_item_id
            LIMIT 20
        """), conn)
        skills = pd.read_sql(text(
            "SELECT employee_id, operation_type, efficiency_rating "
            "FROM Operator_Skill_Matrix"
        ), conn)

    factor = float(calibration["duration_calibration_factor"])
    skill_lookup = {
        (str(r.employee_id), str(r.operation_type).strip().lower()):
        float(r.efficiency_rating)
        for r in skills.itertuples(index=False)
    }
    rows = []
    for row in samples.itertuples(index=False):
        operation_type = machine_type_to_operation_type(row.required_machine_type)
        efficiency = skill_lookup[(str(row.employee_id), operation_type)]
        formula = factor * float(row.lot_quantity) * float(row.sam) / (
            efficiency / 100.0
        )
        segments = segment_minutes(row.planned_work_segments)
        span = (row.planned_end_time - row.planned_start_time).total_seconds() / 60.0
        rows.append({
            "schedule_item_id": int(row.schedule_item_id),
            "formula_productive_minutes": formula,
            "stored_segment_productive_minutes": segments,
            "stored_elapsed_span_minutes": span,
            "formula_minus_segments": formula - segments,
            "segments_conserve_formula": math.isclose(formula, segments, abs_tol=1.0 / 60.0),
            "elapsed_not_less_than_productive": span + 1e-9 >= segments,
        })

    print(json.dumps({
        "measured_columns": {
            "simulator": "duration_calibration_factor * lot_quantity * standard_allowed_minutes / (Operator_Skill_Matrix.efficiency_rating / 100)",
            "scheduler": "duration_calibration_factor * standard_allowed_minutes * round(target_quantity * remaining_frac) / mean(active Production_Lines.target_efficiency_percentage / 100)",
        },
        "calibration": calibration,
        "active_line_efficiency_percent": {
            "mean": float(line_eff.mean_pct),
            "min": float(line_eff.min_pct),
            "max": float(line_eff.max_pct),
        },
        "sample_count": len(rows),
        "all_productive_minutes_conserved": all(r["segments_conserve_formula"] for r in rows),
        "all_elapsed_spans_valid": all(r["elapsed_not_less_than_productive"] for r in rows),
        "max_abs_formula_segment_error_minutes": max(
            abs(r["formula_minus_segments"]) for r in rows
        ),
        "samples": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
