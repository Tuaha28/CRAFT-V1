from __future__ import annotations
import json, os, sys, time, threading
from pathlib import Path
import psutil
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from scratch.run_factory1_craft_frozen_v1 import _build_loaded
from crafter.repair.planning import PlanningContextV1, build_planning_state, select_precedence_safe_window, prepare_planning_schedule
from crafter.production.controller_v1 import _failure_classification
from crafter.repair.boundary_aware_modes import build_boundary_availability_index
from crafter.repair.calendar_regimes import IncrementalCalendarRegimeBuilder
from crafter.repair.route_aware_crew_expansion import (
    worker_domain_accounting, build_worker_contention, authoritative_start_probes,
    generate_additional_crew_configurations, merge_additional_configurations,
    _routing_aware_earliest, select_route_successor_closure,
)
from crafter.repair.solve import run_canonical_schedule

proc=psutil.Process(); limit=3_500_000_000; stop=threading.Event()
def watchdog():
    while not stop.wait(.25):
        rss=proc.memory_info().rss
        if rss>=limit:
            print(json.dumps({"watchdog":"RSS_LIMIT","rss_bytes":rss},sort_keys=True),flush=True); os._exit(3)
threading.Thread(target=watchdog,daemon=True).start()
def mark(stage,t,**extra):
    print(json.dumps({"stage":stage,"seconds":time.perf_counter()-t,"rss_bytes":proc.memory_info().rss,**extra},sort_keys=True,default=str),flush=True)

t=time.perf_counter(); loaded,boundary,keys,identity,contract=_build_loaded(); mark("loaded",t)
problem=loaded[0]
ctx0=PlanningContextV1("window-001","INITIAL_PLAN",1,0,0,86400,20000,(),"FROZEN_BENCHMARK_RAW",None,0)
state0=build_planning_state(loaded,ctx0,boundary=boundary)
t=time.perf_counter(); selected,_=select_precedence_safe_window(problem,state0["after_index"],state0["classified"],ctx0,committed_operation_keys=boundary.committed_operation_keys,boundary=boundary,maximum_operations=22); mark("selected",t,count=len(selected))
ctx=PlanningContextV1("window-001","INITIAL_PLAN",1,0,0,86400,20000,selected,"FROZEN_BENCHMARK_RAW",None,0)
t=time.perf_counter(); state=build_planning_state(loaded,ctx,boundary=boundary); mark("state_selected",t)
t=time.perf_counter(); prepared=prepare_planning_schedule(state,max_machine_sets=1,max_worker_crews=3); mark("prepared",t,configs=sum(len(v) for v in prepared['resource_configurations'].values()),regimes=prepared['regime_count'])
classification,diagnosis=_failure_classification(prepared)
print(json.dumps({"stage":"classification","classification":classification,"diagnosis_count":len(diagnosis.get("worker_route_pressure_all") or ()),"pressured_count":len(diagnosis.get("worker_route_pressure") or ()),"truncation_count":len(diagnosis.get("worker_truncation_evidence") or ())},sort_keys=True),flush=True)
availability=build_boundary_availability_index(state,boundary,horizon_start_seconds=0)
incremental=IncrementalCalendarRegimeBuilder(state,cache=None)
incremental.seed(prepared['resource_configurations'],prepared['regimes'],prepared.get('regime_summary',()),prepared.get('calendar_equivalence',()),additional_breakpoints_by_operation={})
t=time.perf_counter(); base_regimes,base_summary,base_total,base_equiv=incremental.build(prepared['resource_configurations'],additional_breakpoints_by_operation={}); mark("incremental_seed_rebuild",t,regimes=base_total,meta=base_equiv.get('incremental_profile'))
active=set(state['keys']); accounting=worker_domain_accounting(prepared['resource_configurations'],prepared['resource_audit'],active); pressure=build_worker_contention(prepared['resource_audit'],active)
ranked=[row for row in (diagnosis.get('worker_route_pressure_all') or ()) if row.get('route_pressure') and accounting.get(str(row['operation_key']),{}).get('worker_domain_truncated',False)]
targets=[str(row['operation_key']) for row in ranked]
print(json.dumps({"stage":"targets","targets":targets,"accounting_truncated":sum(1 for x in accounting.values() if x.get('worker_domain_truncated'))},sort_keys=True),flush=True)
if targets:
    key=targets[0]; ready=next(row.get('earliest_ready') for row in (diagnosis.get('worker_route_pressure_all') or ()) if str(row.get('operation_key'))==key)
    t=time.perf_counter(); probes=authoritative_start_probes(state,availability,prepared['resource_configurations'],key,int(ready)); mark("probes",t,key=key,count=len(probes))
    t=time.perf_counter(); generated,trace=generate_additional_crew_configurations(state,availability,prepared['resource_configurations'],key,int(ready),8,worker_pressure=pressure,additional_start_probes=probes); mark("generate_additional_crews",t,key=key,generated=len(generated),trace=trace)
    expanded=merge_additional_configurations(prepared['resource_configurations'],{key:generated}); print(json.dumps({"stage":"expanded_domain","configs":sum(len(v) for v in expanded.values()),"rss_bytes":proc.memory_info().rss},sort_keys=True),flush=True)
    t=time.perf_counter(); expanded_regimes,expanded_meta,expanded_total,expanded_equiv=incremental.build(expanded,additional_breakpoints_by_operation={key:probes}); mark("incremental_expanded_regimes",t,total=expanded_total,meta=expanded_equiv.get('incremental_profile'))
    current=expanded; current_regimes=expanded_regimes; current_probes={key:probes}
    for round_no, quota in ((2,16),(3,32),(4,64)):
        additions={}; round_t=time.perf_counter()
        for target in targets:
            info=next((row for row in (diagnosis.get('worker_route_pressure_all') or ()) if str(row.get('operation_key'))==target), None)
            ready=int(info.get('earliest_ready')) if info and info.get('earliest_ready') is not None else 0
            generated,_trace=generate_additional_crew_configurations(state,availability,current,target,ready,quota,worker_pressure=pressure,additional_start_probes=authoritative_start_probes(state,availability,current,target,ready))
            if generated: additions[target]=generated
        current=merge_additional_configurations(current,additions)
        t=time.perf_counter(); current_regimes,_,current_total,current_equiv=incremental.build(current,additional_breakpoints_by_operation=current_probes); mark("round_%d_expansion"%round_no,t,quota=quota,added=sum(len(v) for v in additions.values()),configs=sum(len(v) for v in current.values()),regimes=current_total,meta=current_equiv.get('incremental_profile'),round_seconds=time.perf_counter()-round_t)
stop.set()
