"""Build a what-if copy of one pinned factory capture (the base is never changed).

Usage (scheduler virtualenv):
  python web_v1/backend/integrations/craft/plan_scenario.py <base_capture_dir> <factory_id> <out_dir> <scenario.json>

scenario.json may contain:
  machines_down:   [asset ids]   -> status "Breakdown" (CRAFT plans only with Active machines)
  workers_absent:  [employee ids] -> on leave for this plan
  new_order:       {order_id, product_id, quantity, release, due, routing: [{operation_id,
                    operation_name, sequence, machine_type, sam}]}  -> an unstarted order
Both disruptions last the whole plan: the capture model has no time-limited
machine or worker outages. The copy is re-hashed with the runner's own
normalisation, so CRAFT verifies it exactly like a real capture, and the
scenario is recorded next to it.
"""
import copy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]


def build(base, factory_id, out, scenario):
    source = json.loads((base / f"factory{factory_id}_source.json").read_text(encoding="utf-8"))
    readiness = json.loads((base / f"factory{factory_id}_readiness.json").read_text(encoding="utf-8"))
    source = copy.deepcopy(source)
    machines = {int(m["asset_id"]): m for m in source["resources"]["machines"] if int(m["factory_id"]) == factory_id}
    workers = {int(w["employee_id"]) for w in source["resources"]["workers"] if int(w["factory_id"]) == factory_id}
    for asset in scenario.get("machines_down", ()):
        if int(asset) not in machines:
            raise SystemExit(f"MACHINE_NOT_IN_FACTORY:{asset}")
        machines[int(asset)]["current_status"] = "Breakdown"
    absent = {int(w) for w in scenario.get("workers_absent", ())}
    if absent - workers:
        raise SystemExit(f"WORKER_NOT_IN_FACTORY:{sorted(absent - workers)[0]}")
    rostered = set()
    for row in source["resources"]["work_schedules"]:
        if int(row["employee_id"]) in absent:
            row["is_on_leave"] = True
            rostered.add(int(row["employee_id"]))
    next_id = max(int(r["schedule_id"]) for r in source["resources"]["work_schedules"]) + 1
    for wid in sorted(absent - rostered):
        source["resources"]["work_schedules"].append({"employee_id": wid, "end_time": None, "is_on_leave": True,
                                                      "schedule_id": next_id, "shift_date": source["reference_time"][:10],
                                                      "start_time": None})
        next_id += 1

    order = scenario.get("new_order")
    if order:
        oid = int(order["order_id"])
        if any(int(o["production_order_id"]) == oid for o in source["orders"]):
            raise SystemExit(f"ORDER_ID_TAKEN:{oid}")
        qty = int(order["quantity"])
        reference = datetime.fromisoformat(source["reference_time"])
        release = max(date.fromisoformat(order["release"]), reference.date())
        due = date.fromisoformat(order["due"])
        if qty <= 0 or due < release or not order.get("routing"):
            raise SystemExit("NEW_ORDER_INVALID")
        source["orders"].append({"delivery_deadline": due.isoformat(), "factory_id": factory_id,
                                 "product_id": int(order["product_id"]), "production_order_id": oid,
                                 "scheduled_end_date": due.isoformat(), "scheduled_start_date": release.isoformat(),
                                 "status": "Pending", "target_quantity": qty})
        # An unstarted order carries one not-started lot per step: projected far after
        # the reference time, never a commitment (the same evidence a seeded order has).
        projected = reference + timedelta(days=365)
        next_item = max(int(lot["schedule_item_id"]) for lot in source["lots"]) + 1
        for step in sorted(order["routing"], key=lambda s: int(s["sequence"])):
            source["operations"].append({"operation_id": int(step["operation_id"]),
                                         "operation_name": step["operation_name"],
                                         "operation_sequence_number": int(step["sequence"]),
                                         "production_order_id": oid,
                                         "required_machine_type": step["machine_type"],
                                         "standard_allowed_minutes": str(step["sam"])})
            start, end = projected, projected + timedelta(minutes=1)
            stamp = lambda t: t.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
            source["lots"].append({"actual_end_time": end.isoformat(), "actual_start_time": start.isoformat(),
                                   "actual_work_segments": json.dumps([[stamp(start), stamp(end)]]),
                                   "asset_id": None, "batch_id": None, "employee_id": None,
                                   "execution_status": "Scheduled", "line_id": None, "lot_index": 0,
                                   "lot_quantity": qty, "operation_id": int(step["operation_id"]),
                                   "planned_end_time": end.isoformat(), "planned_start_time": start.isoformat(),
                                   "planned_work_segments": json.dumps([[stamp(start), stamp(end)]]),
                                   "production_order_id": oid, "schedule_item_id": next_item})
            next_item += 1

    from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot
    out.mkdir(parents=True, exist_ok=False)
    raw = json.dumps(source, sort_keys=True, indent=1).encode("utf-8")
    (out / f"factory{factory_id}_source.json").write_bytes(raw)
    snapshot = normalize_snapshot(json.loads(raw))
    readiness.update(raw_file_sha256=hashlib.sha256(raw).hexdigest(),
                     normalized_snapshot_hash=snapshot["snapshot_hash"],
                     normalized_input_hash=snapshot["input_hash"],
                     operation_count=len(snapshot["operations"]),
                     scenario={"base_capture": str(base), "base_raw_file_sha256": readiness.get("raw_file_sha256"),
                               **{k: v for k, v in scenario.items() if k != "new_order"},
                               "new_order": {k: v for k, v in (order or {}).items() if k != "routing"} or None})
    (out / f"factory{factory_id}_readiness.json").write_text(json.dumps(readiness, indent=1), encoding="utf-8")
    (out / "scenario.json").write_text(json.dumps(scenario, indent=1), encoding="utf-8")


def verify(out, factory_id, scenario):
    """Load the copy through the runner (hash checks) and confirm the changes took."""
    os.environ["CRAFT_CAPTURE_DIR"] = str(out)
    from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner
    source, _readiness, snapshot, _hash = runner._source(factory_id)
    eligible = {oid: rows for _rel, oid, rows, _o in runner._eligible_order_routes(source, snapshot)}
    report = {"eligible_orders": len(eligible)}
    order = scenario.get("new_order")
    if order:
        rows = eligible.get(str(order["order_id"]))
        if not rows or len(rows) != len(order["routing"]):
            raise SystemExit("NEW_ORDER_NOT_ELIGIBLE")
        report["new_order_operations"] = len(rows)
    down = {int(m["asset_id"]) for m in source["resources"]["machines"] if m["current_status"] != "Active"}
    if not {int(a) for a in scenario.get("machines_down", ())} <= down:
        raise SystemExit("MACHINE_STILL_ACTIVE")
    leave = {int(w["employee_id"]) for w in source["resources"]["work_schedules"] if w["is_on_leave"]}
    if not {int(w) for w in scenario.get("workers_absent", ())} <= leave:
        raise SystemExit("WORKER_NOT_ON_LEAVE")
    return report


if __name__ == "__main__":
    base, factory, out, spec = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    scenario = json.loads(spec.read_text(encoding="utf-8"))
    build(base, factory, out, scenario)
    print("SCENARIO_OK", json.dumps(verify(out, factory, scenario)))
