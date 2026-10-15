"""Deterministic, in-memory demo fixtures. Deliberately has no DB or scheduler imports."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

NOW = datetime(2026, 10, 7, 10, 20, tzinfo=timezone.utc)
FACTORIES = [{"id": i, "name": f"Factory {i}", "region": region, "health": "Attention" if i == 4 else "Operational", "active_orders": 18 + i * 3, "running_operations": 42 + i * 7, "available_workers": 238 + i * 21, "worker_capacity": 280 + i * 21, "available_machines": 74 + i * 8, "machine_capacity": 82 + i * 8, "utilization": round(67 + i * 2.8, 1), "risk": "At risk" if i in (2, 4) else "Stable"} for i, region in enumerate(["North · Lahore", "Central · Faisalabad", "South · Karachi", "West · Multan", "North · Sialkot", "Central · Gujranwala"], 1)]
LINES = []
for factory in FACTORIES:
    for n in range(1, 5):
        line_id = f"F{factory['id']}-L{n:02d}"
        LINES.append({"id": line_id, "factory_id": factory["id"], "name": f"Line {n:02d}", "active_order": f"PO-{factory['id']}{n:03d}" if n < 4 else "", "operation": ["Sleeve attach", "Side seam", "Top stitch", "Final inspection"][n - 1], "target": 420 + 25 * n, "actual": 388 + ((factory["id"] * 19 + n * 13) % 95), "workers": 24 + n, "machines_available": 12 - (n % 3), "status": "At risk" if factory["id"] == 4 and n == 2 else ("Running" if n < 4 else "Changeover")})

ORDERS = []
styles = [("Rib-knit polo", "SK-POLO-04"), ("Core crew tee", "SK-TEE-12"), ("Utility jogger", "SK-JOGR-08"), ("Oxford shirt", "SK-SHRT-21"), ("Interlock base layer", "SK-BASE-03"), ("Canvas overshirt", "SK-OVSH-06")]
for idx in range(1, 37):
    factory_id = ((idx - 1) % 6) + 1
    line_id = f"F{factory_id}-L{((idx - 1) // 6) % 4 + 1:02d}"
    style, sku = styles[(idx - 1) % len(styles)]
    progress = [28, 46, 73, 91, 12, 58][(idx * 3) % 6]
    risk = "Delayed" if idx in (8, 20, 32) else ("At risk" if idx in (5, 14, 26, 35) else "On track")
    release = NOW - timedelta(days=9 + (idx % 17))
    due = NOW + timedelta(days=(idx % 11) - 4)
    ORDERS.append({"id": f"PO-{factory_id}{idx:03d}", "factory_id": factory_id, "factory_name": f"Factory {factory_id}", "style": style, "sku": sku, "quantity": 4200 + (idx % 8) * 350, "smv": round(8.2 + (idx % 9) * 0.63, 1), "release": release, "due": due, "progress": progress, "current_operation": ["Cutting", "Sleeve attach", "Side seam", "Hem", "Final inspection"][idx % 5], "priority": "Urgent" if idx in (6, 23) else ("High" if idx % 5 == 0 else "Normal"), "risk": risk, "status": "Delayed" if risk == "Delayed" else ("Running" if idx % 4 else "Planned"), "line_id": line_id})

for line in LINES:
    matching_order = next((order for order in ORDERS if order["line_id"] == line["id"]), None)
    if matching_order:
        line["active_order"] = matching_order["id"]
        line["operation"] = matching_order["current_operation"]

MACHINES = []
machine_types = ["Lockstitch", "Overlock", "Flatlock", "Buttonhole", "Press", "Cutting table"]
for line in LINES:
    for n in range(1, 7):
        status = "Down" if line["id"] == "F4-L02" and n == 3 else ("Reserved" if n == 5 else ("Running" if n in (1, 2, 6) else "Available"))
        MACHINES.append({"id": f"M-{line['factory_id']}-{line['id'][-2:]}-{n:02d}", "type": machine_types[(n + line["factory_id"]) % len(machine_types)], "factory_id": line["factory_id"], "line_id": line["id"], "status": status, "current_operation": line["operation"] if status == "Running" else "", "availability": 0 if status == "Down" else (75 if status == "Reserved" else 100)})

WORKERS = []
skill_set = [["Lockstitch", "Top stitch"], ["Overlock", "Seam finish"], ["Flatlock", "Cover stitch"], ["Inspection", "Packing"], ["Buttonhole", "Attach"]]
for line in LINES:
    for n in range(1, 9):
        WORKERS.append({"id": f"W-{line['factory_id']}-{line['id'][-2:]}-{n:03d}", "display_name": f"Operator {line['factory_id']}{line['id'][-2:]}-{n:02d}", "factory_id": line["factory_id"], "line_id": line["id"], "shift": ["Shift A · 06–14", "Shift B · 14–22", "Shift C · 22–06"][n % 3], "skills": skill_set[(n + line["factory_id"]) % len(skill_set)], "availability": "Absent" if line["id"] == "F4-L02" and n == 7 else ("Assigned" if n < 6 else "Available"), "current_operation": line["operation"] if n < 6 else ""})

OPERATIONS = []
for order_idx, order in enumerate(ORDERS):
    for step, operation in enumerate(["Cutting", "Sewing assembly", "Finishing & inspection"]):
        start = NOW - timedelta(hours=5 - ((order_idx + step * 2) % 8), minutes=(order_idx * 11) % 60)
        end = start + timedelta(hours=1 + ((order_idx + step) % 4), minutes=20)
        status = "Completed" if step == 0 and order["progress"] > 45 else ("Running" if step == 1 and order["status"] == "Running" else ("At risk" if order["risk"] != "On track" and step == 1 else "Planned"))
        machine = next((m for m in MACHINES if m["line_id"] == order["line_id"] and m["status"] != "Down"), MACHINES[0])
        crew = [w["id"] for w in WORKERS if w["line_id"] == order["line_id"] and w["availability"] != "Absent"][:(3 + step)]
        OPERATIONS.append({"id": f"{order['id']}-{step + 1}", "order_id": order["id"], "factory_id": order["factory_id"], "line_id": order["line_id"], "operation": operation, "machine_id": machine["id"], "workers": crew, "worker_count": len(crew), "start": start, "end": end, "status": status, "risk": order["risk"], "predecessor": "" if step == 0 else f"{order['id']}-{step}", "successor": "" if step == 2 else f"{order['id']}-{step + 2}", "release": order["release"], "due": order["due"], "smv": round(order["smv"] / 3, 1)})

DISRUPTIONS = [
    {"id": "EVT-2408", "type": "Machine breakdown", "severity": "Critical", "occurred_at": NOW - timedelta(minutes=18), "entity": "M-4-02-03 · Overlock", "factory_id": 4, "line_id": "F4-L02", "order_id": "PO-4010", "impact": "2 operations exposed · 46 min recovery", "status": "Open", "craft_action": "Escalation required", "description": "Needle-bar failure detected during active seam operation."},
    {"id": "EVT-2407", "type": "Worker absence", "severity": "Warning", "occurred_at": NOW - timedelta(minutes=54), "entity": "F4-L02 · Shift B", "factory_id": 4, "line_id": "F4-L02", "order_id": "", "impact": "Crew capacity reduced by 1", "status": "Acknowledged", "craft_action": "Repair accepted", "description": "Unplanned absence logged at shift handover."},
    {"id": "EVT-2406", "type": "Priority override", "severity": "Info", "occurred_at": NOW - timedelta(hours=2), "entity": "PO-2002 · Core crew tee", "factory_id": 2, "line_id": "F2-L01", "order_id": "PO-2002", "impact": "Order moved to urgent queue", "status": "Resolved", "craft_action": "Repair accepted", "description": "Manager marked the customer order urgent."},
    {"id": "EVT-2405", "type": "Line performance", "severity": "Warning", "occurred_at": NOW - timedelta(hours=3, minutes=15), "entity": "F1-L03 · Hourly output", "factory_id": 1, "line_id": "F1-L03", "order_id": "PO-1013", "impact": "Output 11% below target", "status": "Monitoring", "craft_action": "Not triggered", "description": "Line output below target for two consecutive intervals."},
]

CRAFT_RUNS = [
    {"id": "CR-20261007-0042", "factory_id": 4, "trigger": "Machine breakdown · EVT-2408", "reference_time": NOW - timedelta(minutes=18), "affected_region": "Factory 4 / Line 02 · 5 operations", "operations_in_window": 5, "controller_status": "COMPUTATIONAL_LIMIT", "cp_status": "UNKNOWN", "runtime_seconds": 42.8, "configurations": 118, "regimes": 947, "recovery_rounds": 4, "changed_operations": [], "accepted": False, "materialization": "NOT REACHED", "replay": "NOT REACHED", "physical_validation": "NOT REACHED", "scope_validation": "NOT REACHED", "domain_exhausted": False, "physical_infeasibility_proven": False, "message": "CRAFT reached the configured computational safety limit before producing a fully validated repair. The existing validated schedule remains active and this case requires escalation."},
    {"id": "CR-20261007-0041", "factory_id": 2, "trigger": "Priority override · PO-2002", "reference_time": NOW - timedelta(hours=2), "affected_region": "Factory 2 / Lines 01–03 · 8 operations", "operations_in_window": 8, "controller_status": "ACCEPTED", "cp_status": "OPTIMAL", "runtime_seconds": 18.4, "configurations": 74, "regimes": 206, "recovery_rounds": 1, "changed_operations": [{"operation": "PO-2002-2 · Sleeve attach", "old_resource": "F2-L01 / M-2-01-02", "new_resource": "F2-L03 / M-2-03-01", "old_interval": "10:40–12:15", "new_interval": "11:05–12:40", "reason": "Urgent order priority"}, {"operation": "PO-2002-3 · Finishing", "old_resource": "F2-L01 / M-2-01-04", "new_resource": "F2-L03 / M-2-03-03", "old_interval": "12:30–13:20", "new_interval": "12:46–13:36", "reason": "Precedence-preserving repair"}], "accepted": True, "materialization": "PASS", "replay": "PASS", "physical_validation": "VALID", "scope_validation": "SCOPE_VALID", "domain_exhausted": True, "physical_infeasibility_proven": False, "message": "Candidate replayed and validated within the declared repair scope."},
    {"id": "CR-20261007-0040", "factory_id": 1, "trigger": "Worker calendar change", "reference_time": NOW - timedelta(hours=4), "affected_region": "Factory 1 / Line 01 · 3 operations", "operations_in_window": 3, "controller_status": "ACCEPTED", "cp_status": "FEASIBLE", "runtime_seconds": 11.6, "configurations": 31, "regimes": 88, "recovery_rounds": 0, "changed_operations": [{"operation": "PO-1001-2 · Sewing assembly", "old_resource": "F1-L01 / M-1-01-02", "new_resource": "F1-L01 / M-1-01-04", "old_interval": "09:10–10:30", "new_interval": "09:24–10:44", "reason": "Calendar conflict avoided"}], "accepted": True, "materialization": "PASS", "replay": "PASS", "physical_validation": "VALID", "scope_validation": "SCOPE_VALID", "domain_exhausted": False, "physical_infeasibility_proven": False, "message": "Candidate replayed and validated within the declared repair scope."},
]

AUDIT = [{"id": "AUD-9001", "occurred_at": NOW - timedelta(minutes=18), "actor": "System monitor", "factory_id": 4, "action": "Machine breakdown detected", "entity": "M-4-02-03 / PO-4010", "controller_status": "Escalated", "validation": "Not reached", "accepted": False, "runtime_seconds": 42.8, "detail": "Repair safely escalated; existing validated schedule retained."}, {"id": "AUD-9000", "occurred_at": NOW - timedelta(hours=2), "actor": "S. Rahman · Production manager", "factory_id": 2, "action": "Priority override", "entity": "PO-2002 / F2-L01", "controller_status": "Accepted", "validation": "Replay + physical + scope valid", "accepted": True, "runtime_seconds": 18.4, "detail": "Urgent priority set in demo data; repair accepted after all validation gates."}, {"id": "AUD-8999", "occurred_at": NOW - timedelta(hours=4), "actor": "Shift supervisor", "factory_id": 1, "action": "Worker calendar change", "entity": "F1-L01", "controller_status": "Accepted", "validation": "Replay + physical + scope valid", "accepted": True, "runtime_seconds": 11.6, "detail": "One worker calendar changed; a local repair was accepted."}]

def snapshot(value):
    return deepcopy(value)

def dashboard_summary(factory_id=None):
    selected = next((f for f in FACTORIES if str(f["id"]) == str(factory_id)), None)
    factories = [selected] if selected else FACTORIES
    active_orders = sum(f["active_orders"] for f in factories)
    return {"active_orders": active_orders, "orders_at_risk": len([o for o in ORDERS if (not selected or o["factory_id"] == selected["id"]) and o["risk"] != "On track"]), "running_operations": sum(f["running_operations"] for f in factories), "delayed_operations": len([x for x in OPERATIONS if (not selected or x["factory_id"] == selected["id"]) and x["status"] == "At risk"]), "available_workforce": sum(f["available_workers"] for f in factories), "workforce_capacity": sum(f["worker_capacity"] for f in factories), "available_machines": sum(f["available_machines"] for f in factories), "machine_capacity": sum(f["machine_capacity"] for f in factories), "production_health": [{"name": name, "value": count} for name, count in [("On track", 62), ("At risk", 25), ("Delayed", 13)]], "hourly_output": [{"hour": f"{h:02d}:00", "target": 380 + (h % 4) * 12, "actual": 348 + ((h * 37) % 108)} for h in range(6, 15)], "attention": snapshot(DISRUPTIONS), "recent_runs": snapshot(CRAFT_RUNS[:3])}

def filtered_rows(rows, params, keys=("factory_id", "line_id", "status", "risk", "priority")):
    output = list(rows)
    for key in keys:
        value = params.get(key)
        if value and value != "all":
            output = [row for row in output if str(row.get(key, "")).lower() == value.lower()]
    search = params.get("search", "").strip().lower()
    if search:
        output = [row for row in output if search in " ".join(str(v) for v in row.values()).lower()]
    return output

def add_demo_disruption(data):
    item = {"id": f"EVT-DEMO-{len(DISRUPTIONS) + 1:03d}", "type": data["type"], "severity": data["severity"], "occurred_at": NOW, "entity": data["entity"], "factory_id": data["factory_id"], "line_id": data.get("line_id", ""), "order_id": data.get("order_id", ""), "impact": "Demo event · no live schedule changes", "status": "Open", "craft_action": "Simulation only", "description": data["description"]}
    DISRUPTIONS.insert(0, item)
    return item

def set_demo_priority(order_id, priority):
    item = next((order for order in ORDERS if order["id"] == order_id), None)
    if item is None:
        return None
    item["priority"] = priority
    return item

def add_demo_run(factory_id, trigger):
    run = {"id": f"CR-DEMO-{len(CRAFT_RUNS) + 1:03d}", "factory_id": factory_id, "trigger": trigger, "reference_time": NOW, "affected_region": f"Factory {factory_id} · demo scope", "operations_in_window": 4, "controller_status": "COMPUTATIONAL_LIMIT", "cp_status": "UNKNOWN", "runtime_seconds": 8.2, "configurations": 24, "regimes": 71, "recovery_rounds": 2, "changed_operations": [], "accepted": False, "materialization": "NOT REACHED", "replay": "NOT REACHED", "physical_validation": "NOT REACHED", "scope_validation": "NOT REACHED", "domain_exhausted": False, "physical_infeasibility_proven": False, "message": "Demo simulation only. No live schedule was changed; the existing validated schedule remains active."}
    CRAFT_RUNS.insert(0, run)
    return run
