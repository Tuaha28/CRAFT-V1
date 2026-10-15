"""Reproducible normal-state disruption catalog for the 50-order benchmark."""
from __future__ import annotations

from collections import Counter, defaultdict

from crafter.lns_probe.analysis import seconds
from crafter.lns_probe.line_k_model import _available_machine_pool

from .events import DisruptionEvent, EventType


def _future_rows(rows, event_time):
    return [row for row in rows if seconds(row["end_minutes"]) > event_time]


def build_catalog(problem, index, rows, *, seed: int = 42) -> list[DisruptionEvent]:
    """Build 23 deterministic scenarios without mutating scheduling state."""
    machine_rows = defaultdict(list)
    worker_rows = defaultdict(list)
    for row in rows:
        for mid in row.get("machine_ids", ()):
            machine_rows[int(mid)].append(row)
        for wid in row.get("employee_ids", ()):
            worker_rows[str(wid)].append(row)
    # Prefer machines whose first affected operation does not require the
    # entire line pool.  This gives the normal-state disruption catalogue a
    # mix of genuinely repairable cases while retaining the explicit
    # high-contention/low-contention distinction.
    # Require at least one spare compatible physical machine for the first
    # future operation.  Without this screen a so-called repair scenario can
    # be a deterministic zero-mode case (the broken machine is the entire
    # k-sized pool), which is useful as a boundary probe but a poor smoke
    # representative.
    repairable_machines = []
    for m, uses in machine_rows.items():
        first = min(uses, key=lambda r: (seconds(r["start_minutes"]), str(r["operation_key"])))
        operation = problem.operation_by_key[str(first["operation_key"])]
        pool = _available_machine_pool(problem, operation, int(first["line_id"]))
        if (min(len(r.get("machine_ids", ())) for r in uses) <= 5
                and (len(pool) > len(first.get("machine_ids", ()))
                     or len(getattr(operation, "eligible_line_ids", ())) > 1)
                and int(m) in pool):
            repairable_machines.append(m)
    machine_rank = sorted(repairable_machines, key=lambda m: (-len(machine_rows[m]), m))
    low_machine = sorted(repairable_machines, key=lambda m: (len(machine_rows[m]), m))
    worker_rank = sorted(worker_rows, key=lambda w: (-len(worker_rows[w]), w))
    first_by_machine = {}
    for mid in machine_rank + low_machine:
        future = sorted(machine_rows[mid], key=lambda r: (seconds(r["start_minutes"]), str(r["operation_key"])))
        if future:
            first_by_machine.setdefault(mid, future[0])
    first_by_worker = {}
    for wid in worker_rank:
        future = sorted(worker_rows[wid], key=lambda r: (seconds(r["start_minutes"]), str(r["operation_key"])))
        if future:
            # Anchor absenteeism at a representative future use rather than
            # always at the earliest operation (which is often time zero and
            # would make the absence cover the entire horizon).
            first_by_worker[wid] = future[min(len(future) - 1, max(0, len(future) // 3))]
    events = []

    def machine_event(event_id, mid, duration, etype=EventType.MACHINE_BREAKDOWN.value, severity="medium", extra=()):
        row = first_by_machine[mid]
        t = max(0, seconds(row["start_minutes"]) - 60)
        factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
        events.append(DisruptionEvent(event_id, etype, t, factory, (int(mid),), (), (), (), duration,
                                      severity, metadata={"seed": seed, "target_operation_key": str(row["operation_key"]), **dict(extra)}))

    # Six single-machine conditions: high/low contention × three durations.
    for label, mid in (("high", machine_rank[0]), ("low", low_machine[0])):
        for duration, name in ((1800, "short"), (7200, "medium"), (28800, "shift")):
            machine_event(f"machine_{label}_{name}_{mid}", mid, duration,
                          severity="high" if duration >= 7200 else "medium")

    # Three single-worker absences and two multi-worker absences.
    for i, wid in enumerate(worker_rank[:3]):
        row = first_by_worker[wid]
        t = max(0, seconds(row["start_minutes"]) - 60)
        factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
        events.append(DisruptionEvent(f"worker_absence_{i+1}_{wid}", EventType.WORKER_ABSENCE.value, t, factory,
                                      worker_ids=(str(wid),), duration_seconds=14400,
                                      severity="medium", metadata={"seed": seed, "target_operation_key": str(row["operation_key"])}))
    for i, ids in enumerate((tuple(worker_rank[:2]), tuple(worker_rank[2:4]))):
        row = first_by_worker[ids[0]]
        t = max(0, seconds(row["start_minutes"]) - 60)
        factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
        events.append(DisruptionEvent(f"multi_worker_absence_{i+1}", EventType.MULTI_WORKER_ABSENCE.value, t, factory,
                                      worker_ids=tuple(ids), duration_seconds=14400,
                                      severity="high", metadata={"seed": seed, "target_operation_key": str(row["operation_key"])}))

    # Urgent orders: late, pressured, and healthy deterministic representatives.
    order_scores = sorted(index.order_tardiness, key=lambda o: (-index.order_tardiness[o], str(o)))
    healthy = sorted(index.order_tardiness, key=lambda o: (index.order_tardiness[o], str(o)))
    urgent_orders = [order_scores[0], order_scores[len(order_scores)//2], healthy[0]]
    for i, order_id in enumerate(urgent_orders):
        keys = index.order_operations[str(order_id)]
        row = min((index.by_key[k] for k in keys), key=lambda r: seconds(r["start_minutes"]))
        t = max(0, seconds(row["start_minutes"]) - 60)
        factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
        events.append(DisruptionEvent(f"urgent_order_{i+1}_{order_id}", EventType.URGENT_ORDER.value, t, factory,
                                      order_ids=(str(order_id),), duration_seconds=0, severity="high",
                                      metadata={"seed": seed, "priority": "URGENT", "target_operation_key": str(row["operation_key"])}))

    # Three predecessor delays on chains with downstream work.
    candidates = [k for k, succ in index.route_successor.items() if succ]
    for i, key in enumerate(sorted(candidates)[:3]):
        row = index.by_key[key]
        base_end = seconds(row["end_minutes"])
        delay = (900, 3600, 10800)[i]
        events.append(DisruptionEvent(f"predecessor_delay_{i+1}_{key}", EventType.PREDECESSOR_DELAY.value,
                                      max(0, base_end - 60), int(index.operation_features[key]["factory_id"]),
                                      operation_keys=(key,), duration_seconds=delay, severity="high",
                                      metadata={"seed": seed, "predecessor_operation_key": key,
                                                "delayed_end_seconds": base_end + delay,
                                                "delay_seconds": delay}))

    # Two resource-congestion probes use the same exact machine semantics but
    # are labelled separately for impact/repair reporting.
    for i, mid in enumerate(machine_rank[:2]):
        machine_event(f"congestion_{i+1}_{mid}", mid, 7200,
                      etype=EventType.RESOURCE_CONGESTION.value, severity="high")

    # Two simultaneous machine failures.
    for i, mids in enumerate((tuple(machine_rank[:2]), tuple(machine_rank[1:3]))):
        row = first_by_machine[mids[0]]
        t = max(0, seconds(row["start_minutes"]) - 60)
        factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
        events.append(DisruptionEvent(f"multi_machine_breakdown_{i+1}", EventType.MULTIPLE_MACHINE_BREAKDOWN.value,
                                      t, factory, tuple(int(x) for x in mids), duration_seconds=7200,
                                      severity="critical", metadata={"seed": seed, "target_operation_key": str(row["operation_key"])}))

    # Two compound cases.
    m = machine_rank[0]; mr = first_by_machine[m]; t = max(0, seconds(mr["start_minutes"]) - 60)
    worker = str(mr["employee_ids"][0])
    factory = int(index.operation_features[str(mr["operation_key"])] ["factory_id"])
    events.append(DisruptionEvent("combined_machine_worker", EventType.COMBINED.value, t, factory,
                                  machine_ids=(int(m),), worker_ids=(worker,), duration_seconds=7200,
                                  severity="critical", metadata={"seed": seed, "target_operation_key": str(mr["operation_key"])}))
    order_id = str(urgent_orders[0]); keys = index.order_operations[order_id]; row = min((index.by_key[k] for k in keys), key=lambda r: seconds(r["start_minutes"]))
    t = max(0, seconds(row["start_minutes"]) - 60); factory = int(index.operation_features[str(row["operation_key"])] ["factory_id"])
    events.append(DisruptionEvent("combined_urgent_machine", EventType.COMBINED.value, t, factory,
                                  machine_ids=(int(row["machine_ids"][0]),), order_ids=(order_id,), duration_seconds=7200,
                                  severity="critical", metadata={"seed": seed, "priority": "URGENT", "target_operation_key": str(row["operation_key"])}))
    return events
