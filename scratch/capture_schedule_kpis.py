"""Policy-neutral KPIs for an approved captured-factory TEST schedule.

Usage: python scratch/capture_schedule_kpis.py <factory_id> <approved_schedule.json> [more.json ...]

Release/deadline semantics mirror run_factory_test._prepare exactly: seconds
from the capture reference time, clamped at zero (an order already past due
at the reference time has deadline 0). An order is "complete" only when every
operation of its eligible route is scheduled; KPIs are computed over complete
orders and coverage is reported, never silently mixed.
"""
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402


def kpis(factory_id, rows, lane_policy=None):
    """With ``lane_policy`` an order is complete only when every lane of it is;
    its finish is the latest lane finish (KPIs stay per original order)."""
    from crafter.production.lanes import base_order_id
    source, _readiness, snapshot, _hash = runner._source_for(factory_id, lane_policy)
    origin = datetime.fromisoformat(source["reference_time"])
    routes = {}
    for _release, order_id, route, _order in runner._eligible_order_routes(source, snapshot):
        routes.setdefault(base_order_id(order_id), []).extend(r["operation_key"] for r in route)
    orders = {base_order_id(o["production_order_id"]): o for o in source["orders"]}
    by_key = {r["operation_key"]: r for r in rows}
    if len(by_key) != len(rows):
        raise ValueError("DUPLICATE_OPERATION_IN_SCHEDULE")
    complete, partial = {}, []
    for order_id, keys in routes.items():
        present = [k for k in keys if k in by_key]
        if len(present) == len(keys):
            complete[order_id] = max(by_key[k]["end_seconds"] for k in keys)
        elif present:
            partial.append(order_id)
    tardiness, flow = [], []
    for order_id, finish in complete.items():
        order = orders[order_id]
        release = runner._date_seconds(origin, order.get("scheduled_start_date") or source["reference_time"])
        due = runner._date_seconds(origin, order.get("delivery_deadline"))
        tardiness.append(max(0, finish - due))
        flow.append(finish - release)
    start = min(r["start_seconds"] for r in rows)
    finish = max(r["end_seconds"] for r in rows)
    day = 86400
    return {
        "factory_id": factory_id, "operations": len(rows),
        "orders_complete": len(complete), "orders_partially_scheduled": len(partial),
        "eligible_orders": len(routes),
        "makespan_days": round(finish / day, 3), "schedule_span_days": round((finish - start) / day, 3),
        "on_time_orders": sum(t == 0 for t in tardiness),
        "on_time_pct": round(100 * sum(t == 0 for t in tardiness) / len(tardiness), 2) if tardiness else None,
        "total_tardiness_days": round(sum(tardiness) / day, 3),
        "mean_tardiness_days": round(statistics.mean(tardiness) / day, 3) if tardiness else None,
        "max_tardiness_days": round(max(tardiness) / day, 3) if tardiness else None,
        "mean_flow_days": round(statistics.mean(flow) / day, 3) if flow else None,
        "time_origin": source["reference_time"],
    }


if __name__ == "__main__":
    factory = int(sys.argv[1])
    schedule = []
    for path in sys.argv[2:]:
        schedule.extend(json.loads(Path(path).read_text(encoding="utf-8")))
    print(json.dumps(kpis(factory, schedule), indent=1))
