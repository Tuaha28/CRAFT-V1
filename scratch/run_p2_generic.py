"""P2 shared-core regression via canonical run_event_repair acceptance."""
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,'.')
from crafter.repair.engine import build_p2_state, run_event_repair

OUT=Path('outputs/crafter/deterministic_core_p2_generic_v1'); OUT.mkdir(parents=True,exist_ok=True)
def write(name,value): (OUT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')
def main():
    started=time.perf_counter(); state=build_p2_state()
    result=run_event_repair(state=state,max_closure_iterations=30,max_local_operations=150,
        max_machine_sets=3,max_worker_crews=3,progress=lambda x:print(json.dumps(x),flush=True))
    acc=result['acceptance']; final='CRAFT_P2_REPAIR_SOLVED' if acc['status']=='ACCEPTED' else 'CRAFT_P2_GENERIC_PARITY_FAILED'
    payload={'status':final,'event':result['state']['event'].as_dict(),'initial_region':list(result['initial_keys']),
      'final_region':list(result['state']['keys']),'closure':result['closure'],
      'resource_configuration_count':sum(len(x) for x in result['resource_configurations'].values()),
      'calendar_regime_count':result['regime_count'],'calendar_equivalence_mismatches':result['calendar_equivalence']['mismatches'],
      'timing':{**result.get('timing',{}),**acc.get('timing',{}),'total_wall_seconds':time.perf_counter()-started},
      'acceptance':acc,'database_writes':0}
    write('p2_generic_result.json',payload)
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.rglob('*') if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'status':final,'files':files,'database_writes':0})
    print(json.dumps({'status':final,'final_region':len(payload['final_region']),'winning_stage':acc['winning_stage'],
      'materialization':acc['materialization']['mismatch_count'],'replay':acc['replay']['status'],
      'validator':acc['validator']['status'],'event_violations':len(acc['event_violations']),
      'factory_isolation':acc['factory_isolation']['violation_count'],'database_writes':0},indent=2),flush=True)
if __name__=='__main__': main()
