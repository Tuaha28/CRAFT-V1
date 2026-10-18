"""CRAFT vs FIFO/EDD/SPT with LANE_SPLIT_V1 bundle lanes, same physics.

Usage:
  venv/Scripts/python.exe scratch/run_lane_experiment.py <factory_id> <out_dir>
      [--orders N] [--bundle 20] [--max-lanes 4] [--window 15] [--rules FIFO EDD SPT] [--no-lanes]

Selects the first N admissible orders (release order, whole routes) or all,
splits them into lanes, schedules them with CRAFT (packed windows, route-ready
lexicographic objective) and with each dispatch rule (serial SGS, one-operation
windows) over exactly the same lane operations, then reports order-level KPIs
and utilization. Every accepted schedule passes the same independent validation.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402
from crafter.production.lanes import base_order_id, lane_policy  # noqa: E402
from capture_schedule_kpis import kpis  # noqa: E402
from capture_utilization import utilization  # noqa: E402
from run_dispatch_baselines import dispatch_order  # noqa: E402


def rule_keys(source, snapshot):
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    sam = {f'{r["production_order_id"]}_{r["operation_id"]}': float(r.get("standard_allowed_minutes") or 0)
           for r in source["operations"]}
    ops = {op["operation_key"]: op for op in snapshot["operations"]}

    def common(key):
        return (str(ops[key]["order_id"]), int(ops[key]["routing_sequence"]), key)

    return {
        "FIFO": lambda k: (str(orders[str(ops[k]["order_id"])].get("scheduled_start_date") or ""),) + common(k),
        "EDD": lambda k: (str(orders[str(ops[k]["order_id"])].get("delivery_deadline") or "9999"),) + common(k),
        "SPT": lambda k: (sam.get(k, 0.0) * int(ops[k]["quantity"]["not_started"]),) + common(k),
    }, ops


def summarize(name, factory, output, policy, wall):
    rows = output["selected_schedule"]
    validation = output["assembled_schedule_validation"]
    if rows and not validation.get("accepted"):
        raise RuntimeError(f"ASSEMBLED_VALIDATION_FAILED:{name}")
    return {"policy": name, "scheduled": len(rows), "deferred": len(output["deferred_operations"]),
            "assembled_validation": validation.get("accepted"), "wall_seconds": round(wall, 1),
            "peak_rss_mb": round(output["sampled_peak_rss_mb"]),
            "kpis": kpis(factory, rows, policy) if rows else None,
            "utilization": utilization(factory, rows) if rows else None}, rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("factory", type=int)
    p.add_argument("out")
    p.add_argument("--orders", type=int)
    p.add_argument("--release-days", type=int, help="orders released up to N days after the reference date")
    p.add_argument("--skip-craft", action="store_true", help="run only the dispatch rules")
    p.add_argument("--bundle", type=int, default=20)
    p.add_argument("--max-lanes", type=int, default=4)
    p.add_argument("--window", type=int, default=15)
    p.add_argument("--rules", nargs="*", default=["FIFO", "EDD", "SPT"])
    p.add_argument("--no-lanes", action="store_true", help="control: same orders, unsplit")
    p.add_argument("--window-order", choices=["DUE_DATE"], help="CRAFT window feeding order (default: release)")
    p.add_argument("--crew", type=int, help="adaptive crew sizing: max workers per operation (all policies)")
    p.add_argument("--guard", action="store_true", help="CRAFT keeps the better of its window plan and EDD's")
    p.add_argument("--lookahead", type=int, default=0, help="CRAFT lookahead operations per window")
    p.add_argument("--lns", type=float, default=0, help="CRAFT whole-schedule ALNS refinement seconds")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=False)
    policy = None if a.no_lanes else lane_policy(a.bundle, a.max_lanes)
    crew = {"max_crew": a.crew, "bundle_pieces": a.bundle} if a.crew else None
    base_source, _r, base_snapshot, _h = runner._source(a.factory)
    eligible = runner._eligible_order_routes(base_source, base_snapshot)
    if a.release_days is not None:
        from datetime import datetime, timedelta
        cutoff = (datetime.fromisoformat(base_source["reference_time"]) + timedelta(days=a.release_days)).date().isoformat()
        eligible = [r for r in eligible if r[0][:10] <= cutoff]
    chosen = [oid for _rel, oid, _rows, _o in eligible]
    chosen = set(chosen[:a.orders] if a.orders else chosen)
    source, _r, snapshot, _h = runner._source_for(a.factory, policy)
    routes = runner._eligible_order_routes(source, snapshot)
    keys = [row["operation_key"] for _rel, oid, rows, _o in routes if base_order_id(oid) in chosen for row in rows]
    report = {"factory": a.factory, "lane_policy": policy, "window_order": a.window_order, "crew_policy": crew, "dispatch_guard": a.guard, "lookahead": a.lookahead, "lns_seconds": a.lns, "orders": len(chosen), "operations": len(keys),
              "window": a.window, "results": []}
    print(json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)

    def save():
        (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")

    def record(result, rows, name):
        (out / f"{name.lower()}_schedule.json").write_text(json.dumps(rows), encoding="utf-8")
        report["results"].append(result)
        save()
        print(json.dumps({k: result[k] for k in ("policy", "scheduled", "deferred", "wall_seconds")}), flush=True)

    t = time.perf_counter()
    craft = None if a.skip_craft else runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="LANE-EXPERIMENT",
                        route_ready_objective=True, window_size=a.window, lane_policy=policy,
                        window_order=a.window_order, crew_policy=crew,
                        dispatch_guard=a.guard, lookahead=a.lookahead,
                        lns_seconds=a.lns)
    if craft is not None:
        record(*summarize("CRAFT", a.factory, craft, policy, time.perf_counter() - t), "CRAFT")
    if craft is not None and craft.get("lns"):
        report["craft_lns"] = craft["lns"]
        print(json.dumps({"lns_accepted_moves": craft["lns"]["accepted_moves"], "lns_moves": len(craft["lns"]["moves"])}), flush=True)

    by_rule, ops = rule_keys(source, snapshot)
    for rule in a.rules:
        t = time.perf_counter()
        sequence = dispatch_order(keys, ops, by_rule[rule])
        output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint=f"LANE-{rule}",
                             route_ready_objective=True, window_sequence=[(k,) for k in sequence],
                             lane_policy=policy, crew_policy=crew)
        record(*summarize(rule, a.factory, output, policy, time.perf_counter() - t), rule)
    for r in report["results"]:
        k, u = r["kpis"] or {}, r["utilization"] or {}
        print(r["policy"], "complete", k.get("orders_complete"), "makespan_d", k.get("makespan_days"),
              "tardiness_d", k.get("total_tardiness_days"), "on_time", k.get("on_time_orders"),
              "flow_d", k.get("mean_flow_days"), "workers_used", u.get("workers", {}).get("used"),
              "worker_util_used%", u.get("workers", {}).get("util_used_pct"), flush=True)


if __name__ == "__main__":
    main()
