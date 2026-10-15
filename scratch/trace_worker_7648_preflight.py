import os, json, copy, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DB_NAME']='garment_fyp_db_ga_snapshot_scheduler_integration_20260916_seed42'
from database.ga_snapshot import activate_snapshot, get_snapshot_origin
activate_snapshot('scheduler_integration_20260916_seed42')
from database.queries import fetch_active_orders_and_routings, fetch_machine_state, fetch_concurrent_operator_capacity, fetch_assignable_operators, fetch_line_factory_map, fetch_committed_machine_state, fetch_committed_operator_reservations
from database.db import get_engine
import pandas as pd
from agent.decision_engine import AIDecisionEngine
from utils.factory_ownership import resolve_ownership
from ga.scheduler import GAScheduler
from ga.heuristics import baseline_chromosomes
import ga.fitness as fitness

origin=get_snapshot_origin('scheduler_integration_20260916_seed42')
routings=fetch_active_orders_and_routings(); machines=fetch_machine_state()
lines=pd.read_sql('SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active FROM Production_Lines',get_engine())
capacity=fetch_concurrent_operator_capacity(); ownership=resolve_ownership(routings.production_order_id)
ops=fetch_assignable_operators(); engine=AIDecisionEngine(); index=engine._build_operator_index(ops,lines,fetch_line_factory_map())
free,last=fetch_committed_machine_state(origin); reserved=fetch_committed_operator_reservations(origin)
order_release=routings.groupby('production_order_id').release_date.min().reset_index().sort_values(['release_date','production_order_id'])
ids=set(order_release.production_order_id.head(300)); sub=routings[routings.production_order_id.isin(ids)].copy()
sched=GAScheduler(sub,machines,lines,{},current_time=origin,present_operator_count=capacity,ownership_snapshot=ownership,initial_machine_free=free,initial_machine_last_order=last,operator_index=index,initial_operator_reservations=reserved,scored_order_ids={str(x) for x in ids})
chromo=baseline_chromosomes(sched.factory,sched.op_details,sched.order_deadlines_minutes,sched.seq_lookup)['EDF']
trace=[]; original=fitness.allocate_crew
def wrapped(*args,**kwargs):
    res=args[6]
    before=copy.deepcopy(res.get('7648',()))
    out=original(*args,**kwargs)
    after=copy.deepcopy(out.get('reservations',{}).get('7648',()))
    selected=[str(x) for x in out.get('employees',())]
    trace.append({'ready':args[4].isoformat(),'before':[(a.isoformat(),b.isoformat()) for a,b in before], 'selected':selected, 'segments':[(a.isoformat(),b.isoformat()) for a,b in out.get('segments',())], 'after':[(a.isoformat(),b.isoformat()) for a,b in after]})
    return out
fitness.allocate_crew=wrapped
try:
    fit,det=fitness.evaluate_schedule(chromo,sched.num_ops,sched.op_details,{},all_line_ids=sched.active_line_ids,order_deadlines=sched.order_deadlines_minutes,order_release_minutes=sched.order_release_minutes,machine_pools=sched.factory.machine_ids_per_line_map,machine_line_map=sched.machine_line_map,usable_machine_count=sched.usable_machine_count,present_operator_count=capacity,op_sequence_map=sched.seq_lookup,initial_machine_free=free,initial_machine_last_order=last,op_optype=sched.factory.optype_for_op,protected_machine_type=sched.protected_machine_type,base_time=origin,operator_index=index,initial_operator_reservations=reserved,return_details=True)
finally:
    fitness.allocate_crew=original
for item,row in zip(trace,det['phenotype_operations']): item['operation_key']=row['operation_key']; item['order_id']=row['production_order_id']; item['operation_id']=row['operation_id']; item['line_id']=row['line_id']; item['machine_ids']=row['machine_ids']; item['optype']=sched.factory.optype_for_op[row['operation_key']]
target=[x for x in trace if '7648' in x['selected']]
segments=[]
for x in target:
    for a,b in x['segments']: segments.append((a,b,x))
segments.sort(); overlaps=[]
for left,right in zip(segments,segments[1:]):
    if right[0] < left[1]: overlaps.append({'left':{'op':left[2]['operation_key'],'segment':[left[0],left[1]]},'right':{'op':right[2]['operation_key'],'segment':[right[0],right[1]],'before':right[2]['before'],'after':right[2]['after'],'ready':right[2]['ready'],'line':right[2]['line_id'],'machines':right[2]['machine_ids'],'optype':right[2]['optype']}})
out={'fitness':fit[0],'phenotype_hash':det['phenotype_hash'],'worker':'7648','target_assignments':len(target),'overlaps':overlaps[:5],'trace_for_first_overlap':[x for x in target if x['operation_key'] in {'563_1583','991_1568'}]}
Path('outputs/scheduler_integration_seed42/worker_7648_allocation_trace.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
print(json.dumps(out,indent=2))