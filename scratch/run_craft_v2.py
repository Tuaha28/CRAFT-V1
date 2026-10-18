"""CRAFT V2 (dispatch start + CP-SAT LNS) vs FIFO/EDD/SPT, same physics.

Usage:
  venv/Scripts/python.exe scratch/run_craft_v2.py <factory_id> <out_dir>
      [--orders N] [--release-days D] [--pace NORMAL|FAST|SLOW]
      [--seconds 1800] [--rules FIFO EDD SPT] [--reuse <dir>]

1. Baselines as factories run them (one worker, no crews): the --rules.
2. EDD with greedy adaptive crews (the same crew options CRAFT has).
3. CRAFT V2: starts from the better of plain EDD and crew EDD, then improves
   it with time-window and resource-graph CP-SAT moves for --seconds.
Every schedule passes the same independent assembled validation. --reuse
takes already computed baseline schedules from an earlier output directory
(same selection), so a CRAFT rerun does not repeat the rule runs.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scratch")]
from crafter.production.pace import pace_environment, pace_settings  # noqa: E402

PACE = (sys.argv[sys.argv.index("--pace") + 1] if "--pace" in sys.argv else "NORMAL").upper()
os.environ.update(pace_environment(PACE))  # before CRAFT modules read the mode

from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402
from run_dispatch_baselines import dispatch_order  # noqa: E402
from run_lane_experiment import rule_keys, summarize  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("factory", type=int)
    p.add_argument("out")
    p.add_argument("--orders", type=int)
    p.add_argument("--release-days", type=int)
    p.add_argument("--pace", default="NORMAL", choices=["NORMAL", "FAST", "SLOW"])
    p.add_argument("--seconds", type=float, default=1800, help="CRAFT improvement budget")
    p.add_argument("--rules", nargs="*", default=["FIFO", "EDD", "SPT"])
    p.add_argument("--reuse", help="directory with earlier baseline *_schedule.json for this selection")
    p.add_argument("--skip-craft", action="store_true")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=False)
    settings = pace_settings(a.pace)
    crew = {"max_crew": settings["max_crew"], "bundle_pieces": 20}

    source, _r, snapshot, _h = runner._source(a.factory)
    eligible = runner._eligible_order_routes(source, snapshot)
    if a.release_days is not None:
        from datetime import datetime, timedelta
        cutoff = (datetime.fromisoformat(source["reference_time"]) + timedelta(days=a.release_days)).date().isoformat()
        eligible = [r for r in eligible if r[0][:10] <= cutoff]
    chosen = [oid for _rel, oid, _rows, _o in eligible]
    chosen = set(chosen[:a.orders] if a.orders else chosen)
    keys = [row["operation_key"] for _rel, oid, rows, _o in eligible if oid in chosen for row in rows]
    report = {"factory": a.factory, "pace": a.pace, "settings": settings, "orders": len(chosen),
              "operations": len(keys), "improve_seconds": a.seconds,
              "capture": os.environ.get("CRAFT_CAPTURE_DIR"), "results": []}
    print(json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)

    def save():
        (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")

    def record(result, rows, name):
        (out / f"{name.lower()}_schedule.json").write_text(json.dumps(rows), encoding="utf-8")
        report["results"].append(result)
        save()
        print(json.dumps({k: result[k] for k in ("policy", "scheduled", "deferred", "wall_seconds")}), flush=True)

    def reused(name):
        if not a.reuse:
            return None
        path = Path(a.reuse) / f"{name.lower()}_schedule.json"
        old = Path(a.reuse) / "report.json"
        if not path.exists() or not old.exists():
            return None
        prior = json.loads(old.read_text(encoding="utf-8"))
        if prior.get("operations") != len(keys) or prior.get("factory") != a.factory:
            raise SystemExit("REUSE_SELECTION_MISMATCH")
        result = next((r for r in prior["results"] if r["policy"] == name), None)
        return (result, json.loads(path.read_text(encoding="utf-8"))) if result else None

    by_rule, ops = rule_keys(source, snapshot)
    schedules = {}
    jobs = [(rule, rule, None) for rule in a.rules] + [("EDD_CREW", "EDD", crew)]
    if "EDD" not in a.rules:
        jobs.insert(0, ("EDD", "EDD", None))
    for name, rule, crew_policy in jobs:
        prior = reused(name)
        if prior is not None:
            result, rows = prior
            record({**result, "reused_from": a.reuse}, rows, name)
            schedules[name] = rows
            continue
        t = time.perf_counter()
        sequence = dispatch_order(keys, ops, by_rule[rule])
        output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint=f"V2-{name}",
                             route_ready_objective=True, window_sequence=[(k,) for k in sequence],
                             crew_policy=crew_policy, full_domain_windows=True)
        result, rows = summarize(name, a.factory, output, None, time.perf_counter() - t)
        record(result, rows, name)
        schedules[name] = rows

    if not a.skip_craft:
        t = time.perf_counter()
        output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="V2-CRAFT",
                             route_ready_objective=True, crew_policy=crew, improve="V2",
                             lns_seconds=a.seconds,
                             start_candidates=[("EDD", schedules["EDD"]), ("EDD_CREW", schedules["EDD_CREW"])])
        result, rows = summarize("CRAFT_V2", a.factory, output, None, time.perf_counter() - t)
        v2 = output["craft_v2"] or {}
        result["craft_v2"] = {k: v2.get(k) for k in ("pace", "starts", "seconds", "accepted_moves",
                                                    "improving_moves", "first_sweep_complete",
                                                    "weights", "final_score")}
        (out / "craft_v2_moves.json").write_text(json.dumps(v2.get("moves", []), indent=1), encoding="utf-8")
        record(result, rows, "CRAFT_V2")
        print(json.dumps(result["craft_v2"], default=str), flush=True)

    for r in report["results"]:
        k, u = r["kpis"] or {}, r["utilization"] or {}
        print(r["policy"], "complete", k.get("orders_complete"), "makespan_d", k.get("makespan_days"),
              "tardiness_d", k.get("total_tardiness_days"), "on_time", k.get("on_time_orders"),
              "flow_d", k.get("mean_flow_days"), "workers_used", u.get("workers", {}).get("used"),
              "worker_util_used%", u.get("workers", {}).get("util_used_pct"), flush=True)


if __name__ == "__main__":
    main()
