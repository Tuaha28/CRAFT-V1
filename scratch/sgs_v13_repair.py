import json, sys
from pathlib import Path
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import replace
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_final_repair_attempt import _config_key, _validate_selected
from crafter.lns_probe.run_e1_7_final_verified import _replay_mode
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.lns_probe.run_e1_7_final_verified import _phenotype
from crafter.phenotype import validate
from crafter.lns_probe.transaction import _machine_accepts

data=json.loads(open('outputs/crafter/p2_final_repair_attempt_v13/materialized_schedule.json',encoding='utf-8').read())
state=_prepare_state(); keys=set(data['selected']); idx=state['after_index']; problem=state['problem']
base_by={k:next(m for m in state['modes'][k] if int(m.line_id)==int(r['line']) and int(m.machine_count)==int(r['k']) and tuple(map(int,m.machine_ids))==tuple(map(int,r['machines'])) and tuple(map(str,m.worker_ids))==tuple(map(str,r['workers']))) for k,r in data['selected'].items()}
baseline_by={x.assignment.operation_key:x.assignment for x in state['baseline'].operations}
wm=outside_worker_reservations(problem,idx,keys); mm=outside_machine_reservations(problem,idx,keys)
for v in wm.values(): v.sort()
for v in mm.values(): v.sort()
pred={k:idx.route_predecessor.get(k) for k in keys}
done={}; remaining=set(keys); guard=0
while remaining and guard<500:
    guard+=1; ready=[]
    for k in remaining:
        p=pred.get(k)
        if p not in keys or p in done:
            ready.append(k)
    if not ready:
        print('CYCLE_OR_MISSING_PRED', remaining); break
    ready.sort(key=lambda k:(int(data['selected'][k]['start']),str(k)))
    k=ready[0]; remaining.remove(k); base=base_by[k]; row=data['selected'][k]
    p=pred.get(k)
    lower=int(row['start'])
    if p in done: lower=max(lower,int(done[p].end_seconds))
    elif p in baseline_by: lower=max(lower,int(baseline_by[p].end_seconds))
    delayed=str(state['event'].metadata.get('predecessor_operation_key',''))
    if p==delayed: lower=max(lower,int(state['event'].metadata.get('delayed_end_seconds',0)))
    release=int(problem.order_by_id[problem.operation_by_key[k].order_id].release_seconds or 0)
    lower=max(lower,release)
    found=None
    for attempt in range(200):
        ready_dt=problem.origin+timedelta(seconds=lower+int(base.setup_seconds))
        end_dt,segs=_fixed_crew_execution(problem,state['operator_index'],base.worker_ids,ready_dt,float(base.productive_minutes),wm)
        end=int(round((end_dt-problem.origin).total_seconds()))
        if end>int(problem.horizon_seconds): break
        bad=[]
        for mid in base.machine_ids:
            if not _machine_accepts(mm.get(int(mid),()),lower,end,problem.operation_by_key[k].order_id):
                bad.append(max((int(x[1]) for x in mm.get(int(mid),()) if int(x[0])<end and lower<int(x[1])), default=lower+1))
        if not bad:
            found=(lower,end,segs); break
        lower=max(bad)+1
    if found is None:
        print('UNSCHEDULED',k); continue
    start,end,segs=found
    mode=replace(base,mode_id=f'{base.mode_id}|SGS_V13',start_seconds=start,productive_start_seconds=start+int(base.setup_seconds),end_seconds=end,elapsed_seconds=end-start,productive_seconds=int(round(sum((b-a).total_seconds() for a,b in segs))),pause_seconds=max(0,end-(start+int(base.setup_seconds))-int(round(sum((b-a).total_seconds() for a,b in segs)))),worker_segments=tuple(segs),machine_occupancy_start_seconds=start,machine_occupancy_end_seconds=end,incumbent=False,provenance=tuple(base.provenance)+('SGS_V13',),recovery_level='BOUNDED_CRAFT_REPAIR',recovery_reason='P2_SGS_REPAIR',source_event_id=state['event'].event_id)
    done[k]=mode
    for wid in mode.worker_ids: wm.setdefault(str(wid),[]).extend(mode.worker_segments); wm[str(wid)].sort()
    for mid in mode.machine_ids: mm.setdefault(int(mid),[]).append((start,end,problem.operation_by_key[k].order_id,k)); mm[int(mid)].sort()
replay,validator=_validate_selected(state,done)
vr=validate(state['problem'],_phenotype(state['problem'],state['baseline'],done),state['operator_index'])
print(json.dumps({'scheduled':len(done),'guard':guard,'replay':replay,'validator':validator,'report':vr.validation_report},indent=2,default=str))
