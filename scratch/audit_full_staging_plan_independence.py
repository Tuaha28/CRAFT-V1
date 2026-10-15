"""Full-database proof that actual RNG cannot change a frozen plan."""
import argparse, hashlib, json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mysql.connector
from data_generation.seed_database_fixed import db_config

def norm(v):
    if isinstance(v, (datetime, date, time)): return v.isoformat()
    if isinstance(v, Decimal): return format(v, "f")
    if isinstance(v, bytes): return v.decode()
    return v

def read(db):
    cfg=dict(db_config); cfg["database"]=db
    con=mysql.connector.connect(**cfg); cur=con.cursor(dictionary=True)
    try:
        cur.execute("""SELECT j.schedule_item_id,j.production_order_id,j.lot_index,
          r.operation_sequence_number,j.operation_id,o.factory_id,j.line_id,
          j.asset_id,j.employee_id,j.batch_id,j.lot_quantity,
          j.planned_start_time,j.planned_end_time,j.planned_work_segments,
          j.actual_start_time,j.actual_end_time,j.actual_work_segments,
          j.execution_status,j.queue_delay_mins_at_dispatch,
          j.machine_breakdown_count_at_dispatch,j.machine_status_at_dispatch,
          j.employee_on_leave_at_dispatch
          FROM Job_Shop_Schedules j
          JOIN Order_Factory_Ownership o USING(production_order_id)
          JOIN Routing_Operations r USING(operation_id)
          ORDER BY j.schedule_item_id""")
        rows=cur.fetchall()
        cur.execute("SELECT production_order_id,factory_id FROM Order_Factory_Ownership ORDER BY production_order_id")
        own=cur.fetchall()
        cur.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1")
        cal=json.loads(cur.fetchone()["payload"])
    finally:
        cur.close(); con.close()
    pf=("schedule_item_id","production_order_id","lot_index","operation_sequence_number",
        "operation_id","factory_id","line_id","asset_id","employee_id","batch_id",
        "lot_quantity","planned_start_time","planned_end_time","planned_work_segments")
    af=("schedule_item_id","actual_start_time","actual_end_time","actual_work_segments",
        "execution_status","queue_delay_mins_at_dispatch",
        "machine_breakdown_count_at_dispatch","machine_status_at_dispatch",
        "employee_on_leave_at_dispatch")
    project=lambda fields: [[norm(r[n]) for n in fields] for r in rows]
    return pf,af,project(pf),project(af),[[norm(r["production_order_id"]),norm(r["factory_id"])] for r in own],cal

def digest(v):
    return hashlib.sha256(json.dumps(v,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("first"); ap.add_argument("second")
    ap.add_argument("--output",default="outputs/integration_audit/full_staging_plan_independence.json")
    a=ap.parse_args(); x=read(a.first); y=read(a.second)
    assert len(x[2])==len(y[2])
    pd={n:sum(ra[i]!=rb[i] for ra,rb in zip(x[2],y[2])) for i,n in enumerate(x[0])}
    ad={n:sum(ra[i]!=rb[i] for ra,rb in zip(x[3],y[3])) for i,n in enumerate(x[1])}
    ignored={"dataset_version","source_database","generated_at","actual_execution_seed"}
    ce={k:x[5].get(k)==y[5].get(k) for k in sorted((set(x[5])|set(y[5]))-ignored)}
    result={"contract":"same full staging planning inputs/seed; only actual seed differs",
      "databases":[a.first,a.second],"planning_seeds":[x[5].get("planning_seed"),y[5].get("planning_seed")],
      "actual_execution_seeds":[x[5].get("actual_execution_seed"),y[5].get("actual_execution_seed")],
      "schedule_rows":len(x[2]),"plan_hashes":[digest(x[2]),digest(y[2])],
      "plan_hash_equal":x[2]==y[2],"plan_diff_by_field":pd,
      "ownership_hashes":[digest(x[4]),digest(y[4])],"ownership_equal":x[4]==y[4],
      "actual_hashes":[digest(x[3]),digest(y[3])],"actual_hash_different":x[3]!=y[3],
      "actual_diff_by_field":ad,"calibration_planning_inputs_equal":ce}
    assert result["planning_seeds"]==[42,42] and result["actual_execution_seeds"]==[42,999]
    assert result["plan_hash_equal"] and not any(pd.values()) and result["ownership_equal"]
    assert result["actual_hash_different"] and all(ce.values())
    p=Path(a.output); p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(result,indent=2),encoding="utf-8"); print(json.dumps(result,indent=2))
if __name__=="__main__": main()

