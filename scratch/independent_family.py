import json,sys
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.cp_repair import build_repair_model
def solve(a):
 s=cp_model.CpSolver(); s.parameters.max_time_in_seconds=30; s.parameters.num_search_workers=1; z=s.Solve(a['model']); return {'status':s.StatusName(z),'size':a['size'],'wall':s.WallTime()}
def main():
 r=run_p2_repair(max_machine_sets=3,max_worker_crews=3,progress=lambda x: print(json.dumps(x),flush=True)); st=r['state']; rg=r['regimes']; out=[]
 probes=[('FULL',{}),('NO_OUTSIDE_ROUTING',{'include_outside_routing':False}),('NO_INTERNAL_ROUTING',{'include_internal_routing':False}),('NO_OUTSIDE_MACHINE',{'include_outside_machine_reservations':False}),('NO_INTERNAL_MACHINE',{'include_internal_machine_no_overlap':False}),('NO_OUTSIDE_WORKER',{'include_outside_worker_reservations':False}),('NO_INTERNAL_WORKER',{'include_internal_worker_no_overlap':False}),('NO_CHANGEOVER',{'include_changeover':False})]
 for n,kw in probes:
  x=solve(build_repair_model(st,rg,**kw)); x['probe']=n; out.append(x); print(n,x['status'],flush=True)
 open('outputs/crafter/p2_independent_resource_repair_v6/same_builder_family.json','w').write(json.dumps(out,indent=2))
if __name__=='__main__':main()
