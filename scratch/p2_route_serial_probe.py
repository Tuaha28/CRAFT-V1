import sys
sys.path.insert(0,'.')
from crafter.repair.engine import run_p2_repair
r=run_p2_repair(); s=r['state']; regs=r['regimes']; idx=s['after_index']; keys=set(s['keys']); end={}; selected={};
for key in sorted(keys,key=lambda k:(idx.operation_features.get(k,{}).get('order_id',''),idx.operation_features.get(k,{}).get('sequence',0),k)):
 pred=idx.route_predecessor.get(key); bound=end.get(pred,0); found=None
 for reg in sorted(regs[key],key=lambda z:(z['lo'],z['hi'])):
  x=max(bound,reg['lo']);
  if x<=reg['hi']:
   e=reg['formula'][0][0]*x+reg['formula'][0][1]
   if e<=s['problem'].horizon_seconds: found=(x,e); break
 if found is None: print('NO_ROUTE_PLACEMENT',key,'pred',pred,'bound',bound); break
 selected[key]=found; end[key]=found[1]
else: print('ROUTE_SERIAL_FEASIBLE',len(selected),max(end.values()))
