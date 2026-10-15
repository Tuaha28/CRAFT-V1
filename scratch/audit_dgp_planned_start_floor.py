"""Measure the causal effect of treating planned start as a hard actual-start floor."""
import json,random,sys
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
import mysql.connector,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from data_generation.seed_database_fixed import db_config,sample_execution_variance_multiplier
from data_generation.simulation_calendar import work_on_calendar
DB="garment_fyp_db";OUT=ROOT/"outputs/integration_audit/dgp_planned_start_floor_counterfactual.json"
def pm(raw):
    return sum((datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()/60 for a,b in json.loads(raw))
def simulate(rows,base,use_floor):
    rng=random.Random(42);mf={};ef={};pred={};runtime={};bc={};out={};meta={}
    for r in rows:
        aid=r["asset_id"];eid=r["employee_id"];key=(r["production_order_id"],r["lot_index"])
        prev=pred.get(key,r["order_start"]); candidates=[mf.get(aid,base),ef.get(eid,base),prev]
        if use_floor:candidates.append(r["planned_start_exact"])
        ready=max(candidates); crew=r["crew"];band=r["band"]
        dispatch,_,_=work_on_calendar(ready,1,crew,band)
        queue=max(0,(dispatch-prev).total_seconds()/60)
        prior=bc.get(aid,0);rt=runtime.get(aid,0)+r["duration"]
        dynamic=rt>1200 and rng.random()<.15;repair=rng.randint(120,360) if dynamic else 0
        if dynamic:rt=0;bc[aid]=prior+1
        else:bc.setdefault(aid,prior)
        runtime[aid]=rt
        multiplier=sample_execution_variance_multiplier(rng=rng,machine_breakdown_count_at_dispatch=prior,on_leave=False,queue_delay_mins=queue)
        start,end,segs=work_on_calendar(dispatch+timedelta(minutes=repair),r["duration"]*multiplier,crew,band)
        out[r["schedule_item_id"]]=(start,end)
        meta[r["schedule_item_id"]]={"queue":queue,"dynamic":dynamic,"repair":repair,"multiplier":multiplier}
        mf[aid]=end;ef[eid]=end;pred[key]=end
    return out,meta
def labels(rows,ends):
    grp=defaultdict(list)
    for r in rows:
        if r["order_status"] in {"Pending","Processing","Delayed"}:grp[(r["production_order_id"],r["operation_id"])].append(r)
    vals=[]
    for key,rr in grp.items():
        latest=max(ends[r["schedule_item_id"]][1] for r in rr)
        planned=max(r["planned_end_time"] for r in rr)
        mean=np.mean([(r["planned_end_time"]-r["planned_start_time"]).total_seconds()/60 for r in rr])
        late=(latest-planned).total_seconds()/60
        vals.append((key,late,late>.2*mean))
    return vals
def summary(vals):
    a=np.asarray([x[1] for x in vals]);p=int(sum(x[2] for x in vals))
    return {"operations":len(vals),"positive":p,"negative":len(vals)-p,"prevalence":p/len(vals),
      "lateness_median_minutes":float(np.median(a)),"lateness_q90_minutes":float(np.quantile(a,.9)),
      "lateness_q99_minutes":float(np.quantile(a,.99)),"lateness_max_minutes":float(a.max())}
def main():
    cfg=dict(db_config);cfg["database"]=DB;con=mysql.connector.connect(**cfg);cur=con.cursor(dictionary=True)
    cur.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1");cal=json.loads(cur.fetchone()["payload"]);base=datetime.fromisoformat(cal["reference_time"])-timedelta(days=15);factor=float(cal["duration_calibration_factor"])
    cur.execute("""SELECT j.schedule_item_id,j.production_order_id,j.lot_index,j.operation_id,j.asset_id,j.employee_id,j.batch_id,
      j.planned_start_time,j.planned_end_time,j.planned_work_segments,j.lot_quantity,r.standard_allowed_minutes,s.efficiency_rating,j.actual_start_time,j.actual_end_time,
      p.status order_status,p.scheduled_start_date,b.rotation_offset,b.rest_day_offset,w.start_time,w.end_time
      FROM Job_Shop_Schedules j JOIN Production_Orders p USING(production_order_id)
      JOIN Routing_Operations r ON r.operation_id=j.operation_id
      JOIN Machine_Capabilities mc ON mc.asset_id=j.asset_id
      JOIN Operator_Skill_Matrix s ON s.employee_id=j.employee_id AND s.operation_type=mc.operation_type
      LEFT JOIN Employee_Batches b ON b.batch_id=j.batch_id JOIN Work_Schedules w ON w.employee_id=j.employee_id
      ORDER BY j.schedule_item_id""")
    rows=cur.fetchall();cur.close();con.close()
    for r in rows:
        psegs=json.loads(r["planned_work_segments"]);r["planned_start_exact"]=datetime.fromisoformat(psegs[0][0]);r["duration"]=factor*float(r["lot_quantity"])*float(r["standard_allowed_minutes"])/(float(r["efficiency_rating"])/100.0);r["order_start"]=max(base,datetime.combine(r["scheduled_start_date"],datetime.min.time()))
        r["crew"]=(r["rotation_offset"],r["rest_day_offset"]) if r["batch_id"] is not None else None
        r["band"]=None if r["crew"] is not None else ("stored",r["start_time"],r["end_time"])
    current,cm=simulate(rows,base,True);nofloor,nm=simulate(rows,base,False)
    end_err=[abs((current[r["schedule_item_id"]][1]-r["actual_end_time"]).total_seconds()) for r in rows]
    start_err=[abs((current[r["schedule_item_id"]][0]-r["actual_start_time"]).total_seconds()) for r in rows]
    work_err=[abs(cm[r["schedule_item_id"]]["multiplier"]*r["duration"]-pm(json.dumps([[a.isoformat(),b.isoformat()] for a,b in []]))) for r in []]
    a=labels(rows,current);b=labels(rows,nofloor)
    amap={k:(late,pos) for k,late,pos in a};bmap={k:(late,pos) for k,late,pos in b}
    result={"database":DB,"actual_seed":42,
      "current_hard_planned_start_floor":summary(a),"counterfactual_release_and_resource_floors_only":summary(b),
      "positive_labels_removed_without_floor":int(sum(bool(amap[k][1]) and not bool(bmap[k][1]) for k in amap)),
      "positive_labels_added_without_floor":int(sum(not bool(amap[k][1]) and bool(bmap[k][1]) for k in amap)),
      "current_replay":{"max_start_error_seconds":max(start_err),"p99_start_error_seconds":float(np.quantile(start_err,.99)),
        "max_end_error_seconds":max(end_err),"p99_end_error_seconds":float(np.quantile(end_err,.99)),
        "rows_start_error_gt_2s":sum(x>2 for x in start_err),"rows_end_error_gt_2s":sum(x>2 for x in end_err)},
      "interpretation_scope":"Counterfactual preserves frozen resource identities, order releases, RNG call sequence, breakdown rolls/repairs, worker calendars, and the queue-dependent variance formula; only plan.planned_start_time is omitted from actual readiness."}
    OUT.write_text(json.dumps(result,indent=2),encoding="utf-8");print(json.dumps(result,indent=2))
if __name__=="__main__":main()



