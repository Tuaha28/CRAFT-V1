import json,sys
from types import SimpleNamespace
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.disruptions.benchmark import _load_state,_candidate_domains,_neighborhood
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.repair.cp_repair import build_repair_model
from crafter.disruptions.p2_compressed_timing_repair import _generate_regimes
from crafter.lns_probe.anchored_model import outside_machine_reservations,outside_worker_reservations
def main():
 p,oi,rows,durs,details,idx,base,checked,source=_load_state(); keys=('1029_292','1029_293','1029_294'); ev=SimpleNamespace(factory_id=idx.operation_features[keys[0]]['factory_id'],metadata={},event_time_seconds=0)
 hood=_neighborhood(idx,ev,keys,42); dom=_candidate_domains(idx,p,keys,event=None); ms,_=generate_machine_set_candidates(p,idx,hood,keys,dom,max_sets=3,machine_pool_limit=30); modes,fail,cnt=generate_joint_modes(p,details,oi,idx,hood,keys,dom,ms,max_starts=10,event=None)
 st={'problem':p,'operator_index':oi,'rows':rows,'op_details':details,'after_index':idx,'baseline':base,'event':None,'keys':keys,'neighborhood':hood,'domains':dom,'relaxable':keys,'modes':modes,'_machine_res':outside_machine_reservations(p,idx,set(keys)),'_worker_res':outside_worker_reservations(p,idx,set(keys))}
 regs,summary,total=_generate_regimes(st); inc={k:(next((x for x in v if x['base'].incumbent and x['lo']<=x['base'].start_seconds<=x['hi']),v[0]),) for k,v in regs.items()}; force={k:0 for k in inc}
 out={}
 for n,kw in [('A',dict(force_regime_indices=force)),('B',dict(force_regime_indices=None,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('C',dict())]:
  a=build_repair_model(st,inc,**kw); s=cp_model.CpSolver(); s.parameters.max_time_in_seconds=20; s.parameters.num_search_workers=1; z=s.Solve(a['model']); out[n]={'status':s.StatusName(z),'size':a['size']}
 print(json.dumps({'status':'NEW_ENGINE_PRE_EVENT_3_OPERATION_CONTROL','operations':len(keys),'regimes':total,'results':out},indent=2)); open('outputs/crafter/working_craft_p2_decisive_v1/new_engine_pre_event_roundtrip.json','w').write(json.dumps({'status':'NEW_ENGINE_PRE_EVENT_3_OPERATION_CONTROL','operations':len(keys),'regimes':total,'results':out,'database_writes':0},indent=2))
if __name__=='__main__':main()
