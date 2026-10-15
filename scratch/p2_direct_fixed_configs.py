import sys,json
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_final_repair_attempt import _build_true_direct_time_model,_config_key
s=_prepare_state(); modes={}
for k,vals in s['modes'].items():
    inc=next((m for m in vals if m.incumbent), vals[0]); modes[k]=(inc,)
art=_build_true_direct_time_model(s,modes); art['model'].ClearObjective(); print(json.dumps(art['size']),flush=True)
so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=300; so.parameters.num_search_workers=8; so.parameters.randomize_search=True; so.parameters.random_seed=42
st=so.Solve(art['model']); print(json.dumps({'status':so.StatusName(st),'wall':so.WallTime(),'branches':so.NumBranches(),'conflicts':so.NumConflicts()}),flush=True)
