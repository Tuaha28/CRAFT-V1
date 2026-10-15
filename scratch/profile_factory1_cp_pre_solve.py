from __future__ import annotations
import json, os, sys, time, threading
from pathlib import Path
import psutil
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from scratch.run_factory1_craft_frozen_v1 import _build_loaded
from crafter.repair.planning import PlanningContextV1, build_planning_state, select_precedence_safe_window
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.calendar_regimes import generate_calendar_regimes_selected, CalendarRegimeCache
from crafter.repair.cp_repair import build_repair_model
from crafter.repair.solve import run_canonical_schedule

proc = psutil.Process(os.getpid()); limit = 3_500_000_000; stop = threading.Event()
def watchdog():
    while not stop.wait(0.25):
        rss = proc.memory_info().rss
        if rss >= limit:
            print(json.dumps({"watchdog":"RSS_LIMIT","rss_bytes":rss}, sort_keys=True), flush=True)
            os._exit(3)
threading.Thread(target=watchdog, daemon=True).start()
def mark(name, started):
    print(json.dumps({"stage":name,"seconds":time.perf_counter()-started,"rss_bytes":proc.memory_info().rss}, sort_keys=True), flush=True)

t=time.perf_counter(); loaded,boundary,keys,identity,contract=_build_loaded(); mark("loaded",t)
problem=loaded[0]
ctx0=PlanningContextV1("window-001","INITIAL_PLAN",1,0,0,86400,20000,(),"FROZEN_BENCHMARK_RAW",None,0)
state0=build_planning_state(loaded,ctx0,boundary=boundary)
t=time.perf_counter(); selected,_=select_precedence_safe_window(problem,state0["after_index"],state0["classified"],ctx0,committed_operation_keys=boundary.committed_operation_keys,boundary=boundary,maximum_operations=22); mark("selected",t)
ctx=PlanningContextV1("window-001","INITIAL_PLAN",1,0,0,86400,20000,selected,"FROZEN_BENCHMARK_RAW",None,0)
t=time.perf_counter(); state=build_planning_state(loaded,ctx,boundary=boundary); mark("state_selected",t)
t=time.perf_counter(); configs,audit,summary=generate_resource_configurations(state,max_machine_sets=1,max_worker_crews=3,machine_pool_limit=30); mark("resource_configs",t)
cache=CalendarRegimeCache(); t=time.perf_counter(); regimes,rsummary,total,equiv=generate_calendar_regimes_selected(state,configs,cache=cache); mark("calendar",t)
print(json.dumps({"local_window_ops":len(selected),"resource_configs":sum(len(v) for v in configs.values()),"calendar_regimes":total,"rss_bytes":proc.memory_info().rss},sort_keys=True),flush=True)
t=time.perf_counter(); artifact=build_repair_model(state,regimes); mark("build_repair_model",t)
proto=artifact["model"].Proto(); print(json.dumps({"model_variables":len(proto.variables),"model_constraints":len(proto.constraints),"rss_bytes":proc.memory_info().rss},sort_keys=True),flush=True)
t=time.perf_counter()
prepared={"state":state,"resource_configurations":configs,"resource_audit":audit,"resource_summaries":[summary],"regimes":regimes,"regime_summary":rsummary,"regime_count":total,"calendar_equivalence":equiv}
os.environ.update({"CRAFT_FAST_MODE":"0","CRAFT_ADAPTIVE_CP_BUDGETS":"1","CRAFT_DIAGNOSTIC_FIRST_UNKNOWN":"0"})
result=run_canonical_schedule(prepared, objective_mode=False, plan=((999,None,45.0),), unrestricted_final_stage=True)
print(json.dumps({"stage":"solver_invocation","seconds":time.perf_counter()-t,"rss_bytes":proc.memory_info().rss,"acceptance":result.get("acceptance",{}),"status":result.get("acceptance",{}).get("status")}, default=str, sort_keys=True),flush=True)
stop.set()
