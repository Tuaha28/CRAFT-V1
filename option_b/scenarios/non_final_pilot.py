"""NON-FINAL, in-memory Option-B training-pilot scenario only."""
from datetime import datetime,timedelta
import pandas as pd
from ga.scheduler import GAScheduler
NOW=datetime(2026,1,1,8)
def build(seed=42):
 rows=[(1001,1,1,2.,40,20,'single-needle lockstitch'),(1001,2,2,1.5,40,20,'overlock serger (4-thread)'),(1002,1,1,2.5,30,10,'single-needle lockstitch'),(1002,2,2,1.,30,10,'overlock serger (4-thread)'),(1003,1,1,1.8,25,15,'single-needle lockstitch'),(1003,2,2,1.2,25,15,'overlock serger (4-thread)')]
 r=pd.DataFrame([{'production_order_id':o,'operation_id':i,'operation_sequence_number':q,'sam':sam,'target_quantity':qty,'order_deadline':NOW+timedelta(days=d),'required_machine_type':m} for o,i,q,sam,qty,d,m in rows])
 machines=pd.DataFrame([{'asset_id':a,'line_id':1 if j<3 else 2,'operation_type':'body assembly'} for j,a in enumerate(range(101,107))]+[{'asset_id':a,'line_id':1 if j<3 else 2,'operation_type':'seam edge overlock'} for j,a in enumerate(range(201,207))])
 lines=pd.DataFrame({'line_id':[1,2],'is_active':[1,1],'target_efficiency_percentage':[85.,85.]})
 return GAScheduler(r,machines,lines,{},current_time=NOW,present_operator_count=10,legacy_mode='legacy_company_wide')
