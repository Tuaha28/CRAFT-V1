"""Causal/statistical audit of the corrected two-phase DGP delay shift."""
from __future__ import annotations
import json,sys
from collections import defaultdict
from datetime import datetime,timedelta
from pathlib import Path
import mysql.connector,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from data_generation.seed_database_fixed import db_config
from data_generation.simulation_calendar import work_on_calendar
DB="garment_fyp_db"; OUT=ROOT/"outputs/integration_audit/dgp_delay_sanity_audit.json"

def mins(a,b): return (b-a).total_seconds()/60.0
def parse_segments(raw):
    if not raw:return []
    return [(datetime.fromisoformat(a),datetime.fromisoformat(b)) for a,b in json.loads(raw)]
def duration(segs): return sum(mins(a,b) for a,b in segs)
def dist(v):
    a=np.asarray(list(v),dtype=float)
    if not len(a):return {"count":0}
    return {"count":int(len(a)),"min":float(a.min()),"q10":float(np.quantile(a,.1)),
      "q25":float(np.quantile(a,.25)),"median":float(np.median(a)),"mean":float(a.mean()),
      "q75":float(np.quantile(a,.75)),"q90":float(np.quantile(a,.9)),
      "q95":float(np.quantile(a,.95)),"q99":float(np.quantile(a,.99)),"max":float(a.max())}
def grouped(items,key):
    out={}
    for k,vals in items.items():
        n=len(vals);p=sum(v["positive"] for v in vals)
        out[str(k)]={"operations":n,"positive":p,"prevalence":p/n if n else None,
          "lateness_minutes":dist(v["lateness"] for v in vals),
          "queue_delay_minutes_decisive_lot":dist(v["queue"] for v in vals)}
    return out
