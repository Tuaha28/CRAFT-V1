import sys,json
sys.path.insert(0,'.')
from crafter.repair.engine import run_p2_repair
from ortools.sat.python import cp_model
r=run_p2_repair(); print('region',len(r['state']['keys']),'regimes',r['regime_count'],flush=True)
one={k:(next((x for x in rs if x['base'].incumbent and x['lo']<=x['base'].start_seconds<=x['hi']),rs[0]),) for k,rs in r['regimes'].items()}
a=__import__('crafter.disruptions.p2_compressed_timing_repair',fromlist=['_build'])._build(r['state'],one,include_reservations=False,include_machine_internal=False,include_worker_internal=False,include_changeover=False)
so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=60; so.parameters.num_search_workers=1; st=so.Solve(a['model']); print('one_route',so.StatusName(st),so.WallTime(),flush=True)
for name,kw in [('base',{}),('noroute',{'include_routing':False}),('nores',{'include_reservations':False}),('nomachine',{'include_machine_internal':False}),('noworker',{'include_worker_internal':False}),('nochg',{'include_changeover':False})]:
 a=__import__('crafter.repair.cp_repair',fromlist=['build_repair_model']).build_repair_model(r['state'],r['regimes']) if not kw else __import__('crafter.disruptions.p2_compressed_timing_repair',fromlist=['_build'])._build(r['state'],r['regimes'],**kw)
 so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=20; so.parameters.num_search_workers=1; st=so.Solve(a['model']); print(name,so.StatusName(st),so.WallTime(),flush=True)
