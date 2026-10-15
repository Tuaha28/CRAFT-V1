import json
import sys
sys.path.insert(0, '.')
from crafter.repair.engine import build_p2_state

s=build_p2_state(); idx=s['after_index']; key='991_1568'
base={x.assignment.operation_key:x for x in s['baseline'].operations}
op=s['problem'].operation_by_key[key]
same=sorted((x.operation_key,x.sequence) for x in s['problem'].operations if x.order_id==op.order_id)
pred=idx.route_predecessor.get(key); succ=idx.route_successor.get(key)
def row(k):
    item=base.get(k)
    return None if item is None else {'start':item.assignment.start_seconds,'end':item.assignment.end_seconds,'line':item.assignment.line_id,'machines':item.assignment.machine_ids,'workers':item.employee_ids}
print(json.dumps({'key':key,'sequence':op.sequence,'order_chain':same,'index_pred':pred,'index_succ':succ,'pred_baseline':row(pred),'self_baseline':row(key),'succ_baseline':row(succ)},indent=2,default=str))
