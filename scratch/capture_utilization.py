"""Machine / worker utilization of an approved captured-factory TEST schedule.

Usage: python scratch/capture_utilization.py <factory_id> <schedule.json> [more.json ...]

Horizon is [reference time, last scheduled finish]. Busy time is the
productive worker segments CRAFT committed (setup included), never the
start-to-end hold. Pools follow run_factory_test._prepare's TEST eligibility:
  workers  - active, in the factory, member of an active batch, not on leave;
             available time = their batch shift rotation minus rest days
             (utils.shift_rotation.batch_available_at, 30-min midpoints);
  machines - Active, in the factory, on an active line; there is no machine
             calendar in the capture, so machine utilization is reported on a
             24/7 basis and on a staffed basis (hours any eligible same-line
             worker is on shift).
Idle resources stay in the denominator: "used" and "pool" figures are both shown.
"""
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.shift_rotation import batch_available_at  # noqa: E402
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402

STEP = 1800


def _on_shift_steps(crew, origin, steps):
    rotation, rest = crew
    return [batch_available_at(rotation, rest, origin + timedelta(seconds=i * STEP + STEP // 2))
            for i in range(steps)]


def utilization(factory_id, rows):
    source, _readiness, _snapshot, _hash = runner._source(factory_id)
    origin = datetime.fromisoformat(source["reference_time"])
    res = source["resources"]
    lines = {int(r["line_id"]) for r in res["lines"] if int(r["factory_id"]) == factory_id and r["is_active"]}
    batches = {int(r["batch_id"]): r for r in res["batches"] if r["is_active"]}
    member = {int(r["employee_id"]): int(r["batch_id"]) for r in res["batch_members"]}
    leave = {int(r["employee_id"]) for r in res["work_schedules"] if r["is_on_leave"]}
    workers = {str(w["employee_id"]): batches[member[int(w["employee_id"])]]
               for w in res["workers"]
               if int(w["factory_id"]) == factory_id and w["is_active"] and int(w["employee_id"]) in member
               and member[int(w["employee_id"])] in batches and int(w["employee_id"]) not in leave}
    machines = {int(m["asset_id"]): int(m["line_id"]) for m in res["machines"]
                if int(m["factory_id"]) == factory_id and m["current_status"] == "Active" and int(m["line_id"]) in lines}

    horizon = max(r["end_seconds"] for r in rows)
    steps = -(-horizon // STEP)
    worker_busy, machine_busy = defaultdict(int), defaultdict(int)
    for row in rows:
        cfg = row["resource_configuration"]
        productive = sum(b - a for a, b in row["worker_segments"])
        for wid in cfg["worker_ids"]:
            worker_busy[str(wid)] += productive
        for mid in cfg["machine_ids"]:
            machine_busy[int(mid)] += productive
    unknown = (set(worker_busy) - set(workers)) | {str(m) for m in set(machine_busy) - set(machines)}
    if unknown:
        raise ValueError(f"SCHEDULED_RESOURCE_OUTSIDE_TEST_POOL:{sorted(unknown)[:5]}")

    crew_steps = {}
    worker_avail = {}
    for wid, batch in workers.items():
        crew = (int(batch["rotation_offset"]), int(batch["rest_day_offset"]))
        if crew not in crew_steps:
            crew_steps[crew] = _on_shift_steps(crew, origin, steps)
        worker_avail[wid] = sum(crew_steps[crew]) * STEP
    line_staffed = {}
    for line in lines:
        crews = {(int(b["rotation_offset"]), int(b["rest_day_offset"])) for b in workers.values()
                 if int(b["line_id"]) == line}
        line_staffed[line] = sum(any(crew_steps[c][i] for c in crews) for i in range(steps)) * STEP if crews else 0

    def pct(busy, avail):
        return round(100 * busy / avail, 2) if avail else None

    used_w = sorted(worker_busy)
    used_m = sorted(machine_busy)
    per_worker = [pct(worker_busy[w], worker_avail[w]) for w in used_w]
    per_machine_staffed = [pct(machine_busy[m], line_staffed[machines[m]]) for m in used_m]
    by_line = {}
    for line in sorted(lines):
        lw = [w for w, b in workers.items() if int(b["line_id"]) == line]
        lm = [m for m, l in machines.items() if l == line]
        if not lw and not lm:
            continue
        by_line[line] = {
            "workers": len(lw), "workers_used": sum(w in worker_busy for w in lw),
            "worker_util_pct": pct(sum(worker_busy[w] for w in lw), sum(worker_avail[w] for w in lw)),
            "machines": len(lm), "machines_used": sum(m in machine_busy for m in lm),
            "machine_util_staffed_pct": pct(sum(machine_busy[m] for m in lm), line_staffed[line] * len(lm)),
        }
    def spread(xs):
        xs = [x for x in xs if x is not None]  # resources with no staffed time are left out
        return {"min": min(xs), "median": statistics.median(xs), "max": max(xs)} if xs else None
    return {
        "factory_id": factory_id, "operations": len(rows), "horizon_days": round(horizon / 86400, 3),
        "workers": {"pool": len(workers), "used": len(used_w),
                    "util_pool_pct": pct(sum(worker_busy.values()), sum(worker_avail.values())),
                    "util_used_pct": pct(sum(worker_busy.values()), sum(worker_avail[w] for w in used_w)),
                    "per_used_worker_pct": spread(per_worker)},
        "machines": {"pool": len(machines), "used": len(used_m),
                     "util_pool_24x7_pct": pct(sum(machine_busy.values()), horizon * len(machines)),
                     "util_pool_staffed_pct": pct(sum(machine_busy.values()),
                                                  sum(line_staffed[l] for l in machines.values())),
                     "util_used_staffed_pct": pct(sum(machine_busy.values()),
                                                  sum(line_staffed[machines[m]] for m in used_m)),
                     "per_used_machine_staffed_pct": spread(per_machine_staffed)},
        "by_line": by_line,
        "basis": "busy=committed productive worker segments; horizon=[reference_time, last finish]",
    }


if __name__ == "__main__":
    factory = int(sys.argv[1])
    schedule = []
    for path in sys.argv[2:]:
        schedule.extend(json.loads(Path(path).read_text(encoding="utf-8")))
    print(json.dumps(utilization(factory, schedule), indent=1))
