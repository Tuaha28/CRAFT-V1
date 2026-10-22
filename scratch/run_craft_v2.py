"""CRAFT V2 (dispatch start + CP-SAT LNS) vs FIFO/EDD/SPT, same physics.

Usage:
  venv/Scripts/python.exe scratch/run_craft_v2.py <factory_id> <out_dir>
      [--orders N] [--release-days D] [--pace NORMAL|FAST|SLOW]
      [--seconds 1800] [--rules FIFO EDD SPT] [--reuse <dir>]

1. Baselines as factories run them (one worker, no crews): the --rules.
2. EDD with greedy adaptive crews (the same crew options CRAFT has).
3. CRAFT V2: starts from the best of plain EDD, crew EDD and crew
   SAVABLE_FIRST (orders that can still be on time by due date, then the
   rest shortest first), then improves it with CP-SAT moves for --seconds.
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

if "--horizon-extra-days" in sys.argv:  # read by the runner when it builds the problem
    os.environ["CRAFT_HORIZON_EXTRA_DAYS"] = sys.argv[sys.argv.index("--horizon-extra-days") + 1]
# CP-SAT search threads = this machine's logical cores (8 threads on a
# 4-thread laptop only time-share); override with CRAFT_SOLVER_WORKERS.
os.environ.setdefault("CRAFT_SOLVER_WORKERS", str(os.cpu_count() or 4))
PACE = (sys.argv[sys.argv.index("--pace") + 1] if "--pace" in sys.argv else "NORMAL").upper()
os.environ.update(pace_environment(PACE))  # before CRAFT modules read the mode

from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402
from run_dispatch_baselines import dispatch_order  # noqa: E402
from run_lane_experiment import rule_keys, summarize  # noqa: E402


SHIFT_HOURS_ESTIMATE = 8  # only to classify orders as savable; never scheduling physics


def savable_first(source, ops, crew):
    """CRAFT start order (Moore-Hodgson idea: protect the orders that can
    still be on time). An order is savable when its remaining route at the
    widest allowed crew, at about one shift per day, fits before its due
    date. Savable orders go first by due date; the rest (already late or
    too big to make it) after, shortest remaining work first, so one huge
    late order cannot hold every crew while saveable orders wait."""
    from datetime import datetime
    origin = datetime.fromisoformat(source["reference_time"])
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    sam = {f'{r["production_order_id"]}_{r["operation_id"]}': float(r.get("standard_allowed_minutes") or 0)
           for r in source["operations"]}
    work = {}
    for key, op in ops.items():
        qty = int(op["quantity"]["not_started"])
        k = max(1, min(crew["max_crew"], qty // crew["bundle_pieces"]))
        work[str(op["order_id"])] = work.get(str(op["order_id"]), 0.0) + sam.get(key, 0.0) * qty / k / 60

    def day(value):
        return (datetime.fromisoformat(str(value)) - origin).total_seconds() / 86400 if value else None

    def rank(order_id):
        order = orders[order_id]
        release, due = max(0.0, day(order.get("scheduled_start_date")) or 0.0), day(order.get("delivery_deadline"))
        if due is not None and due > 0 and release + work[order_id] / SHIFT_HOURS_ESTIMATE <= due:
            return (0, due)
        return (1, work[order_id])

    return lambda k: rank(str(ops[k]["order_id"])) + (str(ops[k]["order_id"]), int(ops[k]["routing_sequence"]), k)


def rolling_v3(a, source, keys, ops, by_rule, crew):
    """CRAFT V3 the way a factory plans: cycle by cycle. Each cycle takes the
    most urgent released orders (release within --plan-days, at most
    --cycle-orders orders), runs locked V3 on them (rule
    starts, CP-SAT time windows, CP-SAT improvement from both starts) with
    every earlier frozen operation as a fixed, validated commitment, and
    freezes every order that starts in the first --freeze-days, whole route as
    planned (an order on the floor keeps its plan). Nothing new starts before
    the cycle start: the cycle's orders get it as their earliest start. V3
    itself is unchanged; only its inputs differ per cycle."""
    from datetime import datetime, timedelta
    origin = datetime.fromisoformat(source["reference_time"])
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    ops_of = {}
    for key in keys:
        ops_of.setdefault(str(ops[key]["order_id"]), []).append(key)

    def day(value):
        return (datetime.fromisoformat(str(value)[:10]) - origin).total_seconds() / 86400 if value else 0.0

    release = {o: day(orders[o].get("scheduled_start_date")) for o in ops_of}
    due = {o: day(orders[o].get("delivery_deadline")) if orders[o].get("delivery_deadline") else 1e9 for o in ops_of}
    original_source, frozen, cycles, now = runner._source, {}, [], 0
    tw = {"hours": a.tw_hours, "commit_hours": a.tw_commit_hours, "cap": a.tw_cap}
    while len(frozen) < len(keys):
        if len(cycles) >= 200:
            raise SystemExit("ROLLING_CYCLE_LIMIT")
        open_orders = [o for o in ops_of if any(k not in frozen for k in ops_of[o])]
        started = [o for o in open_orders if any(k in frozen for k in ops_of[o])]
        ready = sorted((o for o in open_orders if o not in started and release[o] <= now + a.plan_days),
                       key=lambda o: (due[o], release[o], o))
        scope = started + ready[:max(0, a.cycle_orders - len(started))]
        if not scope:  # nothing released yet: jump to the next release
            now = max(now + 1, int(min(release[o] for o in open_orders) - a.plan_days))
            continue
        last = len(scope) == len(open_orders)
        floor = (origin + timedelta(days=now)).date().isoformat()

        def floored(factory_id, _floor=floor, _scope=set(scope)):
            src = original_source(factory_id)  # (source, readiness, snapshot, hash)
            for order in src[0]["orders"]:
                if str(order["production_order_id"]) in _scope:
                    order["scheduled_start_date"] = max(str(order.get("scheduled_start_date") or floor)[:10], _floor)
            return src

        runner._source = floored
        try:
            cycle_started = time.perf_counter()
            approved = list(frozen.values())
            pending = [k for o in scope for k in ops_of[o] if k not in frozen]
            # Frozen operations go in only as approved (fixed, validated) rows.
            common = dict(selected_keys=pending, approved_rows=approved, route_ready_objective=True,
                          full_domain_windows=True)
            starts = {}
            # Plain EDD has no crew domain, so it cannot validate frozen crew
            # rows; after the first cycle EDD_CREW is the EDD start.
            rule_runs = ((("EDD", "EDD", None),) if not approved else ()) + (
                ("EDD_CREW", "EDD", crew), ("SAVABLE_FIRST_CREW", "SAVABLE_FIRST", crew))
            for name, rule, crew_policy in rule_runs:
                output = runner._run(a.factory, "all", approval_fingerprint=f"ROLL-{len(cycles)}-{name}",
                                     window_sequence=[(k,) for k in dispatch_order(pending, ops, by_rule[rule])],
                                     crew_policy=crew_policy, **common)
                starts[name] = output["selected_schedule"]
            output = runner._run(a.factory, "all", approval_fingerprint=f"ROLL-{len(cycles)}-TW",
                                 crew_policy=crew, time_windows=tw, **common)
            candidates = [("CRAFT_TW", output["selected_schedule"])] + [(n, starts[n]) for n in
                                                                       ("EDD_CREW", "SAVABLE_FIRST_CREW", "EDD")
                                                                       if n in starts]
            runs = []
            for policy in ("FIRST", "BEST"):
                if policy == "BEST" and runs:
                    best_start = min((s for s in runs[0][1]["craft_v2"]["starts"] if s.get("score") is not None),
                                     key=lambda s: tuple(s["score"]))["start"]
                    if best_start == "CRAFT_TW":
                        continue
                output = runner._run(a.factory, "all", approval_fingerprint=f"ROLL-{len(cycles)}-V3",
                                     crew_policy=crew, improve="V2", lns_seconds=a.seconds,
                                     start_policy=policy, start_candidates=candidates, **common)
                runs.append((policy, output))
            chosen, output = min(runs, key=lambda r: tuple((r[1]["craft_v2"] or {}).get("final_score")
                                                          or (float("inf"),)))
        finally:
            runner._source = original_source
        plan = output["selected_schedule"]
        if any(int(r["start_seconds"]) < now * 86400 for r in plan):
            raise SystemExit("ROLLING_PLAN_STARTS_IN_THE_PAST")
        # An order that starts inside the freeze window is frozen whole (its
        # route as planned): an order is either fixed or not started, so the
        # earliest-start floor only ever applies to unstarted orders.
        cut = (now + a.freeze_days) * 86400
        begun = {str(ops[r["operation_key"]]["order_id"]) for r in plan if int(r["start_seconds"]) < cut}
        newly = plan if last else [r for r in plan if str(ops[r["operation_key"]]["order_id"]) in begun]
        cycles.append({"cycle": len(cycles), "start_day": now, "orders": scope, "operations": len(pending),
                       "frozen_now": len(newly), "policy_used": chosen, "last": last,
                       "validation": output["assembled_schedule_validation"],
                       "deferred": output["deferred_operations"],
                       "peak_rss_mb": output["sampled_peak_rss_mb"],
                       "wall_seconds": round(time.perf_counter() - cycle_started, 1)})
        print(json.dumps({k: v for k, v in cycles[-1].items() if k not in ("validation", "deferred", "orders")}),
              flush=True)
        if last and len(plan) < len(pending):
            raise SystemExit("ROLLING_LAST_CYCLE_DEFERRED_OPERATIONS")
        frozen.update({r["operation_key"]: r for r in newly})
        if newly or last:
            now += a.freeze_days
        else:  # nothing starts this week: move to the plan's first start
            now = max(now + a.freeze_days, int(min(int(r["start_seconds"]) for r in plan) // 86400))
    return list(frozen.values()), cycles


def main():
    p = argparse.ArgumentParser()
    p.add_argument("factory", type=int)
    p.add_argument("out")
    p.add_argument("--orders", type=int)
    p.add_argument("--order-offset", type=int, default=0,
                   help="skip this many eligible orders (by release) before taking --orders: another order set")
    p.add_argument("--release-days", type=int)
    p.add_argument("--pace", default="NORMAL", choices=["NORMAL", "FAST", "SLOW"])
    p.add_argument("--seconds", type=float, default=1800, help="CRAFT improvement budget")
    p.add_argument("--rules", nargs="*", default=["FIFO", "EDD", "SPT"])
    p.add_argument("--reuse", help="directory with earlier baseline *_schedule.json for this selection")
    p.add_argument("--skip-craft", action="store_true")
    p.add_argument("--mode", default="v2", choices=["v2", "windows", "v3", "rolling"],
                   help="v2: dispatch starts + CP-SAT moves; windows: CP-SAT decides every operation "
                        "in rolling route windows (the original CRAFT architecture), with crews and the "
                        "full resource domain; v3 (final CRAFT): CP-SAT builds the plan in rolling TIME "
                        "windows of ready operations ranked by operation due date (commit the first shift, "
                        "re-optimise the rest), then CP-SAT moves improve it; dispatch plans are only a "
                        "validated safety floor")
    p.add_argument("--tw-hours", type=float, default=24)
    p.add_argument("--tw-commit-hours", type=float, default=8)
    p.add_argument("--tw-cap", type=int, default=22)
    p.add_argument("--start-policy", default="BOTH", choices=["BOTH", "FIRST", "BEST"],
                   help="v3: improve the CP-SAT window plan (FIRST), the best validated start (BEST), "
                        "or both and keep the better final plan (BOTH, the locked V3 default; "
                        "each improvement gets --seconds)")
    p.add_argument("--plan-days", type=float, default=14,
                   help="rolling: an order enters a cycle once released within this many days")
    p.add_argument("--freeze-days", type=int, default=7,
                   help="rolling: operations starting in this many days are frozen; the next cycle starts there")
    p.add_argument("--cycle-orders", type=int, default=10,
                   help="rolling: at most this many orders per cycle (an order that starts is frozen whole)")
    p.add_argument("--window-size", type=int, default=15)
    p.add_argument("--extra-starts", nargs="*", default=[],
                   help="more V2 start schedules by policy name, read from --reuse (e.g. CRAFT_WINDOWS: "
                        "CP-SAT builds the plan, the moves improve it, EDD stays only a safety floor)")
    p.add_argument("--horizon-extra-days", type=float, default=30,
                   help="planning room past the last due date (all policies)")
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
    chosen = chosen[a.order_offset:]
    chosen = set(chosen[:a.orders] if a.orders else chosen)
    keys = [row["operation_key"] for _rel, oid, rows, _o in eligible if oid in chosen for row in rows]
    report = {"factory": a.factory, "pace": a.pace, "settings": settings, "orders": len(chosen),
              "operations": len(keys), "improve_seconds": a.seconds,
              "order_offset": a.order_offset, "horizon_extra_days": a.horizon_extra_days,
              "dispatch_window_days": float(os.environ.get("CRAFT_DISPATCH_WINDOW_DAYS", "0")),
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
        if (prior.get("operations") != len(keys) or prior.get("factory") != a.factory
                or prior.get("horizon_extra_days", 30) != a.horizon_extra_days
                or prior.get("order_offset", 0) != a.order_offset
                or prior.get("dispatch_window_days", 0.0) != report["dispatch_window_days"]
                or (prior.get("settings") or {}).get("max_crew") != settings["max_crew"]):
            raise SystemExit("REUSE_SELECTION_MISMATCH")
        result = next((r for r in prior["results"] if r["policy"] == name), None)
        return (result, json.loads(path.read_text(encoding="utf-8"))) if result else None

    extra = {}
    for name in a.extra_starts:
        prior = reused(name)
        if prior is None:
            raise SystemExit(f"EXTRA_START_NOT_IN_REUSE:{name}")
        result, rows = prior
        record({**result, "reused_from": a.reuse}, rows, name)
        extra[name] = rows
    by_rule, ops = rule_keys(source, snapshot)
    by_rule["SAVABLE_FIRST"] = savable_first(source, ops, crew)
    schedules = {}
    jobs = [(rule, rule, None) for rule in a.rules] + [("EDD_CREW", "EDD", crew)]
    if not a.skip_craft and a.mode in ("v2", "v3"):
        jobs.append(("SAVABLE_FIRST_CREW", "SAVABLE_FIRST", crew))
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

    if not a.skip_craft and a.mode == "windows":
        t = time.perf_counter()
        output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="V2-CRAFT-W",
                             route_ready_objective=True, crew_policy=crew, full_domain_windows=True,
                             window_size=a.window_size)
        result, rows = summarize("CRAFT_WINDOWS", a.factory, output, None, time.perf_counter() - t)
        result.update(window_size=a.window_size, window_order="RELEASE")
        record(result, rows, "CRAFT_WINDOWS")
    if not a.skip_craft and a.mode == "v3":
        tw = {"hours": a.tw_hours, "commit_hours": a.tw_commit_hours, "cap": a.tw_cap}
        prior = reused("CRAFT_TW")
        if prior is not None:
            result, rows = prior
            record({**result, "reused_from": a.reuse}, rows, "CRAFT_TW")
        else:
            t = time.perf_counter()
            output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="V3-CRAFT-TW",
                                 route_ready_objective=True, crew_policy=crew, full_domain_windows=True,
                                 time_windows=tw)
            result, rows = summarize("CRAFT_TW", a.factory, output, None, time.perf_counter() - t)
            result.update(time_windows=tw, windows=len(output["windows"]))
            record(result, rows, "CRAFT_TW")
            (out / "craft_tw_windows.json").write_text(json.dumps(output["windows"], indent=1, default=str),
                                                       encoding="utf-8")
        t = time.perf_counter()
        starts = [("CRAFT_TW", rows)] + [(n, schedules[n]) for n in ("EDD_CREW", "SAVABLE_FIRST_CREW", "EDD")]
        runs = []
        for policy in (("FIRST", "BEST") if a.start_policy == "BOTH" else (a.start_policy,)):
            if policy == "BEST" and runs:
                best_start = min((s for s in runs[0][1]["craft_v2"]["starts"] if s.get("score") is not None),
                                 key=lambda s: tuple(s["score"]))["start"]
                if best_start == "CRAFT_TW":
                    continue  # the best start is the CP-SAT plan: BEST would repeat FIRST
            output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="V3-CRAFT",
                                 route_ready_objective=True, crew_policy=crew, improve="V2",
                                 lns_seconds=a.seconds, start_policy=policy, start_candidates=starts)
            runs.append((policy, output))
        chosen, output = min(runs, key=lambda r: tuple((r[1]["craft_v2"] or {}).get("final_score") or (float("inf"),)))
        result, rows = summarize("CRAFT_V3", a.factory, output, None, time.perf_counter() - t)
        v2 = output["craft_v2"] or {}
        result["start_policy"] = a.start_policy
        result["start_policy_used"] = chosen
        result["start_policy_scores"] = {p: (o["craft_v2"] or {}).get("final_score") for p, o in runs}
        result["craft_v3"] = {k: v2.get(k) for k in ("pace", "starts", "seconds", "accepted_moves",
                                                    "improving_moves", "decisions", "floor_used", "final_score")}
        (out / "craft_v3_moves.json").write_text(json.dumps(v2.get("moves", []), indent=1), encoding="utf-8")
        record(result, rows, "CRAFT_V3")
        print(json.dumps(result["craft_v3"], default=str), flush=True)
    if not a.skip_craft and a.mode == "v2":
        t = time.perf_counter()
        output = runner._run(a.factory, "all", selected_keys=keys, approval_fingerprint="V2-CRAFT",
                             route_ready_objective=True, crew_policy=crew, improve="V2",
                             lns_seconds=a.seconds,
                             start_candidates=[(n, schedules[n]) for n in ("EDD", "EDD_CREW", "SAVABLE_FIRST_CREW")]
                             + [(n, extra[n]) for n in a.extra_starts])
        result, rows = summarize("CRAFT_V2", a.factory, output, None, time.perf_counter() - t)
        v2 = output["craft_v2"] or {}
        result["craft_v2"] = {k: v2.get(k) for k in ("pace", "starts", "seconds", "accepted_moves",
                                                    "improving_moves", "first_sweep_complete",
                                                    "weights", "final_score")}
        (out / "craft_v2_moves.json").write_text(json.dumps(v2.get("moves", []), indent=1), encoding="utf-8")
        record(result, rows, "CRAFT_V2")
        print(json.dumps(result["craft_v2"], default=str), flush=True)

    if not a.skip_craft and a.mode == "rolling":
        t = time.perf_counter()
        rows, cycles = rolling_v3(a, source, keys, ops, by_rule, crew)
        output = {"selected_schedule": rows, "assembled_schedule_validation": cycles[-1]["validation"],
                  "deferred_operations": cycles[-1]["deferred"], "sampled_peak_rss_mb": max(c["peak_rss_mb"] for c in cycles)}
        result, rows = summarize("CRAFT_V3_ROLLING", a.factory, output, None, time.perf_counter() - t)
        result["rolling"] = {"plan_days": a.plan_days, "freeze_days": a.freeze_days,
                             "cycle_orders": a.cycle_orders, "cycles": len(cycles)}
        (out / "craft_v3_rolling_cycles.json").write_text(json.dumps(cycles, indent=1, default=str), encoding="utf-8")
        record(result, rows, "CRAFT_V3_ROLLING")

    for r in report["results"]:
        k, u = r["kpis"] or {}, r["utilization"] or {}
        print(r["policy"], "complete", k.get("orders_complete"), "partial", k.get("orders_partially_scheduled"), "makespan_d", k.get("makespan_days"),
              "tardiness_d", k.get("total_tardiness_days"), "on_time", k.get("on_time_orders"),
              "flow_d", k.get("mean_flow_days"), "workers_used", u.get("workers", {}).get("used"),
              "worker_util_used%", u.get("workers", {}).get("util_used_pct"), flush=True)


if __name__ == "__main__":
    main()
