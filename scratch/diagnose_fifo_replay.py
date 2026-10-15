import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from crafter.baselines.fifo_v1 import build_workload_from_neutral_payload
from crafter.repair.calendar_regimes import _execution
from crafter.repair.scoped_validation import _config

neutral = json.loads((root / "outputs/crafter/evaluation_v2/neutral_factory2_state.json").read_text())
schedule = json.loads((root / "outputs/crafter/baselines/fifo_factory2_v1/fifo_schedule.json").read_text())["schedule"]
workload = build_workload_from_neutral_payload(neutral)
origin = workload.problem.origin
base = defaultdict(list)
for row in neutral["resource_commitments"]["worker"]:
    base[str(row["employee_id"])].append((origin + timedelta(seconds=int(row["start_seconds"])), origin + timedelta(seconds=int(row["end_seconds"]))))
all_segments = []
for candidate in schedule:
    cfg = _config(candidate)
    for left, right in candidate.get("worker_segments", ()):
        all_segments.append((str(candidate["operation_key"]), cfg.worker_ids, int(left), int(right)))

def key(row):
    segs = row.get("worker_segments") or ()
    first = int(segs[0][0]) if segs else int(row["start_seconds"])
    return first, int(row["start_seconds"]), int(row["end_seconds"]), str(row["operation_key"])

for row in sorted(schedule, key=key):
    cfg = _config(row)
    worker_res = {worker: list(values) for worker, values in base.items()}
    for other_key, workers, left, right in all_segments:
        if other_key == cfg.operation_key:
            continue
        for worker in workers:
            worker_res.setdefault(str(worker), []).append((origin + timedelta(seconds=left), origin + timedelta(seconds=right)))
    for values in worker_res.values():
        values.sort()
    try:
        end, segments = _execution({"problem": workload.problem, "operator_index": workload.operator_index, "_worker_res": worker_res}, cfg, int(row["start_seconds"]))
    except Exception as exc:
        print(json.dumps({
            "operation_key": cfg.operation_key, "exception": type(exc).__name__, "message": str(exc),
            "start": row["start_seconds"], "end": row["end_seconds"],
            "workers": list(cfg.worker_ids), "segments": row["worker_segments"],
            "other_reservations": {
                worker: [[int((a-origin).total_seconds()), int((b-origin).total_seconds())] for a,b in worker_res.get(worker, ())]
                for worker in cfg.worker_ids
            },
        }, indent=2))
        break
    actual = [[int(a), int(b)] for a,b in segments]
    if int(end) != int(row["end_seconds"]) or actual != row["worker_segments"]:
        print(json.dumps({"operation_key":cfg.operation_key,"type":"MISMATCH","expected_end":row["end_seconds"],"actual_end":int(end),"expected":row["worker_segments"],"actual":actual},indent=2))
        break
else:
    print("PASS")
