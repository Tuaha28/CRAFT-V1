import sys,json,time
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_final_repair_attempt import _build_true_direct_time_model
s=_prepare_state(); art=_build_true_direct_time_model(s,s['modes']); art['model'].ClearObjective()
print(json.dumps({'model':art['size']},default=str),flush=True)
so=cp_model.CpSolver(); so.parameters.max_time_in_seconds=600; so.parameters.num_search_workers=8; so.parameters.randomize_search=True; so.parameters.random_seed=42
st=so.Solve(art['model']); print(json.dumps({'status':so.StatusName(st),'wall':so.WallTime(),'branches':so.NumBranches(),'conflicts':so.NumConflicts()},default=str),flush=True)
if st in (cp_model.FEASIBLE,cp_model.OPTIMAL):
  from datetime import timedelta
  from dataclasses import replace
  from crafter.disruptions.p2_final_repair_attempt import _validate_selected
  selected={}
  for key,cfgs in art['configs'].items():
    i=next(i for i,l in enumerate(art['config_literals'][key]) if so.Value(l)); base=cfgs[i]; start=int(so.Value(art['starts'][key])); end=int(so.Value(art['ends'][key])); segs=[]
    for seg in art['segments'][key][i]:
      if so.Value(seg['lit']): segs.append((s['problem'].origin+timedelta(seconds=int(so.Value(seg['start']))),s['problem'].origin+timedelta(seconds=int(so.Value(seg['end'])))))
    selected[key]=replace(base,mode_id=base.mode_id+'|DIRECT_COMPACT',start_seconds=start,productive_start_seconds=int((segs[0][0]-s['problem'].origin).total_seconds()) if segs else start+base.setup_seconds,end_seconds=end,elapsed_seconds=end-start,pause_seconds=max(0,end-start-base.setup_seconds-base.productive_seconds),worker_segments=tuple(segs),machine_occupancy_start_seconds=start,machine_occupancy_end_seconds=end,incumbent=False)
  replay,val=_validate_selected(s,selected); print(json.dumps({'replay':replay,'validator':val},default=str),flush=True)
