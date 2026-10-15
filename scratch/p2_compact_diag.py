import json,sys
sys.path.insert(0,'.')
from crafter.disruptions.p2_compressed_timing_repair import _prepare_state,_generate_regimes,_build
from crafter.lns_probe.anchored_model import outside_machine_reservations,outside_worker_reservations
from ortools.sat.python import cp_model
s=_prepare_state(); s['_machine_res']=outside_machine_reservations(s['problem'],s['after_index'],set(s['keys'])); s['_worker_res']=outside_worker_reservations(s['problem'],s['after_index'],set(s['keys']))
regs,rows,total=_generate_regimes(s)
print('REGIMES',total)
for label,kw in [('full',{}),('no_outside',{'include_outside':False}),('no_res',{'include_reservations':False}),('no_route',{'include_routing':False})]:
    a=_build(s,regs,**kw); so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=30; so.parameters.num_search_workers=1
    st=so.Solve(a['model']); print(label,so.StatusName(st),so.WallTime(),a['size'])
