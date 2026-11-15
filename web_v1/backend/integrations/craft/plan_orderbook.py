"""Eligible order book of one pinned factory capture, in the order CRAFT takes
order sets (release order, as scratch/run_craft_v2.py --order-offset does).

Usage (scheduler virtualenv, CRAFT_CAPTURE_DIR set):
  python web_v1/backend/integrations/craft/plan_orderbook.py <factory_id> <out.json>
Reporting only; reads the verified capture through the runner.
"""
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402

DAY = 86400


def main():
    factory_id, out = int(sys.argv[1]), Path(sys.argv[2])
    source, _readiness, snapshot, source_hash = runner._source(factory_id)
    origin = datetime.fromisoformat(source["reference_time"])
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    names = {f'{o["production_order_id"]}_{o["operation_id"]}': o for o in source["operations"]}
    book = []
    for release, oid, route, _order in runner._eligible_order_routes(source, snapshot):
        order = orders[str(oid)]
        due = runner._date_seconds(origin, order.get("delivery_deadline"))
        work = sum(float(names.get(r["operation_key"], {}).get("standard_allowed_minutes") or 0)
                   * int(r["quantity"]["not_started"]) for r in route)
        book.append({"order_id": str(oid), "release": str(release)[:10],
                     "due": str(order.get("delivery_deadline") or "")[:10],
                     "due_day": round(due / DAY, 2) if due is not None else None,
                     "pieces": order.get("target_quantity"), "steps": len(route),
                     "work_hours": round(work / 60, 1),
                     "first_step": names.get(route[0]["operation_key"], {}).get("operation_name") if route else None})
    res = source["resources"]
    out.write_text(json.dumps({"factory_id": factory_id, "reference_time": source["reference_time"],
                               "source_sha256": source_hash, "orders_in_capture": len(source["orders"]),
                               "eligible": book,
                               "resources": {k: len(v) for k, v in res.items() if isinstance(v, list)}}),
                   encoding="utf-8")
    print("ORDERBOOK_OK", len(book))


if __name__ == "__main__":
    main()
