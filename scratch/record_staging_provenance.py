"""Persist authoritative staging provenance and ownership inputs."""
import json,sys
from pathlib import Path
import mysql.connector,pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from data_generation.seed_database_fixed import db_config
DB="garment_fyp_db_seed_v2_20260914_142905"
manifest=json.loads((ROOT/"outputs"/"seeder_v2"/DB/"manifest.json").read_text())
validation=json.loads((ROOT/"outputs"/"seeder_v2"/DB/"validation.json").read_text())
cfg=dict(db_config);cfg["database"]=DB
con=mysql.connector.connect(**cfg);cur=con.cursor(dictionary=True)
cur.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1");cal=json.loads(cur.fetchone()["payload"])
cur.execute("SELECT f.factory_id,f.name,COUNT(o.production_order_id) orders FROM Factories f LEFT JOIN Order_Factory_Ownership o ON o.factory_id=f.factory_id GROUP BY f.factory_id,f.name ORDER BY f.factory_id")
owned=cur.fetchall();cur.close();con.close()
ws=pd.read_excel(ROOT/"data_generation"/"WorkStation.xlsx")
grp=ws.groupby("Department").agg(lines=("LineName","nunique"),worker_capacity=("Worker Capacity","sum")).reset_index()
total=float(grp.worker_capacity.sum())
inputs=[{"department":r.Department,"lines":int(r.lines),"worker_capacity":float(r.worker_capacity),"capacity_weight":float(r.worker_capacity/total)} for r in grp.itertuples()]
result={"database":DB,"dataset_version":cal["dataset_version"],"manifest":manifest,
 "calibration":cal,"ownership_policy":"capacity-weighted among factories feasible for the order's complete routing",
 "ownership_weight_source":"sum WorkStation.xlsx Worker Capacity by Department; conditional routing feasibility means final shares need not equal unconditional weights",
 "ownership_generation_inputs":inputs,"ownership_result":owned,"validation":validation}
p=ROOT/"outputs"/"integration_audit"/"authoritative_staging_provenance.json"
p.write_text(json.dumps(result,indent=2,default=str),encoding="utf-8")
print(json.dumps({"database":DB,"dataset_version":cal["dataset_version"],"ownership_inputs":inputs,"ownership_result":owned,"valid":validation["valid"]},indent=2))
