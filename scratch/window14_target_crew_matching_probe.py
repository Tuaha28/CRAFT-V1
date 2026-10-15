"""Matching-only probe for the first route-bound bottleneck."""
from __future__ import annotations
import json
from pathlib import Path
import sys
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import build_planning_state
from crafter.repair.boundary_aware_modes import build_boundary_availability_index, _crew_candidates
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, rows_and_provenance, quiet

def configs_for_successor(state, boundary):
    from crafter.repair.boundary_aware_modes import generate_boundary_aware_configurations
    configs, _funnel, _stats, _index = quiet(generate_boundary_aware_configurations, state, boundary, crew_diversity=16, machine_set_cap=3, max_start_probes=4)
    return configs.get('251_2066', ())

def main():
    loaded=quiet(_load_state); route=loaded[5]; accepted,rows,_=rows_and_provenance(); boundary,_facts=make_boundary(route,rows)
    state=quiet(build_planning_state,loaded,context(W14_KEYS),boundary=boundary)
    index=build_boundary_availability_index(state,boundary,horizon_start_seconds=0)
    op=state['problem'].operation_by_key['251_2065']; detail=state['op_details']['251_2065']
    out={'operation_key':'251_2065','boundary_end_test_start_seconds':528572,'results':[],'successor_operation_key':'251_2066','successor_start_test_seconds':535138,'successor_results':[],'database_writes':0}
    for line_id,k in ((21,11),(32,10)):
        machine_sets=[]
        for mid in sorted(int(x.machine_id) for x in state['problem'].machines if int(x.factory_id)==2 and int(x.line_id)==line_id)[:3]:
            machine_sets.append((mid,))
        for machine_set in machine_sets:
            crews=_crew_candidates(state,index,'251_2065',line_id,k,machine_set,528572,max_crews=8)
            out['results'].append({'line_id':line_id,'k':k,'machine_set':list(machine_set),'crew_count':len(crews),'crews':[list(x) for x in crews]})
    # Also test the immediate successor at the end of the hidden-crew target
    # (the existing K=16 successor domain is still truncated).
    seen=set()
    successor_configs=configs_for_successor(state, boundary)
    for cfg in successor_configs:
        signature=(int(cfg.line_id),int(cfg.k))
        if signature in seen: continue
        seen.add(signature)
        samples=[]
        for start in (535138,600000,700000,1000000,2000000):
            crews=_crew_candidates(state,index,'251_2066',int(cfg.line_id),int(cfg.k),cfg.machine_ids,start,max_crews=8)
            samples.append({'start_seconds':start,'crew_count':len(crews),'crews':[list(x) for x in crews]})
        out['successor_results'].append({'line_id':signature[0],'k':signature[1],'samples':samples})
    Path('outputs/crafter/factory2_172_v1/window14_step4b/target_crew_matching_probe.json').write_text(json.dumps(out,indent=2,default=str)+'\n',encoding='utf-8')
if __name__=='__main__': main()
