import heapq
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from utils.derived_constants import load_derived_constants

OUT = Path("outputs/integration_audit/seeder_timeline_dependencies.json")
SOURCE = Path("data_generation/seed_database_fixed.py")
engine = get_engine()
cal = load_derived_constants()
reference = datetime.fromisoformat(cal["reference_time"])
base = reference - timedelta(days=15)

with engine.connect() as conn:
    employees = pd.read_sql(text("""
        SELECT e.employee_id, e.factory_id, e.role, ws.is_on_leave,
               osm.operation_type
        FROM Employees e
        JOIN Work_Schedules ws ON ws.employee_id=e.employee_id
        JOIN Operator_Skill_Matrix osm ON osm.employee_id=e.employee_id
        WHERE e.is_active=1 AND e.role='Operator'
    """), conn)
    rows = pd.read_sql(text("""
        SELECT j.schedule_item_id, j.employee_id, j.planned_end_time,
               j.actual_end_time, mc.operation_type, ofo.factory_id
        FROM Job_Shop_Schedules j
        JOIN Machine_Capabilities mc ON mc.asset_id=j.asset_id
        JOIN Order_Factory_Ownership ofo
          ON ofo.production_order_id=CAST(j.production_order_id AS CHAR)
        ORDER BY j.schedule_item_id
    """), conn)

pools = defaultdict(list)
employee_keys = defaultdict(list)
for r in employees.itertuples(index=False):
    if bool(r.is_on_leave):
        continue
    eid = int(r.employee_id)
    key = (str(r.operation_type), int(r.factory_id))
    pools[key].append(eid)
    employee_keys[eid].append(key)

actual_clock = {int(e): base for e in employees.employee_id.unique()}
planned_clock = dict(actual_clock)
actual_heaps = {}
planned_heaps = {}
for key, ids in pools.items():
    ids = sorted(set(ids))
    actual_heaps[key] = [(base, e) for e in ids]
    planned_heaps[key] = [(base, e) for e in ids]
    heapq.heapify(actual_heaps[key])
    heapq.heapify(planned_heaps[key])

def earliest(heap, clocks):
    while heap and heap[0][0] != clocks[heap[0][1]]:
        heapq.heappop(heap)
    if not heap:
        raise RuntimeError("empty replay pool")
    return heap[0][1]

not_actual = 0
not_planned = 0
examples = []
for r in rows.itertuples(index=False):
    key = (str(r.operation_type), int(r.factory_id))
    chosen = int(r.employee_id)
    ae = earliest(actual_heaps[key], actual_clock)
    pe = earliest(planned_heaps[key], planned_clock)
    if chosen != ae:
        not_actual += 1
    if chosen != pe:
        not_planned += 1
        if len(examples) < 10:
            examples.append({
                "schedule_item_id": int(r.schedule_item_id),
                "chosen_employee": chosen,
                "actual_clock_earliest_employee": ae,
                "planned_clock_earliest_employee": pe,
                "pool": [key[0], key[1]],
                "chosen_actual_free": actual_clock[chosen].isoformat(),
                "planned_earliest_free": planned_clock[pe].isoformat(),
            })
    planned_clock[chosen] = pd.Timestamp(r.planned_end_time).to_pydatetime()
    actual_clock[chosen] = pd.Timestamp(r.actual_end_time).to_pydatetime()
    for member_key in employee_keys[chosen]:
        heapq.heappush(actual_heaps[member_key], (actual_clock[chosen], chosen))
        heapq.heappush(planned_heaps[member_key], (planned_clock[chosen], chosen))

source_lines = SOURCE.read_text(encoding="utf-8").splitlines()
needles = [
 "heapq.heappop(machine_heaps[heap_key])",
 "min(candidates, key=lambda e: (employee_free_time[e], e))",
 "plan_ready = max(planned_machine_free[asset_id], planned_employee_free[employee_id], planned_prev_end)",
 "actual_ready = max(machine_free_time[asset_id], employee_free_time[employee_id], prev_end, start_t)",
 "machine_free_time[asset_id] = actual_end_t",
 "employee_free_time[employee_id] = actual_end_t",
 "planned_machine_free[asset_id] = end_t",
 "planned_employee_free[employee_id] = end_t",
 "planned_prev_end = end_t",
 "prev_end = actual_end_t",
 "heapq.heappush(machine_heaps[heap_key], (actual_end_t, asset_id))",
]
locations = {}
for needle in needles:
    locations[needle] = next((i+1 for i,line in enumerate(source_lines) if needle in line), None)

result = {
 "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
 "historical_fix_commit": "e8e4c118e23a8965122869bb3b56bc14e9c17f55",
 "employee_selection_replay": {
   "rows": int(len(rows)),
   "chosen_not_actual_clock_earliest": int(not_actual),
   "chosen_not_planned_clock_earliest": int(not_planned),
   "chosen_not_planned_clock_earliest_rate": not_planned/max(1,len(rows)),
   "examples": examples,
 },
 "state_ownership": {
   "shared_immutable_identity": [
     "order and product routing", "operation sequence", "lot quantity",
     "order factory ownership", "machine capability/factory/line identity",
     "qualified non-leave employee identity", "employee batch/shift and skill efficiency",
     "calibration factor and scheduling iteration order"
   ],
   "planned_state": [
     "planned_machine_free[asset_id]", "planned_employee_free[employee_id]",
     "planned_prev_end for the current lot", "planned_start/end/work_segments"
   ],
   "actual_state": [
     "machine_heaps keyed by actual_end", "machine_free_time[asset_id]",
     "employee_free_time[employee_id]", "prev_end for the current lot",
     "machine_runtime_tracker and breakdown count", "dynamic repair draw",
     "execution variance draw", "actual_start/end/work_segments"
   ]
 },
 "actual_to_planned_dependencies": [
   {
     "source":"machine_heaps actual_end ordering",
     "target":"planned machine, line and capability identity",
     "effect":"selected asset is fixed before plan_ready; stored plan follows actual-clock-selected machine"
   },
   {
     "source":"employee_free_time actual_end ordering",
     "target":"planned employee, batch/calendar and efficiency identity",
     "effect":"selected employee determines planned duration and calendar before plan_ready"
   }
 ],
 "separated_dependencies": [
   "planned predecessor readiness advances only with planned_prev_end",
   "actual predecessor readiness advances only with prev_end=actual_end",
   "planned and actual resource clocks are updated from their respective ends after identity is selected"
 ],
 "plan_to_actual_dependencies": [
   "actual_ready includes planned start as a lower bound, so actual cannot dispatch before plan",
   "actual and planned reuse the already-selected machine and employee identities"
 ],
 "other_contracts": {
   "setup_or_changeover_in_seeder": False,
   "initial_nonusable_machines_in_dispatch_pools": False,
   "leave_workers_in_dispatch_pools": False,
   "on_leave_at_dispatch_can_be_true": False,
   "machine_repair_end_used": False,
   "dynamic_breakdown_changes_identity_pool": False,
   "dynamic_breakdown_delays_selected_machine_through_actual_end": True
 },
 "source_locations": locations,
 "classification":"INCOMPLETE_HISTORICAL_FIX",
 "requires_reseed_retrain_if_corrected": True
}
OUT.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
print(json.dumps(result, indent=2))
