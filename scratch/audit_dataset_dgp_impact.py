"""Old-versus-corrected-DGP row-level impact profile."""
import argparse, json, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import mysql.connector, numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from data_generation.seed_database_fixed import db_config

def dist(values):
    a=np.asarray(list(values),dtype=float)
    if not len(a): return {"count":0}
    return {"count":int(len(a)),"min":float(a.min()),"q10":float(np.quantile(a,.10)),
      "q25":float(np.quantile(a,.25)),"median":float(np.median(a)),"mean":float(a.mean()),
      "q75":float(np.quantile(a,.75)),"q90":float(np.quantile(a,.90)),
      "q99":float(np.quantile(a,.99)),"max":float(a.max()),"sum":float(a.sum())}

def segments_minutes(raw):
    if not raw: return 0.0
    rows=json.loads(raw) if isinstance(raw,str) else raw
    total=0.0
    for start,end in rows:
        total+=(datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()/60
    return total

def profile(db):
    cfg=dict(db_config);cfg["database"]=db
    con=mysql.connector.connect(**cfg);cur=con.cursor(dictionary=True)
    try:
        counts={}
        for name,sql in {
          "orders":"SELECT COUNT(*) n FROM Production_Orders",
          "routing_operations":"SELECT COUNT(*) n FROM Routing_Operations",
          "distinct_order_operations":"SELECT COUNT(*) n FROM (SELECT DISTINCT production_order_id,operation_id FROM Job_Shop_Schedules) x",
          "schedule_rows":"SELECT COUNT(*) n FROM Job_Shop_Schedules",
          "machines":"SELECT COUNT(*) n FROM Machinery_Assets",
          "employees":"SELECT COUNT(*) n FROM Employees"}.items():
            cur.execute(sql);counts[name]=int(cur.fetchone()["n"])
        cur.execute("SELECT o.factory_id,COUNT(*) n FROM Order_Factory_Ownership o GROUP BY o.factory_id ORDER BY o.factory_id")
        factory={str(r["factory_id"]):int(r["n"]) for r in cur.fetchall()}
        cur.execute("""SELECT asset_id,employee_id,planned_start_time,planned_end_time,
          actual_start_time,actual_end_time,planned_work_segments,actual_work_segments,
          queue_delay_mins_at_dispatch FROM Job_Shop_Schedules ORDER BY schedule_item_id""")
        rows=cur.fetchall()
        cur.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1")
        cal=json.loads(cur.fetchone()["payload"])
    finally:
        cur.close();con.close()
    pp=[];pe=[];ap=[];ae=[];queue=[];mp=defaultdict(float);ma=defaultdict(float);wp=defaultdict(float);wa=defaultdict(float)
    pmin=min(r["planned_start_time"] for r in rows);pmax=max(r["planned_end_time"] for r in rows)
    amin=min(r["actual_start_time"] for r in rows if r["actual_start_time"]);amax=max(r["actual_end_time"] for r in rows if r["actual_end_time"])
    for r in rows:
        pprod=segments_minutes(r["planned_work_segments"]); pel=(r["planned_end_time"]-r["planned_start_time"]).total_seconds()/60
        aprod=segments_minutes(r["actual_work_segments"]); ael=(r["actual_end_time"]-r["actual_start_time"]).total_seconds()/60
        pp.append(pprod);pe.append(pel);ap.append(aprod);ae.append(ael);queue.append(float(r["queue_delay_mins_at_dispatch"] or 0))
        mp[r["asset_id"]]+=pel;ma[r["asset_id"]]+=ael;wp[r["employee_id"]]+=pprod;wa[r["employee_id"]]+=aprod
    pspan=(pmax-pmin).total_seconds()/60;aspan=(amax-amin).total_seconds()/60
    machine_ids=range(1,counts["machines"]+1);employee_ids=range(1,counts["employees"]+1)
    p_pause=np.asarray(pe)-np.asarray(pp);a_pause=np.asarray(ae)-np.asarray(ap)
    return {"database":db,"dataset_version":cal.get("dataset_version"),"planning_seed":cal.get("planning_seed"),
      "actual_execution_seed":cal.get("actual_execution_seed"),"reference_time":cal.get("reference_time"),
      "duration_calibration_factor":cal.get("duration_calibration_factor"),"calibration":cal,
      "counts":counts,"factory_ownership":{"counts":factory,"proportions":{k:v/counts["orders"] for k,v in factory.items()}},
      "planned_lot_productive_minutes":dist(pp),"planned_lot_elapsed_minutes":dist(pe),
      "actual_lot_productive_minutes":dist(ap),"actual_lot_elapsed_minutes":dist(ae),
      "queue_delay_minutes":dist(queue),"planned_calendar_pause_minutes":dist(p_pause),
      "actual_calendar_pause_minutes":dist(a_pause),
      "calendar_totals":{"planned_productive_minutes":float(sum(pp)),"planned_elapsed_minutes":float(sum(pe)),
        "planned_pause_fraction_of_elapsed":float(p_pause.sum()/sum(pe)),
        "actual_productive_minutes":float(sum(ap)),"actual_elapsed_minutes":float(sum(ae)),
        "actual_pause_fraction_of_elapsed":float(a_pause.sum()/sum(ae))},
      "horizons":{"planned_start":pmin.isoformat(),"planned_end":pmax.isoformat(),"planned_minutes":pspan,
        "actual_start":amin.isoformat(),"actual_end":amax.isoformat(),"actual_minutes":aspan},
      "machine_planned_occupied_load_minutes":dist(mp[i] for i in machine_ids),
      "machine_actual_occupied_load_minutes":dist(ma[i] for i in machine_ids),
      "machine_planned_utilization_over_global_horizon":dist(mp[i]/pspan for i in machine_ids),
      "machine_actual_utilization_over_global_horizon":dist(ma[i]/aspan for i in machine_ids),
      "worker_planned_productive_load_minutes":dist(wp[i] for i in employee_ids),
      "worker_actual_productive_load_minutes":dist(wa[i] for i in employee_ids),
      "worker_planned_utilization_over_global_horizon":dist(wp[i]/pspan for i in employee_ids),
      "worker_actual_utilization_over_global_horizon":dist(wa[i]/aspan for i in employee_ids)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("old");ap.add_argument("new");ap.add_argument("--output",required=True);a=ap.parse_args()
    old=profile(a.old);new=profile(a.new)
    og=json.loads((ROOT/"outputs/integration_audit/graph_profile_old_live.json").read_text())
    ng=json.loads((ROOT/"outputs/integration_audit/graph_profile_new_staging.json").read_text())
    result={"definitions":{"machine_load":"sum of elapsed operation occupancy (planned_end-start or actual_end-start), including pauses",
      "worker_load":"sum of explicit productive worker segments; pauses excluded",
      "utilization":"resource load divided by the database-wide corresponding schedule horizon",
      "delay_label":"operation-level max(actual_end)-max(planned_end)>0, exactly graph.labels",
      "completion_target":"operation-level max(planned_end)-min(planned_start), exactly graph.labels",
      "priority":"train-cutpoint-derived label emitted by production graph build"},
      "old":old,"new":new,"graph":{"old":og,"new":ng}}
    p=Path(a.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(result,indent=2,default=str),encoding="utf-8")
    summary={"old_database":a.old,"new_database":a.new,"counts":{"old":old["counts"],"new":new["counts"]},
      "delay_prevalence":{"old":og["delay"]["prevalence"],"new":ng["delay"]["prevalence"]},
      "queue_median":{"old":old["queue_delay_minutes"]["median"],"new":new["queue_delay_minutes"]["median"]},
      "queue_p99":{"old":old["queue_delay_minutes"]["q99"],"new":new["queue_delay_minutes"]["q99"]},
      "completion_median":{"old":og["completion"]["distribution_minutes"]["median"],"new":ng["completion"]["distribution_minutes"]["median"]},
      "operation_nodes":{"old":og["nodes"]["operation"],"new":ng["nodes"]["operation"]},
      "contention_edges":{"old":og["contention_edges"],"new":ng["contention_edges"]}}
    print(json.dumps(summary,indent=2))
if __name__=="__main__":main()
