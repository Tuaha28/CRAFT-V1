"""Digest of one finished CRAFT V3 run for the web app (reporting only).

Usage (scheduler virtualenv, CRAFT_CAPTURE_DIR set as for the run):
  python web_v1/backend/integrations/craft/plan_digest.py <factory_id> <run_dir>

Reads <run_dir>/report.json and *_schedule.json written by
scratch/run_craft_v2.py, writes the factory Excel workbook
(scratch/export_craft_workbook.py) and <run_dir>/digest.json: per-method
KPIs, per-order due dates and finishes, and the compact CRAFT V3 and
EDD-Crew schedules for the Gantt. Never changes a schedule or the capture.
"""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
from openpyxl import load_workbook  # noqa: E402

from export_craft_workbook import build  # noqa: E402
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402

DAY = 86400
GANTT_POLICIES = ("CRAFT_V3", "EDD_CREW")


def method_row(result, orders):
    k, w = result.get("kpis") or {}, (result.get("utilization") or {}).get("workers", {})
    on_time = k.get("on_time_orders") or 0
    return {"policy": result["policy"], "scheduled": result["scheduled"], "deferred": result["deferred"],
            "complete": k.get("orders_complete", 0), "partial": k.get("orders_partially_scheduled", 0),
            "on_time": on_time, "late": orders - on_time,  # an unfinished order is not on time
            "total_tardiness_days": k.get("total_tardiness_days"),
            "mean_tardiness_days": (round(k["total_tardiness_days"] / orders, 3)
                                    if k.get("total_tardiness_days") is not None and orders else None),
            "max_tardiness_days": k.get("max_tardiness_days"), "makespan_days": k.get("makespan_days"),
            "mean_flow_days": k.get("mean_flow_days"), "workers_used": w.get("used"),
            "worker_pool": w.get("pool"), "util_used_pct": w.get("util_used_pct"),
            "wall_seconds": result.get("wall_seconds")}


def main():
    factory_id, run_dir = int(sys.argv[1]), Path(sys.argv[2])
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    source, _readiness, snapshot, _hash = runner._source(factory_id)
    origin = datetime.fromisoformat(source["reference_time"])
    snap_ops = {op["operation_key"]: op for op in snapshot["operations"]}
    meta = {f'{o["production_order_id"]}_{o["operation_id"]}': o for o in source["operations"]}
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    schedules = {}
    for result in report["results"]:
        path = run_dir / f'{result["policy"].lower()}_schedule.json'
        schedules[result["policy"]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    planned = {str(snap_ops[r["operation_key"]]["order_id"])
               for rows in schedules.values() for r in rows}

    workbook = run_dir / f"factory{factory_id}_craft_v3_plan.xlsx"
    reasons = {}
    if schedules.get("CRAFT_V3"):
        build(factory_id, run_dir, "craft_v3", str(workbook), client=True)  # the client sees the CRAFT plan only
        sheet = load_workbook(workbook, read_only=True)["Order status"]
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if row and row[0] is not None:
                reasons[str(row[0])] = {"status": row[4], "reason": row[8]}

    def finish_of(policy, keys):
        rows = {r["operation_key"]: r for r in schedules.get(policy, ())}
        ends = [int(rows[k]["end_seconds"]) for k in keys if k in rows]
        return {"finish_day": round(max(ends) / DAY, 3) if ends else None,
                "complete": len(ends) == len(keys)}

    order_rows = []
    for _release, oid, route, _order in runner._eligible_order_routes(source, snapshot):
        if str(oid) not in planned:
            continue
        order = orders[str(oid)]
        keys = [r["operation_key"] for r in route]
        release = runner._date_seconds(origin, order.get("scheduled_start_date") or source["reference_time"])
        due = runner._date_seconds(origin, order.get("delivery_deadline"))
        order_rows.append({"order_id": str(oid), "pieces": order.get("target_quantity"),
                           "release_day": round(max(0, release) / DAY, 3),
                           "due_day": round(due / DAY, 3) if due is not None else None,
                           "steps": len(keys),
                           "methods": {p: finish_of(p, keys) for p in schedules},
                           **reasons.get(str(oid), {})})

    gantt = {}
    for policy in GANTT_POLICIES:
        gantt[policy] = [{"key": r["operation_key"], "order_id": str(snap_ops[r["operation_key"]]["order_id"]),
                          "step": int(snap_ops[r["operation_key"]]["routing_sequence"]),
                          "name": meta.get(r["operation_key"], {}).get("operation_name"),
                          "machine_type": meta.get(r["operation_key"], {}).get("required_machine_type"),
                          "line": r["resource_configuration"]["line_id"],
                          "start": int(r["start_seconds"]), "end": int(r["end_seconds"]),
                          "crew": int(r["resource_configuration"].get("k", 1)),
                          "pieces": r["resource_configuration"].get("quantity"),
                          "machines": [str(m) for m in r["resource_configuration"]["machine_ids"]],
                          "workers": [str(w) for w in r["resource_configuration"]["worker_ids"]]}
                         for r in schedules.get(policy, ())]

    v3 = next((r for r in report["results"] if r["policy"] == "CRAFT_V3"), {})
    craft = v3.get("craft_v3") or {}
    digest = {"factory_id": factory_id, "reference_time": source["reference_time"],
              "orders": report.get("orders"), "operations": report.get("operations"),
              "improve_seconds": report.get("improve_seconds"), "settings": report.get("settings"),
              "methods": [method_row(r, report.get("orders") or 0) for r in report["results"]],
              "craft_v3": {"start_policy_used": v3.get("start_policy_used"),
                           "kept_start": next((s["start"] for s in craft.get("starts") or () if s.get("chosen")), None),
                           "starts": craft.get("starts"), "final_score": craft.get("final_score"),
                           "floor_used": craft.get("floor_used"),
                           "accepted_moves": craft.get("accepted_moves"),
                           "improving_moves": craft.get("improving_moves")},
              "order_rows": order_rows, "gantt": gantt,
              "workbook": workbook.name if workbook.exists() else None}
    (run_dir / "digest.json").write_text(json.dumps(digest, default=str), encoding="utf-8")
    print("DIGEST_OK", len(order_rows), sum(len(v) for v in gantt.values()))


if __name__ == "__main__":
    main()
