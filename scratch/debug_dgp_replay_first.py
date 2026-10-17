"""Locate the first divergence between stored phase-B outcomes and a replay."""
import json, random, sys
from datetime import datetime, timedelta
from pathlib import Path
import mysql.connector
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from data_generation.seed_database_fixed import db_config,sample_execution_variance_multiplier
from data_generation.simulation_calendar import work_on_calendar

def mins(raw):
    return sum((datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()/60 for a,b in json.loads(raw))

def main():
    cfg=dict(db_config); cfg['database']='garment_fyp_db'; con=mysql.connector.connect(**cfg); cur=con.cursor(dictionary=True)
    cur.execute('SELECT payload FROM Dataset_Calibration WHERE singleton_id=1'); cal=json.loads(cur.fetchone()['payload'])
    base=datetime.fromisoformat(cal['reference_time'])-timedelta(days=15); seed=int(cal['actual_execution_seed']); factor=float(cal['duration_calibration_factor'])
    cur.execute('''SELECT j.schedule_item_id,j.production_order_id,j.lot_index,j.operation_id,j.asset_id,j.employee_id,j.batch_id,j.lot_quantity,j.planned_start_time,j.planned_work_segments,r.standard_allowed_minutes,s.efficiency_rating,j.actual_start_time,j.actual_end_time,j.actual_work_segments,j.queue_delay_mins_at_dispatch,j.machine_breakdown_count_at_dispatch,p.scheduled_start_date,b.rotation_offset,b.rest_day_offset,w.start_time,w.end_time FROM Job_Shop_Schedules j JOIN Production_Orders p USING(production_order_id) JOIN Routing_Operations r ON r.operation_id=j.operation_id JOIN Machine_Capabilities mc ON mc.asset_id=j.asset_id JOIN Operator_Skill_Matrix s ON s.employee_id=j.employee_id AND s.operation_type=mc.operation_type LEFT JOIN Employee_Batches b ON b.batch_id=j.batch_id JOIN Work_Schedules w ON w.employee_id=j.employee_id ORDER BY j.schedule_item_id''')
    rows=cur.fetchall(); cur.close(); con.close(); rng=random.Random(seed); mf={};ef={};pred={};runtime={};bc={}
    result={'actual_seed':seed,'rows':len(rows),'first_divergence':None}
    for i,r in enumerate(rows):
        aid,eid=r['asset_id'],r['employee_id']; key=(r['production_order_id'],r['lot_index'])
        order_start=max(base,datetime.combine(r['scheduled_start_date'],datetime.min.time())); previous=pred.get(key,order_start)
        ready=max(mf.get(aid,base),ef.get(eid,base),previous,r['planned_start_time'])
        crew=(r['rotation_offset'],r['rest_day_offset']) if r['batch_id'] is not None else None
        band=None if crew is not None else ('stored',r['start_time'],r['end_time'])
        dispatch,_,_=work_on_calendar(ready,1,crew,band); queue=max(0,(dispatch-previous).total_seconds()/60)
        work=factor*float(r['lot_quantity'])*float(r['standard_allowed_minutes'])/(float(r['efficiency_rating'])/100.0); prior=bc.get(aid,0); rt=runtime.get(aid,0)+work
        dynamic=rt>1200 and rng.random()<.15; repair=rng.randint(120,360) if dynamic else 0
        if dynamic: rt=0; bc[aid]=prior+1
        else: bc.setdefault(aid,prior)
        runtime[aid]=rt
        mult=sample_execution_variance_multiplier(rng=rng,machine_breakdown_count_at_dispatch=prior,on_leave=False,queue_delay_mins=queue)
        start,end,_=work_on_calendar(dispatch+timedelta(minutes=repair),work*mult,crew,band); mf[aid]=end;ef[eid]=end;pred[key]=end
        stored_work=mins(r['actual_work_segments']); errors={'start_error_seconds':(start-r['actual_start_time']).total_seconds(),'end_error_seconds':(end-r['actual_end_time']).total_seconds(),'queue_error_seconds':(queue-float(r['queue_delay_mins_at_dispatch']))*60,'work_error_seconds':(work*mult-stored_work)*60}
        if max(abs(errors['start_error_seconds']),abs(errors['end_error_seconds']))>1.1:
            result['first_divergence']={'index':i,'schedule_item_id':r['schedule_item_id'],'order_id':r['production_order_id'],'lot_index':r['lot_index'],'operation_id':r['operation_id'],'asset_id':aid,'employee_id':eid,'planned_work_minutes':work,'stored_actual_work_minutes':stored_work,'replay_actual_work_minutes':work*mult,'multiplier':mult,'prior_breakdowns_replay':prior,'prior_breakdowns_stored':r['machine_breakdown_count_at_dispatch'],'dynamic_breakdown_replay':dynamic,'repair_minutes_replay':repair,'queue_replay':queue,'queue_stored':float(r['queue_delay_mins_at_dispatch']),'start_replay':start.isoformat(),'start_stored':r['actual_start_time'].isoformat(),'end_replay':end.isoformat(),'end_stored':r['actual_end_time'].isoformat(),**errors}; break
    out=ROOT/'outputs/integration_audit/dgp_replay_first_divergence.json';out.write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result,indent=2))
if __name__=='__main__': main()