def main():
    cfg=dict(db_config);cfg["database"]=DB;con=mysql.connector.connect(**cfg);cur=con.cursor(dictionary=True)
    cur.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1");cal=json.loads(cur.fetchone()["payload"])
    cur.execute("""SELECT j.*,p.status AS order_status,p.scheduled_start_date,o.factory_id,
      r.operation_sequence_number,mc.operation_type,b.rotation_offset,b.rest_day_offset,
      w.start_time,w.end_time
      FROM Job_Shop_Schedules j
      JOIN Production_Orders p USING(production_order_id)
      JOIN Order_Factory_Ownership o USING(production_order_id)
      JOIN Routing_Operations r USING(operation_id)
      JOIN Machine_Capabilities mc USING(asset_id)
      LEFT JOIN Employee_Batches b USING(batch_id)
      JOIN Work_Schedules w USING(employee_id)
      ORDER BY j.schedule_item_id""")
    rows=cur.fetchall();cur.close();con.close()
    base=datetime.fromisoformat(cal["reference_time"])-timedelta(days=15)
    active={"Pending","Processing","Delayed"}
    pred_p={};pred_a={};mach_p={};mach_a={};emp_p={};emp_a={}
    causal={};queue_res=[];end_res=[];segment_res=[];resource_winners=defaultdict(int)
    lot_components=[]; active_rows=[]
    for r in rows:
        key=(r["production_order_id"],r["lot_index"])
        crew=(r["rotation_offset"],r["rest_day_offset"]) if r["batch_id"] is not None else None
        band=None if crew is not None else ("stored",r["start_time"],r["end_time"])
        order_start=max(base,datetime.combine(r["scheduled_start_date"],datetime.min.time()))
        pp=pred_p.get(key,order_start)
        pa=pred_a.get(key,order_start)
        mp=mach_p.get(r["asset_id"],base); ma=mach_a.get(r["asset_id"],base)
        ep=emp_p.get(r["employee_id"],base); ea=emp_a.get(r["employee_id"],base)
        candidates={"planned_floor":r["planned_start_time"],"predecessor":pa,"machine":ma,"employee":ea}
        ready=max(candidates.values()); winners=sorted(k for k,v in candidates.items() if v==ready)
        for wnr in winners:resource_winners[wnr]+=1/len(winners)
        dispatch,_,_=work_on_calendar(ready,1,crew,band)
        expected_queue=mins(pa,dispatch)
        queue_res.append(expected_queue-float(r["queue_delay_mins_at_dispatch"] or 0))
        pseg=parse_segments(r["planned_work_segments"]);aseg=parse_segments(r["actual_work_segments"])
        pprod=duration(pseg);aprod=duration(aseg)
        replay_start,replay_end,replay_segments=work_on_calendar(r["actual_start_time"],aprod,crew,band)
        end_res.append(mins(replay_end,r["actual_end_time"]))
        segment_res.append(len(replay_segments)-len(aseg))
        pel=mins(r["planned_start_time"],r["planned_end_time"]);ael=mins(r["actual_start_time"],r["actual_end_time"])
        start_shift=mins(r["planned_start_time"],r["actual_start_time"])
        prod_delta=aprod-pprod;pause_delta=(ael-aprod)-(pel-pprod)
        lateness=mins(r["planned_end_time"],r["actual_end_time"])
        residual=lateness-start_shift-prod_delta-pause_delta
        pred_inherited=max(0.0,mins(pp,pa)) if r["operation_sequence_number"]>1 else 0.0
        dynamic=mins(dispatch,r["actual_start_time"])>1/60
        info={"row":r,"crew":crew,"band":band,"planned_prod":pprod,"actual_prod":aprod,
          "planned_pause":pel-pprod,"actual_pause":ael-aprod,"start_shift":start_shift,
          "prod_delta":prod_delta,"pause_delta":pause_delta,"lot_lateness":lateness,
          "residual":residual,"predecessor_actual_ready":pa,"predecessor_planned_ready":pp,
          "predecessor_inherited":pred_inherited,"ready":ready,"dispatch":dispatch,
          "queue":float(r["queue_delay_mins_at_dispatch"] or 0),"ready_winners":winners,
          "dynamic_breakdown_inferred":dynamic,"prior_breakdowns":int(r["machine_breakdown_count_at_dispatch"] or 0)}
        causal[r["schedule_item_id"]]=info;lot_components.append(info)
        if r["order_status"] in active:active_rows.append(r)
        pred_p[key]=r["planned_end_time"];pred_a[key]=r["actual_end_time"]
        mach_p[r["asset_id"]]=r["planned_end_time"];mach_a[r["asset_id"]]=r["actual_end_time"]
        emp_p[r["employee_id"]]=r["planned_end_time"];emp_a[r["employee_id"]]=r["actual_end_time"]
    op_rows=defaultdict(list)
    for r in active_rows:op_rows[(r["production_order_id"],r["operation_id"])].append(r)
    ops=[]
    for key,rr in op_rows.items():
        latest_actual=max(rr,key=lambda x:x["actual_end_time"]); ci=causal[latest_actual["schedule_item_id"]]
        latest_plan=max(x["planned_end_time"] for x in rr); late=mins(latest_plan,latest_actual["actual_end_time"])
        mean_plan=float(np.mean([mins(x["planned_start_time"],x["planned_end_time"]) for x in rr]))
        pos=late>.2*mean_plan
        ops.append({"order":key[0],"operation":key[1],"sequence":rr[0]["operation_sequence_number"],
          "factory":rr[0]["factory_id"],"operation_type":rr[0]["operation_type"],
          "planned_start":min(x["planned_start_time"] for x in rr),"planned_end":latest_plan,
          "actual_start":min(x["actual_start_time"] for x in rr),"actual_end":latest_actual["actual_end_time"],
          "lateness":late,"threshold":.2*mean_plan,"positive":bool(pos),
          "queue":ci["queue"],"decisive_schedule_item":latest_actual["schedule_item_id"],
          "decisive_lot_index":latest_actual["lot_index"],
          "decisive_lot_planned_start":latest_actual["planned_start_time"],
          "decisive_lot_planned_end":latest_actual["planned_end_time"],
          "decisive_lot_actual_start":latest_actual["actual_start_time"],
          "decisive_lot_actual_end":latest_actual["actual_end_time"],
          "predecessor_actual_ready":ci["predecessor_actual_ready"],
          "predecessor_inherited":ci["predecessor_inherited"],
          "planned_pause":ci["planned_pause"],"actual_pause":ci["actual_pause"],
          "prior_breakdowns":ci["prior_breakdowns"],"dynamic_breakdown_inferred":ci["dynamic_breakdown_inferred"],
          "ready_winners":ci["ready_winners"]})
    by_factory=defaultdict(list);by_type=defaultdict(list);by_month=defaultdict(list);by_week=defaultdict(list);by_order=defaultdict(list)
    for o in ops:
        by_factory[o["factory"]].append(o);by_type[o["operation_type"]].append(o);by_order[o["order"]].append(o)
        by_month[o["planned_start"].strftime("%Y-%m")].append(o)
        iso=o["planned_start"].isocalendar();by_week[f"{iso.year}-W{iso.week:02d}"].append(o)
    order_prev={}
    for oid,v in by_order.items():
        p=sum(x["positive"] for x in v)/len(v)
        order_prev[str(oid)]={"operations":len(v),"positive":sum(x["positive"] for x in v),"prevalence":p,
          "max_lateness_minutes":max(x["lateness"] for x in v),"max_queue_delay_minutes":max(x["queue"] for x in v)}
    positives=[o for o in ops if o["positive"]]
    inherited=[o for o in positives if o["predecessor_inherited"]>=.5*max(o["lateness"],1e-9)]
    categories={"positive_lte_60m":sum(o["positive"] and o["lateness"]<=60 for o in ops),
      "positive_60_to_480m":sum(o["positive"] and 60<o["lateness"]<=480 for o in ops),
      "positive_gt_480m":sum(o["positive"] and o["lateness"]>480 for o in ops)}
    unique_orders=sorted(by_order);rng=np.random.default_rng(42);perm=rng.permutation(len(unique_orders))
    val=set(np.asarray(unique_orders)[perm[:max(1,int(len(unique_orders)*.2))]].tolist())
    train=[o for o in ops if o["order"] not in val];tp=sum(o["positive"] for o in train);tn=len(train)-tp
    mostly=min(by_order,key=lambda x:sum(o["positive"] for o in by_order[x])/len(by_order[x]))
    moderate=min(by_order,key=lambda x:abs(sum(o["positive"] for o in by_order[x])/len(by_order[x])-.5))
    extreme=max(by_order,key=lambda x:max(o["lateness"] for o in by_order[x]))
    q99=float(np.quantile([c["queue"] for c in lot_components],.99))
    tail_candidates=[c for c in lot_components if c["queue"]>=q99]
    tail=max(tail_candidates,key=lambda c:c["queue"])["row"]["production_order_id"]
    chosen={"mostly_on_time":mostly,"moderately_delayed":moderate,"extremely_delayed":extreme,"queue_p99_tail":tail}
    traces={}
    for label,oid in chosen.items():
        traces[label]={"order":oid,"operation_positive_fraction":order_prev[str(oid)]["prevalence"],"operations":[]}
        for o in sorted(by_order[oid],key=lambda x:x["sequence"]):
            traces[label]["operations"].append({k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in o.items()})
    comp=np.asarray([[c["start_shift"],c["prod_delta"],c["pause_delta"],c["residual"]] for c in lot_components])
    positive_lot=np.asarray([c["lot_lateness"]>0 for c in lot_components])
    result={"database":DB,"dataset_version":cal["dataset_version"],"origin_database":cal.get("origin_database"),
      "label_definition":"max(actual_end)-max(planned_end) > 20% of mean lot planned elapsed duration, active orders only",
      "overall":{"operations":len(ops),"positive":len(positives),"negative":len(ops)-len(positives),"prevalence":len(positives)/len(ops),
        "lateness_minutes":dist(o["lateness"] for o in ops),"categories":categories},
      "by_factory":grouped(by_factory,None),"by_operation_type":grouped(by_type,None),
      "by_planned_start_month":grouped(by_month,None),"by_planned_start_week":grouped(by_week,None),
      "by_order":order_prev,"order_summary":{"orders":len(by_order),
        "all_operations_positive":sum(v["prevalence"]==1 for v in order_prev.values()),
        "at_least_90pct_positive":sum(v["prevalence"]>=.9 for v in order_prev.values()),
        "essentially_every_operation_definition":">=90% positive",
        "prevalence_distribution":dist(v["prevalence"] for v in order_prev.values())},
      "queue_by_factory":{k:dist(causal[r["schedule_item_id"]]["queue"] for r in active_rows if r["factory_id"]==k) for k in sorted(by_factory)},
      "queue_by_operation_type":{k:dist(causal[r["schedule_item_id"]]["queue"] for r in active_rows if r["operation_type"]==k) for k in sorted(by_type)},
      "additive_lot_lateness":{"identity":"actual_end-planned_end = start_shift + productive_delta + pause_delta",
        "rows":len(comp),"max_abs_residual_minutes":float(np.max(np.abs(comp[:,3]))),
        "component_distributions":{"start_shift":dist(comp[:,0]),"productive_delta":dist(comp[:,1]),"pause_delta":dist(comp[:,2])},
        "positive_lateness_positive_component_share":{"start_shift":float(np.maximum(comp[positive_lot,0],0).sum()/np.maximum(comp[positive_lot,:3],0).sum()),
          "productive_delta":float(np.maximum(comp[positive_lot,1],0).sum()/np.maximum(comp[positive_lot,:3],0).sum()),
          "pause_delta":float(np.maximum(comp[positive_lot,2],0).sum()/np.maximum(comp[positive_lot,:3],0).sum())}},
      "causal_indicators":{"resource_ready_winners":dict(resource_winners),
        "positive_operations_mostly_inherited_predecessor":len(inherited),
        "fraction_positive_operations_mostly_inherited_predecessor":len(inherited)/len(positives),
        "mostly_inherited_definition":"decisive lot predecessor actual-vs-planned readiness delta >= 50% of operation lateness",
        "positive_operations_decisive_prior_breakdown":sum(o["prior_breakdowns"]>0 for o in positives),
        "positive_operations_decisive_dynamic_breakdown":sum(o["dynamic_breakdown_inferred"] for o in positives),
        "rows_with_prior_breakdown":sum(c["prior_breakdowns"]>0 for c in lot_components),
        "rows_with_dynamic_breakdown_inferred":sum(c["dynamic_breakdown_inferred"] for c in lot_components),
        "employee_leave_rows":sum(bool(c["row"]["employee_on_leave_at_dispatch"]) for c in lot_components)},
      "recurrence_checks":{"queue_formula_max_abs_residual_minutes":float(np.max(np.abs(queue_res))),
        "queue_formula_rows_over_0_01m":int(np.sum(np.abs(queue_res)>.01)),
        "actual_completion_replay_max_abs_residual_minutes":float(np.max(np.abs(end_res))),
        "actual_completion_rows_over_0_01m":int(np.sum(np.abs(end_res)>.01)),
        "actual_segment_count_mismatches":int(np.sum(np.asarray(segment_res)!=0)),
        "actual_ready_uses_max_not_sum":True},
      "spans":{"operation_planned_completion_span_minutes":dist(mins(o["planned_start"],o["planned_end"]) for o in ops),
        "operation_actual_completion_span_minutes":dist(mins(o["actual_start"],o["actual_end"]) for o in ops),
        "order_planned_makespan_minutes":dist(mins(min(o["planned_start"] for o in v),max(o["planned_end"] for o in v)) for v in by_order.values()),
        "order_actual_makespan_minutes":dist(mins(min(o["actual_start"] for o in v),max(o["actual_end"] for o in v)) for v in by_order.values())},
      "ml_consequences":{"train_operations":len(train),"train_positive":tp,"train_negative":tn,
        "train_pos_weight_neg_over_pos":tn/tp,"majority_class_accuracy_baseline":max(len(positives),len(ops)-len(positives))/len(ops),
        "ap_no_skill_baseline":len(positives)/len(ops),"loss_contract":"BCEWithLogitsLoss pos_weight=train_negative/train_positive; valid for pos_weight<1 but downweights the majority-positive class"},
      "representative_order_traces":traces}
    OUT.write_text(json.dumps(result,indent=2,default=str),encoding="utf-8")
    print(json.dumps({"overall":result["overall"],"order_summary":result["order_summary"],
      "additive":result["additive_lot_lateness"],"causal":result["causal_indicators"],
      "recurrence":result["recurrence_checks"],"ml":result["ml_consequences"],
      "trace_orders":chosen},indent=2))
if __name__=="__main__":main()


