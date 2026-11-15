"""Factory-ready Excel workbook for one validated CRAFT schedule.

Usage:
  venv/Scripts/python.exe scratch/export_craft_workbook.py <factory_id> <run_dir> [--schedule craft_v2] [--out file.xlsx]

<run_dir> is a scratch/run_craft_v2.py output directory (report.json plus
*_schedule.json). Set CRAFT_CAPTURE_DIR (and CRAFT_RESIDUAL_ROUTES etc.) as
for the run. Sheets:
  Summary       - every policy in the run side by side (KPIs, utilization);
                  with client=True only the exported plan, named CRAFT
  Order status  - due vs planned finish per order, with the main late reason
  Line plan     - one row per operation, by line, day and start
  Worker jobs   - each worker's job list in time order (printable)
  Line load     - planned vs on-shift worker hours per line and day
Reporting only: reads the schedule and the pinned capture, never changes them.
Late reasons are derived from the schedule itself (waits between route steps
and which order held the same machine or worker during the wait).
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from capture_utilization import STEP, _on_shift_steps  # noqa: E402
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402

DAY = 86400
HEADER = PatternFill("solid", fgColor="1F4E78")
LATE = PatternFill("solid", fgColor="F8CBAD")
ON_TIME = PatternFill("solid", fgColor="C6EFCE")
OVER = PatternFill("solid", fgColor="FFEB9C")
SIGNIFICANT_WAIT = 4 * 3600


def _sheet(wb, title, header, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), HEADER
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    for i, width in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    return ws


def late_reason(order_rows, release, due, holders, earlier_on_floor):
    """The main reason an order finishes after its due date, in plain words.
    ``holders(row, a, b)`` names (resource, other order) busy on the row's
    machines/workers during [a, b)."""
    if due <= 0:
        return "Already past its due date when this plan starts"
    waits = []
    ready = release
    for i, row in enumerate(order_rows):
        start = int(row["start_seconds"])
        if i == 0 and earlier_on_floor:
            waits.append((start - ready, "an earlier step of this order is still in progress on the floor", row))
        else:
            waits.append((start - ready, None, row))
        ready = int(row["end_seconds"])
    gap, why, row = max(waits, key=lambda w: w[0])
    total_wait = sum(max(0, w[0]) for w in waits)
    work_days = sum(int(r["end_seconds"]) - int(r["start_seconds"]) for r in order_rows) / DAY
    if total_wait < SIGNIFICANT_WAIT or work_days >= (due - release) / DAY:
        return (f"Too much work for the time available: about {work_days:.1f} days of production "
                f"(shift time included), {max(0.0, (due - release) / DAY):.1f} days from release to due date")
    if why:
        return f"Waits {gap / DAY:.1f} days: {why}"
    busy = holders(row, int(row["start_seconds"]) - gap, int(row["start_seconds"]))
    if busy:
        resource, other = busy
        return f"Waits {gap / DAY:.1f} days for {resource}, busy with order {other}"
    return f"Waits {gap / DAY:.1f} days for shift hours or a qualified resource to become free"


def build(factory_id, run_dir, schedule_name, out, compare=(), client=False):
    run_dir = Path(run_dir)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    rows = json.loads((run_dir / f"{schedule_name.lower()}_schedule.json").read_text(encoding="utf-8"))
    source, _readiness, snapshot, _hash = runner._source(factory_id)
    origin = datetime.fromisoformat(source["reference_time"])
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    op_names = {f'{o["production_order_id"]}_{o["operation_id"]}': o for o in source["operations"]}
    snap_ops = {op["operation_key"]: op for op in snapshot["operations"]}
    res = source["resources"]
    lines = {int(m["asset_id"]): int(m["line_id"]) for m in res["machines"]}
    skills = defaultdict(list)
    for s in res["skills"]:
        skills[str(s["employee_id"])].append(f'{s["operation_type"]} {float(s["efficiency_rating"]):.0f}%')
    when = lambda s: origin + timedelta(seconds=int(s))  # noqa: E731

    by_order = defaultdict(list)
    for row in rows:
        by_order[str(snap_ops[row["operation_key"]]["order_id"])].append(row)
    for order_rows in by_order.values():
        order_rows.sort(key=lambda r: int(snap_ops[r["operation_key"]]["routing_sequence"]))
    by_resource = defaultdict(list)
    for row in rows:
        oid = str(snap_ops[row["operation_key"]]["order_id"])
        for m in row["resource_configuration"]["machine_ids"]:
            by_resource[("machine", str(m))].append((int(row["start_seconds"]), int(row["end_seconds"]), oid))
        for w in row["resource_configuration"]["worker_ids"]:
            by_resource[("worker", str(w))].append((int(row["start_seconds"]), int(row["end_seconds"]), oid))

    def holders(row, a, b):
        own = str(snap_ops[row["operation_key"]]["order_id"])
        best = None
        for kind, field in (("machine", "machine_ids"), ("worker", "worker_ids")):
            for rid in row["resource_configuration"][field]:
                for s, e, oid in by_resource[(kind, str(rid))]:
                    overlap = min(e, b) - max(s, a)
                    if oid != own and overlap > 0 and (best is None or overlap > best[0]):
                        best = (overlap, f"{kind} {rid}", oid)
        return best[1:] if best else None

    def earlier_on_floor(oid, first_key):
        first_seq = int(snap_ops[first_key]["routing_sequence"])
        return any(op["order_id"] == oid and int(op["routing_sequence"]) < first_seq
                   and op["execution_state"] not in ("COMPLETED",) for op in snapshot["operations"])

    wb = Workbook()
    wb.remove(wb.active)

    # Summary: every policy of the run side by side (client: the exported plan only).
    summary = []
    for r in report["results"]:
        if client and r["policy"] != schedule_name.upper():
            continue
        k, u = r.get("kpis") or {}, r.get("utilization") or {}
        w = u.get("workers", {})
        summary.append(["CRAFT" if client else r["policy"], r["scheduled"], r["deferred"], k.get("orders_complete"),
                        k.get("orders_partially_scheduled"), k.get("on_time_orders"), k.get("on_time_pct"),
                        k.get("total_tardiness_days"), k.get("max_tardiness_days"), k.get("mean_flow_days"),
                        k.get("makespan_days"), w.get("used"), w.get("pool"), w.get("util_used_pct"),
                        w.get("util_pool_pct"), r.get("wall_seconds")])
    ws = _sheet(wb, "Summary", ["Plan" if client else "Policy", "Steps placed", "Steps not placed", "Orders complete",
                                "Orders partial", "On time", "On time %", "Total lateness (days)",
                                "Worst lateness (days)", "Mean flow (days)", "Last finish (day)",
                                "Workers used", "Worker pool", "Util. of used workers %",
                                "Util. of whole pool %", "Runtime (s)"], summary,
                [20] + [12] * 15)
    ws.append([])
    ws.append([f"Factory {factory_id}; plan starts {source['reference_time']}" if client else
               f"Factory {factory_id}; plan starts {source['reference_time']}; pace {report.get('pace')}; "
               f"schedule exported: {schedule_name}; capture {report.get('capture')}"])
    ws.append(["Lateness and on-time figures count complete orders only; 'Orders partial' are not in them."])

    # Order status with reasons.
    status_rows = []
    for _rel, oid, route, _o in runner._eligible_order_routes(source, snapshot):
        if oid not in by_order:
            continue
        order = orders[str(oid)]
        release = runner._date_seconds(origin, order.get("scheduled_start_date") or source["reference_time"])
        due = runner._date_seconds(origin, order.get("delivery_deadline"))
        placed = by_order[oid]
        complete = len(placed) == len(route)
        finish = max(int(r["end_seconds"]) for r in placed)
        late = max(0, finish - due)
        if not complete:
            state, reason = "Not fully planned", f"{len(route) - len(placed)} step(s) could not be placed in the horizon"
        elif late:
            state = "Late"
            reason = late_reason(placed, release, due, holders, earlier_on_floor(oid, placed[0]["operation_key"]))
        else:
            state, reason = "On time", f"{(due - finish) / DAY:.1f} days to spare"
        status_rows.append([int(oid), order.get("target_quantity"), order.get("delivery_deadline"),
                            when(finish).strftime("%Y-%m-%d %H:%M"), state, round(late / DAY, 2),
                            len(placed), len(route), reason])
    ws = _sheet(wb, "Order status", ["Order", "Pieces", "Due date", "Planned finish", "Status",
                                     "Days late", "Steps planned", "Steps", "Main reason"],
                sorted(status_rows, key=lambda r: (r[4] == "On time", -r[5])), [10, 9, 12, 17, 16, 10, 8, 8, 90])
    for row in ws.iter_rows(min_row=2):
        row[4].fill = ON_TIME if row[4].value == "On time" else LATE

    # Line plan.
    plan = []
    for row in rows:
        cfg, op = row["resource_configuration"], snap_ops[row["operation_key"]]
        meta = op_names.get(row["operation_key"], {})
        start, end = when(row["start_seconds"]), when(row["end_seconds"])
        plan.append([cfg["line_id"], start.strftime("%Y-%m-%d"), start.strftime("%H:%M"),
                     end.strftime("%Y-%m-%d %H:%M"), int(op["order_id"]), op["routing_sequence"],
                     op.get("operation_name") or meta.get("operation_name"), meta.get("required_machine_type"),
                     cfg.get("quantity"), cfg["k"], ", ".join(map(str, cfg["machine_ids"])),
                     ", ".join(map(str, cfg["worker_ids"])), round(cfg["productive_seconds"] / 3600, 2)])
    plan.sort(key=lambda r: (r[0], r[1], r[2]))
    _sheet(wb, "Line plan", ["Line", "Date", "Start", "End", "Order", "Step no.", "Step", "Machine type",
                             "Pieces", "Crew", "Machines", "Workers", "Productive hours"], plan,
           [7, 11, 7, 17, 8, 7, 26, 28, 8, 6, 20, 20, 10])

    # Worker jobs (printable: grouped by worker, time order).
    jobs = []
    for (kind, wid), items in sorted(by_resource.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
        if kind != "worker":
            continue
        for s, e, oid in sorted(items):
            row = next(r for r in by_order[oid] if int(r["start_seconds"]) == s
                       and str(wid) in map(str, r["resource_configuration"]["worker_ids"]))
            op = snap_ops[row["operation_key"]]
            cfg = row["resource_configuration"]
            machine = cfg["machine_ids"][list(map(str, cfg["worker_ids"])).index(str(wid))] \
                if len(cfg["machine_ids"]) == len(cfg["worker_ids"]) else ", ".join(map(str, cfg["machine_ids"]))
            jobs.append([int(wid), "; ".join(skills.get(str(wid), [])), when(s).strftime("%Y-%m-%d %H:%M"),
                         when(e).strftime("%Y-%m-%d %H:%M"), int(oid), op.get("operation_name"),
                         machine, cfg["k"]])
    ws = _sheet(wb, "Worker jobs", ["Worker", "Skills (efficiency)", "Start", "End", "Order", "Step",
                                    "Machine", "Crew size"], jobs, [9, 40, 17, 17, 8, 26, 10, 9])
    ws.print_title_rows = "1:1"

    # Line load per day: planned productive worker-hours vs on-shift worker-hours.
    batches = {int(b["batch_id"]): b for b in res["batches"] if b["is_active"]}
    member = {int(m["employee_id"]): int(m["batch_id"]) for m in res["batch_members"]}
    leave = {int(w["employee_id"]) for w in res["work_schedules"] if w["is_on_leave"]}
    horizon = max(int(r["end_seconds"]) for r in rows)
    steps = -(-horizon // STEP)
    crew_steps, available = {}, defaultdict(float)
    for w in res["workers"]:
        eid = int(w["employee_id"])
        if (int(w["factory_id"]) != factory_id or not w["is_active"] or eid in leave
                or member.get(eid) not in batches):
            continue
        batch = batches[member[eid]]
        crew = (int(batch["rotation_offset"]), int(batch["rest_day_offset"]))
        if crew not in crew_steps:
            crew_steps[crew] = _on_shift_steps(crew, origin, steps)
        for i, on in enumerate(crew_steps[crew]):
            if on:
                available[(int(batch["line_id"]), i * STEP // DAY)] += STEP / 3600
    planned = defaultdict(float)
    for row in rows:
        line = int(row["resource_configuration"]["line_id"])
        for a, b in row["worker_segments"]:
            for wid in row["resource_configuration"]["worker_ids"]:
                t = int(a)
                while t < int(b):  # split segments at midnight
                    nxt = min(int(b), (t // DAY + 1) * DAY)
                    planned[(line, t // DAY)] += (nxt - t) / 3600
                    t = nxt
    load = []
    for (line, day), hours in sorted(planned.items()):
        avail = available.get((line, day), 0.0)
        load.append([line, (origin + timedelta(days=day)).strftime("%Y-%m-%d"), round(hours, 1),
                     round(avail, 1), round(100 * hours / avail, 1) if avail else None])
    ws = _sheet(wb, "Line load", ["Line", "Date", "Planned worker-hours", "On-shift worker-hours (line)",
                                  "Load %"], load, [7, 11, 18, 24, 9])
    for row in ws.iter_rows(min_row=2):
        if row[4].value is not None and row[4].value >= 85:
            row[4].fill = OVER
    ws.append([])
    ws.append(["Workers may be assigned across lines (BATCH_LINE_THEN_FACTORY), so a line can exceed 100% of its own staff."])

    # Pace comparison: the CRAFT plan of each run (e.g. SLOW / NORMAL / FAST
    # on the same orders), so the manager chooses with the trade-offs in view.
    if compare:
        pace_rows = []
        for other in (run_dir, *map(Path, compare)):
            rep = json.loads((Path(other) / "report.json").read_text(encoding="utf-8"))
            craft = next((r for r in rep["results"] if r["policy"] == "CRAFT_V2"), None)
            if craft is None:
                continue
            k, w = craft.get("kpis") or {}, (craft.get("utilization") or {}).get("workers", {})
            pace_rows.append([rep.get("pace"), (rep.get("settings") or {}).get("max_crew"), rep.get("orders"),
                              k.get("on_time_orders"), k.get("total_tardiness_days"), k.get("max_tardiness_days"),
                              k.get("mean_flow_days"), k.get("makespan_days"), w.get("used"), w.get("util_used_pct"),
                              str(other)])
        _sheet(wb, "Pace comparison", ["Pace", "Max crew", "Orders", "On time", "Total lateness (days)",
                                       "Worst lateness (days)", "Mean flow (days)", "Last finish (day)",
                                       "Workers used", "Util. of used workers %", "Run"], pace_rows,
               [9, 9, 8, 8, 14, 14, 12, 12, 12, 14, 60])

    wb.save(out)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("factory", type=int)
    p.add_argument("run_dir")
    p.add_argument("--schedule", default="craft_v2")
    p.add_argument("--out")
    p.add_argument("--compare", nargs="*", default=(), help="more run dirs (same orders, other paces)")
    a = p.parse_args()
    out = a.out or str(Path(a.run_dir) / f"factory{a.factory}_{a.schedule.lower()}_plan.xlsx")
    print(build(a.factory, a.run_dir, a.schedule, out, a.compare))


if __name__ == "__main__":
    main()
