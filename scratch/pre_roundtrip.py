import json,sys,time
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.cp_repair import build_repair_model
from crafter.disruptions.benchmark import _load_state
from crafter.lns_probe.anchored_model import outside_machine_reservations,outside_worker_reservations
from crafter.disruptions.p2_compressed_timing_repair import _generate_regimes
OUT='outputs/crafter/working_craft_p2_decisive_v1/new_engine_pre_event_roundtrip.json'
def solve(a):
    s=cp_model.CpSolver(); s.parameters.max_time_in_seconds=40; s.parameters.num_search_workers=1
    st=s.Solve(a['model']); return {'status':s.StatusName(st),'code':int(st),'wall':s.WallTime(),'size':a['size']}
def main():
    r=run_p2_repair(); st=r['state']
    p,oi,rows,durs,details,idx,base,checked,source=_load_state()
    pre=dict(st); pre.update(problem=p,operator_index=oi,rows=rows,op_details=details,after_index=idx,baseline=base,event=None)
    pre['_machine_res']=outside_machine_reservations(p,idx,set(st['keys']))
    pre['_worker_res']=outside_worker_reservations(p,idx,set(st['keys']))
    regs,summary,total=_generate_regimes(pre)
    one={k:(next((x for x in v if x['base'].incumbent and x['lo']<=x['base'].start_seconds<=x['hi']),v[0]),) for k,v in regs.items() if v}
    force={k:0 for k in one}
    A=build_repair_model(pre,one,force_regime_indices=force,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False)
    C=build_repair_model(pre,one,force_regime_indices=force)
    out={'status':'EXECUTED_NEW_ENGINE_PRE_EVENT','operations':len(one),'regimes':total,'control_A':solve(A),'control_C':solve(C),'database_writes':0}
    open(OUT,'w').write(json.dumps(out,indent=2)); print(out)
if __name__=='__main__': main()
