from __future__ import annotations
import contextlib, io, json, sys
from pathlib import Path
sys.path.insert(0, ".")
import scratch.diagnose_window14_v3 as d
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3

RUN=Path('outputs/crafter/factory2_172_v1')
OUT=RUN/'window14_step4a'
d.RUN=RUN; d.OUT=OUT

def quiet(fn,*a,**k):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()): return fn(*a,**k)

def context(keys):
    return PlanningContextV1('window-014-supplement','ROLLING_PLAN',2,326400,326400,412800,346400,tuple(keys),'FROZEN_BENCHMARK_RAW',snapshot_as_of_seconds=0,previous_provisional_plan_reference='provisional-013')

def compact(x):
    return {k:x.get(k) for k in ('label','machine_cap','worker_cap','unrestricted_final_stage','required_operation_count','resource_configuration_count','calendar_regime_count','candidate_exists','canonical_status','cp_status','stages','scoped_acceptance','prepare_seconds','canonical_seconds','resource_domain_summary')}

def main():
    loaded=quiet(_load_state); problem,_,_,_,_,route,_,_,_=loaded
    windows, rows=quiet(d._accepted_rows); boundary,facts=quiet(d._make_boundary,route,rows,2)
    failure=json.loads((RUN/'window_014.json').read_text()); keys=tuple(failure['window_acceptance']['scope']['missing_required_operations'])
    plan=TemporalEscalationPolicyV3().plans[1]
    cases=[]
    for label, mc,wc in [('EXACT_22_1X8_UNRESTRICTED',1,8),('EXACT_22_3X3_UNRESTRICTED',3,3)]:
        state=quiet(build_planning_state,loaded,context(keys),boundary=boundary)
        print('START',label,flush=True); r=quiet(d._run_case,label,state,facts,keys,mc,wc,unrestricted=True,plan=plan); print('DONE',label,r.get('candidate_exists'),r.get('cp_status'),flush=True); cases.append(compact(r))
    variants={
        'KEEP_MACHINE_ONLY_REMOVE_WORKER_ROUTING':(True,False,False),
        'KEEP_WORKER_ONLY_REMOVE_MACHINE_ROUTING':(False,True,False),
    }
    for label,(km,kw,kr) in variants.items():
        b,f=d._boundary_variant(boundary,facts,keep_machine=km,keep_worker=kw,keep_routing=kr)
        state=quiet(build_planning_state,loaded,context(keys),boundary=b)
        print('START',label,flush=True); r=quiet(d._run_case,label+'_1X3_UNRESTRICTED_DIAGNOSTIC',state,f,keys,1,3,unrestricted=True,plan=plan); print('DONE',label,r.get('candidate_exists'),r.get('cp_status'),flush=True); cases.append(compact(r))
    with (OUT/'resource_domain_experiments.jsonl').open('a',encoding='utf-8') as h:
        for r in cases[:2]: h.write(json.dumps(r,default=str,sort_keys=True)+'\n')
    with (OUT/'boundary_ablations.jsonl').open('a',encoding='utf-8') as h:
        for r in cases[2:]: h.write(json.dumps({**r,'diagnostic_only':True,'accepted_for_production':False},default=str,sort_keys=True)+'\n')
    (OUT/'supplemental_probes.json').write_text(json.dumps({'resource_wider_time':cases[:2],'boundary_two_dimension':cases[2:],'database_writes':0},indent=2,default=str)+'\n')

if __name__=='__main__': main()
