"""Orders of each test case that are already due on or before the reference time:
late for every method, whatever the schedule (finish > reference >= due).
Uses the runner's own eligible-order list, in the release order the order sets
are taken (--order-offset 0, 10, 20). Run with the scheduler virtualenv:
  python deliverables/paper/results/overdue.py <repo root> <out overdue.json>"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
CAPTURES = {"load30": "capture_20261018T043552523848Z", "load60": "capture_20261018T084010774436Z",
            "load90": "capture_20261018T084816687698Z"}

if len(sys.argv) == 3:  # driver: one child process per capture (the runner reads its capture once)
    result = {}
    for load in CAPTURES:
        out = subprocess.run([sys.executable, __file__, str(ROOT), "-", load], capture_output=True, text=True,
                             check=True, env=dict(os.environ, CRAFT_RESIDUAL_ROUTES="1", DB_PASS="unused",
                                                  CRAFT_WORKER_LINE_POLICY="BATCH_LINE_THEN_FACTORY"))
        result.update(json.loads(out.stdout.strip().splitlines()[-1]))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result))
else:
    load = sys.argv[3]
    os.environ["CRAFT_CAPTURE_DIR"] = str(ROOT / "web_v1/audits/factory_source_integration_v1" / CAPTURES[load])
    sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
    from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner
    source, _r, snapshot, _h = runner._source(4)
    reference = source["reference_time"][:10]
    eligible = runner._eligible_order_routes(source, snapshot)
    counts = {}
    for number, offset in ((1, 0), (2, 10), (3, 20)):
        orders = [order for _rel, _oid, _rows, order in eligible[offset:offset + 10]]
        counts[f"{load}_set{number}"] = sum(1 for o in orders if str(o["delivery_deadline"])[:10] <= reference)
    print(json.dumps(counts))
