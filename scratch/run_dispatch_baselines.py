"""FIFO / EDD / SPT dispatch baselines under CRAFT's own physics and validators.

Usage:
  venv/Scripts/python.exe scratch/run_dispatch_baselines.py <factory_id> <craft_cycles.json> <out_dir> [FIFO EDD SPT]

Each baseline is a serial schedule-generation scheme: operations are placed
one at a time (one-operation CP windows) in the rule's priority order over the
precedence-ready list, at the earliest feasible slot the canonical model
admits. Resource domain, calendars, execution contracts, exact replay,
physical, scope and assembled validation are the same code CRAFT uses; only
the multi-operation optimization is replaced by the rule. To compare like for
like, each baseline schedules exactly the operation set of each approved CRAFT
cycle, on top of its own earlier cycles. An operation the rule cannot place
(and its route successors) is reported as unscheduled, never hidden.
"""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scratch"))
from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner  # noqa: E402
from capture_schedule_kpis import kpis  # noqa: E402


def rule_keys(factory_id):
    source, _readiness, snapshot, _hash = runner._source(factory_id)
    orders = {str(o["production_order_id"]): o for o in source["orders"]}
    sam = {f'{r["production_order_id"]}_{r["operation_id"]}': float(r.get("standard_allowed_minutes") or 0)
           for r in source["operations"]}
    ops = {op["operation_key"]: op for op in snapshot["operations"]}

    def common(key):
        op = ops[key]
        return (str(op["order_id"]), int(op["routing_sequence"]), key)

    def fifo(key):
        order = orders[str(ops[key]["order_id"])]
        return (str(order.get("scheduled_start_date") or ""),) + common(key)

    def edd(key):
        order = orders[str(ops[key]["order_id"])]
        return (str(order.get("delivery_deadline") or "9999"),) + common(key)

    def spt(key):
        return (sam.get(key, 0.0) * int(ops[key]["quantity"]["not_started"]),) + common(key)

    return {"FIFO": fifo, "EDD": edd, "SPT": spt}, ops


def dispatch_order(keys, ops, key_fn):
    """Static list-scheduling order: repeatedly take the best precedence-ready operation."""
    routes = {}
    for key in sorted(keys, key=lambda k: (str(ops[k]["order_id"]), int(ops[k]["routing_sequence"]))):
        routes.setdefault(str(ops[key]["order_id"]), []).append(key)
    pointer = {order: 0 for order in routes}
    sequence = []
    while len(sequence) < len(keys):
        ready = [route[pointer[o]] for o, route in routes.items() if pointer[o] < len(route)]
        best = min(ready, key=key_fn)
        sequence.append(best)
        pointer[str(ops[best]["order_id"])] += 1
    return sequence


def craft_cycle_sets(cycles_path, factory_id):
    data = json.loads(Path(cycles_path).read_text(encoding="utf-8"))
    factory = next(f for f in data["factories"] if f["factory_id"] == factory_id)
    sets = []
    for cycle in factory["cycles"]:
        if cycle.get("approval") != "TEST_APPROVED":
            continue
        validated = [a for a in cycle["attempts"] if a["status"] == "VALIDATED"]
        sets.append(validated[-1]["accepted_keys"])
    return sets


def main():
    factory_id, cycles_path, out = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    rules = sys.argv[4:] or ["FIFO", "EDD", "SPT"]
    out.mkdir(parents=True, exist_ok=False)
    keys_by_rule, ops = rule_keys(factory_id)
    cycle_sets = craft_cycle_sets(cycles_path, factory_id)
    report = {"factory_id": factory_id, "craft_cycles": len(cycle_sets),
              "method": "SERIAL_SGS_ONE_OPERATION_CP_WINDOWS_SAME_VALIDATORS", "rules": {}}
    for rule in rules:
        approved, unscheduled, cycles, started = [], {}, [], time.perf_counter()
        for number, keys in enumerate(cycle_sets, 1):
            done = {r["operation_key"] for r in approved}
            # Drop operations whose route predecessor the rule failed to place.
            selectable, blocked = [], set(unscheduled)
            for key in sorted(keys, key=lambda k: (str(ops[k]["order_id"]), int(ops[k]["routing_sequence"]))):
                order, seq = str(ops[key]["order_id"]), int(ops[key]["routing_sequence"])
                if any(str(ops[b]["order_id"]) == order and int(ops[b]["routing_sequence"]) < seq for b in blocked):
                    unscheduled[key] = "ROUTE_PREDECESSOR_UNSCHEDULED_BY_RULE"
                    blocked.add(key)
                else:
                    selectable.append(key)
            if not selectable:
                cycles.append({"cycle": number, "selected": 0})
                continue
            sequence = dispatch_order(selectable, ops, keys_by_rule[rule])
            begin = time.perf_counter()
            output = runner._run(factory_id, "all", selected_keys=selectable, approved_rows=approved,
                                 approval_fingerprint=f"BASELINE-{rule}-CYCLE-{number}",
                                 route_ready_objective=True,
                                 window_sequence=[(key,) for key in sequence])
            accepted = output["selected_schedule"]
            validation = output["assembled_schedule_validation"]
            if accepted and not validation.get("accepted"):
                raise RuntimeError(f"BASELINE_ASSEMBLED_VALIDATION_FAILED:{rule}:{number}")
            approved.extend(accepted)
            for key, reason in output["deferred_operations"].items():
                unscheduled[key] = reason.get("reason", "DEFERRED")
            cycles.append({"cycle": number, "selected": len(selectable), "accepted": len(accepted),
                           "deferred": len(output["deferred_operations"]),
                           "seconds": round(time.perf_counter() - begin, 2),
                           "assembled_validation": validation.get("accepted"),
                           "peak_rss_mb": output["sampled_peak_rss_mb"]})
            print(json.dumps({"rule": rule, **cycles[-1]}), flush=True)
        (out / f"{rule.lower()}_schedule.json").write_text(json.dumps(approved, indent=1), encoding="utf-8")
        report["rules"][rule] = {"scheduled": len(approved), "unscheduled": unscheduled, "cycles": cycles,
                                 "wall_seconds": round(time.perf_counter() - started, 2),
                                 "kpis": kpis(factory_id, approved) if approved else None}
        (out / "baselines.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({rule: {k: v.get("kpis") for k, v in [("x", r)]}["x"] for rule, r in report["rules"].items()},
                     indent=1, default=str))


if __name__ == "__main__":
    main()
