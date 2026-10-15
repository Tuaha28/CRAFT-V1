import sys,json,time
sys.path.insert(0,'.')
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_compressed_timing_repair import _add_boundary_halo,_generate_regimes,_build
from crafter.lns_probe.anchored_model import outside_machine_reservations,outside_worker_reservations
from ortools.sat.python import cp_model
s=_prepare_state(); s['_machine_res']=outside_machine_reservations(s['problem'],s['after_index'],set(s['keys'])); s['_worker_res']=outside_worker_reservations(s['problem'],s['after_index'],set(s['keys'])); s,added,hard=_add_boundary_halo(s); regs,rows,total=_generate_regimes(s); print('prepared',len(s['keys']),total,flush=True)
variants=[('calendar_only',dict(include_routing=False,include_reservations=False,include_machine_internal=False,include_worker_internal=False,include_changeover=False)),('routing',dict(include_routing=True,include_outside=False,include_reservations=False,include_machine_internal=False,include_worker_internal=False,include_changeover=False)),('routing_outside',dict(include_routing=True,include_outside=True,include_reservations=False,include_machine_internal=False,include_worker_internal=False,include_changeover=False)),('machine_internal',dict(include_routing=False,include_reservations=False,include_machine_internal=True,include_worker_internal=False,include_changeover=False)),('worker_internal',dict(include_routing=False,include_reservations=False,include_machine_internal=False,include_worker_internal=True,include_changeover=False)),('all_no_changeover',dict(include_changeover=False)),('full',dict())]
out=[]
for name,kw in variants:
 a=_build(s,regs,**kw); so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=30; so.parameters.num_search_workers=1; st=so.Solve(a['model']); row={'family':name,'status':so.StatusName(st),'wall':so.WallTime(),'model':a['size']}; print(name,row['status'],row['wall'],flush=True); out.append(row)
json.dump({'operations':len(s['keys']),'regimes':total,'added':added,'hard':hard,'variants':out},open('outputs/crafter/working_craft_p2_autonomous_v1/presolve_family_isolation.json','w'),indent=2)
