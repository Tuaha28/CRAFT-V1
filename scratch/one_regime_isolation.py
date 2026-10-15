import json,sys,time
from pathlib import Path
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.cp_repair import build_repair_model
from crafter.disruptions.benchmark import _load_state
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.disruptions.p2_compressed_timing_repair import _generate_regimes
OUT=Path('outputs/crafter/working_craft_p2_decisive_v1')
def solve(a,limit=20):
 s=cp_model.CpSolver(); s.parameters.max_time_in_seconds=limit; s.parameters.num_search_workers=1
 t=time.time(); st=s.Solve(a['model']); return {'status':s.StatusName(st),'code':int(st),'wall':s.WallTime(),'elapsed':time.time()-t,'size':a['size']}
def main():
 r=run_p2_repair(); state=r['state']; rs=r['regimes']
 # New-engine pre-event control: restore the authoritative pre-event problem,
 # index and baseline, retain only the same physical operation slice, and
 # regenerate regimes through the canonical builder path.
 p0,oi0,rows0,durs0,details0,idx0,base0,checked0,source0=_load_state()
 pre=dict(state); pre.update(problem=p0,operator_index=oi0,rows=rows0,op_details=details0,after_index=idx0,baseline=base0,event=None)
 pre['_machine_res']=outside_machine_reservations(p0,idx0,set(state['keys']))
 pre['_worker_res']=outside_worker_reservations(p0,idx0,set(state['keys']))
 pre_rs,pre_sum,pre_total=_generate_regimes(pre)
 pre_one={k:(next((x for x in v if x['base'].incumbent and x['lo'] <= x['base'].start_seconds <= x['hi']),v[0]),) for k,v in pre_rs.items() if v}
 pre_a=build_repair_model(pre,pre_one,force_regime_indices={k:0 for k in pre_one},include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)
 pre_b=build_repair_model(pre,pre_one,force_regime_indices={k:0 for k in pre_one})
 dump={'control_A':solve(pre_a,20),'control_C':solve(pre_b,20),'operations':len(pre_one),'regimes':pre_total,'status':'EXECUTED_NEW_ENGINE_PRE_EVENT'}
 (OUT/'new_engine_pre_event_roundtrip.json').write_text(json.dumps(dump,indent=2),encoding='utf-8')
 one={k:(next((x for x in v if x['base'].incumbent and x['lo']<=x['base'].start_seconds<=x['hi']),v[0]),) for k,v in rs.items()}
 arc_diag=[]
 for k in state['keys']:
  s=state['after_index'].route_successor.get(k)
  if s in rs:
   ends=[int(rg['formula'][0][0])*int(rg['lo'])+int(rg['formula'][0][1]) for rg in rs[k]]
   starts=[int(rg['lo']) for rg in rs[s]]
   arc_diag.append({'pred':k,'succ':s,'pred_min_end':min(ends),'succ_max_start':max(starts),'compatible_possible':any(e<=st for e in ends for st in starts),'pred_modes':len(rs[k]),'succ_modes':len(rs[s])})
 (OUT/'routing_arc_domain_diagnostic.json').write_text(json.dumps(arc_diag,indent=2),encoding='utf-8')
 fam=[('F0',dict(include_internal_routing=False,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=False,include_event_bounds=False)),('F1',dict(include_internal_routing=False,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('F3',dict(include_internal_routing=True,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('F4',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('F5',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('F6',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)),('F7',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=False,include_changeover=False)),('F8',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=True,include_changeover=False)),('F9',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=True,include_changeover=True))]
 out=[]
 for n,kw in fam:
  a=build_repair_model(state,one,force_regime_indices={k:0 for k in one},**kw); x=solve(a); x['family']=n; out.append(x); print(n,x,flush=True)
 edges=[]
 for k in state['keys']:
  s=state['after_index'].route_successor.get(k)
  if s in state['keys']: edges.append((k,s))
 edge_results=[]
 for edge in edges:
  a=build_repair_model(state,one,force_regime_indices={k:0 for k in one},exclude_routing_edges={edge},include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)
  x=solve(a,5); x['edge']=edge; edge_results.append(x)
  if x['status'] in ('FEASIBLE','OPTIMAL'): print('EDGE_RESTORES',edge,flush=True)
 (OUT/'one_regime_family_isolation.json').write_text(json.dumps({'region':len(state['keys']),'results':out,'edge_results':edge_results},indent=2),encoding='utf-8')
if __name__=='__main__':main()
