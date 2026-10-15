"""Bounded targeted worker-domain probe for the diagnosed W14 chain."""
from __future__ import annotations
import json
from pathlib import Path
import sys
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import build_planning_state
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.boundary_aware_modes import generate_boundary_aware_configurations
from crafter.repair.engine import _routing_aware_earliest
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, rows_and_provenance, quiet

def main():
    loaded=quiet(_load_state); route=loaded[5]; accepted,rows,_=rows_and_provenance(); boundary,_facts=make_boundary(route,rows)
    state=quiet(build_planning_state,loaded,context(W14_KEYS),boundary=boundary)
    out={"target_operation":"251_2065","database_writes":0,"levels":[]}
    for k in (32,):
        configs, funnel, stats, idx=quiet(generate_boundary_aware_configurations,state,boundary,crew_diversity=k,machine_set_cap=3,max_start_probes=4)
        cache=CalendarRegimeCache(); regimes,_,total,_=quiet(generate_calendar_regimes,state,configs,cache=cache)
        bounds=_routing_aware_earliest(state,regimes)
        row={"worker_crew_diversity":k,"resource_configurations":sum(len(v) for v in configs.values()),"calendar_regimes":total,"target_worker_choices":len({str(w) for c in configs['251_2065'] for w in c.worker_ids}),"target_configurations":len(configs['251_2065']),"target_earliest_start":bounds.get('earliest_start',{}).get('251_2065'),"target_earliest_end":bounds.get('earliest_end',{}).get('251_2065'),"successor_earliest_start":bounds.get('earliest_start',{}).get('251_2066'),"successor_earliest_end":bounds.get('earliest_end',{}).get('251_2066'),"unresolved":bounds.get('unresolved',[])}
        out["levels"].append(row)
    Path("outputs/crafter/factory2_172_v1/window14_step4b/targeted_worker_expansion_probe.json").write_text(json.dumps(out,indent=2,default=str)+"\n",encoding="utf-8")
if __name__=="__main__": main()
