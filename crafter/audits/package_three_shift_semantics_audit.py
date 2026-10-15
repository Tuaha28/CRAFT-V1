"""Package a deterministic, read-only three-shift audit report from an existing run."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


def package(source: str, destination: str):
    src = Path(source)
    dst = Path(destination)
    if dst.exists():
        raise FileExistsError(dst)
    shutil.copytree(src, dst)
    report = """# CRAFT Three-Shift and Worker-Calendar Semantics Audit

Status: `CRAFT_SHIFT_DATA_INSUFFICIENT`

## Direct answers

1. **Shifts modeled:** three worker bands: 06:00–14:00, 14:00–22:00, and 22:00–06:00. Machines use a deliberate 00:00–24:00 envelope; this is not 24-hour worker availability.
2. **Definition:** `utils/shift_rotation.py` and `utils/work_calendar.py`; machine envelope constants are in `utils/config.py`.
3. **Database assignments:** yes. `work_schedules` contains dated start/end windows and leave; `employee_batches`/`batch_members` contain batch line, rotation and rest-day data. The current assignable CRAFT index contains 7,820 batch-crew workers and 0 floaters; the broader DB join contains 500 floater rows.
4. **24-hour worker availability:** no in the tested snapshot. The 10-worker safety audit passes; each tested worker has off-hours.
5. **Outside legitimate shift:** authoritative `operator_available_at` and productive-segment generation prevent it; selected-mode segment checks passed.
6. **Multiple shifts in one day:** not as simultaneous availability. The same fixed worker may resume on later eligible shifts/days for a long operation; no implicit double-booking or crew handover is modeled.
7. **Overnight:** supported for the 22:00–06:00 band, including date crossing.
8. **Operation across shift boundaries:** yes, by pausing between productive worker segments while retaining the exact elapsed machine envelope.
9. **Worker changes during a long operation:** no implicit handover; the current contract uses the same fixed crew across pauses.
10. **Machine continuity:** machines remain continuously occupied from start through end, including worker pauses; replacement-crew handover is not implicit.
11. **Validator enforcement:** yes. The independent validator checks worker productive segments, the same availability predicate, worker overlap, machine occupancy, precedence, eligibility and commitments.
12. **Benchmark semantics:** current CRAFT experiments were generated under the repository's three-shift-aware worker calendar and full-day machine policy. They are therefore **POTENTIALLY_AFFECTED**, not automatically invalid, until factory SOP details are confirmed; no benchmark was rerun.
13. **Potentially affected results:** CR/E1.x, E1.7/P2, and the current P2 budget-escalation result. This audit itself made no scheduling/data changes.
14. **Missing information:** factory-confirmed breaks, overtime, rotation/rest-day SOP, Friday behavior, handover rules, and machine shift/maintenance calendars.
15. **Next change (not performed):** confirm those policies with Needle Craft, then decide whether the current fixed-crew/pause contract needs a controlled semantics update and rerun affected benchmarks.

## Evidence and interpretation

The snapshot database exposes the three distinct windows 06:00–14:00, 14:00–22:00 and 22:00–06:00. Batch members use deterministic weekly rotation and staggered rest days; floaters, where present, use dated `Work_Schedules` windows. A sampled 18-hour walk produced multiple legal productive segments over calendar days, and sampled serialized JointExecutableModes had calendar-safe worker segments. The current code is therefore three-shift-aware for worker execution, but exact Needle Craft operating policy is not fully confirmed by authoritative SOP data.

Current experiment recorded for provenance: `outputs/crafter/disruption_repair_v2_p2_budget_escalation_v3/`, status `CRAFT_P2_BUDGET_ESCALATION_PARTIAL`, budgets 30 and 35 both infeasible, broad benchmark not run, database writes 0.

Database writes: 0. No scheduling benchmark was rerun.
"""
    (dst / "final_report.md").write_text(report, encoding="utf-8")
    files = {}
    for p in sorted(dst.rglob("*")):
        if p.is_file() and p.name != "artifact_hash_manifest.json":
            files[str(p.relative_to(dst)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    (dst / "artifact_hash_manifest.json").write_text(json.dumps({"files": files, "artifact_count": len(files), "database_writes": 0, "status": "CRAFT_SHIFT_DATA_INSUFFICIENT"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "CRAFT_SHIFT_DATA_INSUFFICIENT", "artifact_count": len(files), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(package("outputs/crafter/three_shift_semantics_audit_v3", "outputs/crafter/three_shift_semantics_audit_v4"), indent=2))
