"""Read-only proof of disruption alternate-machine factory scope."""
import json, sys
from pathlib import Path
import pandas as pd
from sqlalchemy import text
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database.db import get_engine
from database.queries import fetch_machine_state
from utils.machine_reserve import build_reserve, reserve_pool_by_type
from utils.schedule_status import OCCUPYING_STATUSES

engine=get_engine()
status_sql=",".join("'"+s+"'" for s in OCCUPYING_STATUSES)
with engine.connect() as c:
    affected=pd.read_sql(text(f"""
      SELECT j.schedule_item_id,j.production_order_id,j.operation_id,j.asset_id,
             j.line_id AS schedule_line_id, ma.factory_id AS broken_factory_id,
             ofo.factory_id AS owned_factory_id, mc.operation_type
      FROM Job_Shop_Schedules j
      JOIN Machinery_Assets ma ON ma.asset_id=j.asset_id
      JOIN Order_Factory_Ownership ofo
        ON ofo.production_order_id=CAST(j.production_order_id AS CHAR)
      JOIN Machine_Capabilities mc ON mc.asset_id=j.asset_id
      WHERE j.execution_status IN ({status_sql})
    """),c)
    active=pd.read_sql(text("""
      SELECT ma.asset_id,ma.line_id,ma.factory_id,mc.operation_type
      FROM Machinery_Assets ma JOIN Machine_Capabilities mc ON mc.asset_id=ma.asset_id
      WHERE ma.current_status='Active'
    """),c)

groups=[]
external_pairs=local_pairs=rows_with_external=rows_without_local=0
for (fac,opt),grp in affected.groupby(["owned_factory_id","operation_type"]):
    candidates=active[active.operation_type==opt]
    local=candidates[candidates.factory_id==fac]
    external=candidates[candidates.factory_id!=fac]
    n=len(grp)
    local_pairs += n*len(local)
    external_pairs += n*len(external)
    rows_with_external += n if len(external) else 0
    rows_without_local += n if not len(local) else 0
    groups.append({"factory_id":int(fac),"operation_type":str(opt),
                   "affected_rows":n,"local_active_candidates":len(local),
                   "external_active_candidates":len(external)})
machines=fetch_machine_state()
reserved,_=build_reserve(machines)
pools=reserve_pool_by_type(machines,reserved)
machine_factory={int(r.asset_id):int(r.factory_id) for r in machines.dropna(subset=["factory_id"]).itertuples()}
reserve_external=0
reserve_total=0
for r in affected.itertuples():
    ids=pools.get(str(r.operation_type).strip().lower(),[])
    reserve_total += len(ids)
    reserve_external += sum(machine_factory.get(int(a))!=int(r.owned_factory_id) for a in ids)
out={
 "measurement":"candidate machine factory_id versus Order_Factory_Ownership.factory_id",
 "occupying_rows":len(affected),
 "broken_machine_factory_mismatches":int((affected.broken_factory_id!=affected.owned_factory_id).sum()),
 "schedule_line_factory_not_measured_here":"covered by identifier_joins.json",
 "rows_with_companywide_external_active_candidates":rows_with_external,
 "rows_without_local_active_candidate":rows_without_local,
 "local_candidate_pairs":local_pairs,
 "external_candidate_pairs_exposed_by_current_query":external_pairs,
 "standby_candidate_pairs_total":reserve_total,
 "standby_external_candidate_pairs_exposed":reserve_external,
 "groups":groups,
 "commit_columns":["asset_id","planned_start_time","planned_end_time"],
 "line_id_updated_on_patch":False,
}
path=Path("outputs/integration_audit/disruption_factory_scope.json")
path.write_text(json.dumps(out,indent=2)+"\n",encoding="utf-8")
print(json.dumps(out,indent=2))
