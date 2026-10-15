"""Compute exact regime/precedence lower-bound diagnostics read-only."""
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
    configs,_,_,_=quiet(generate_boundary_aware_configurations,state,boundary,crew_diversity=16,machine_set_cap=3,max_start_probes=4)
    cache=CalendarRegimeCache(); regimes,_,total,_=quiet(generate_calendar_regimes,state,configs,cache=cache)
    bounds=_routing_aware_earliest(state,regimes)
    out={"calendar_regimes":total,"unresolved":bounds.get("unresolved",[]),"bounds":{},"database_writes":0}
    for key in sorted(W14_KEYS):
        rs=regimes.get(key,())
        out["bounds"][key]={"regime_count":len(rs),"min_lo":min((int(r.get('lo',0)) for r in rs),default=None),"max_hi":max((int(r.get('hi',0)) for r in rs),default=None),"earliest_start":bounds.get('earliest_start',{}).get(key),"earliest_end":bounds.get('earliest_end',{}).get(key)}
    Path("outputs/crafter/factory2_172_v1/window14_step4b/route_bound_diagnostics.json").write_text(json.dumps(out,indent=2,default=str)+"\n",encoding="utf-8")
if __name__=="__main__": main()
