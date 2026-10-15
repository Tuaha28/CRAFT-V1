"""Run authoritative M2 through canonical run_event_repair acceptance."""
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0,'.')
from crafter.disruptions.benchmark import _load_state
from crafter.disruptions.v2_execution_diagnostics import _event_from_row
from crafter.repair.engine import _build_event_state_from_loaded, run_event_repair
from crafter.repair.event_semantics import with_m2_capacity_contract

OUT=Path('outputs/crafter/deterministic_core_m2_v2'); OUT.mkdir(parents=True,exist_ok=True)
def write(name,value): (OUT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')

def main():
    loaded=_load_state(); rows=loaded[2]; by={str(x['operation_key']):x for x in rows}
    key='122_1061'
    raw=_event_from_row(by[key],'M2_K_REDUCTION',duration_seconds=1800)
    event=replace(raw,metadata={**raw.metadata,'repair_semantics':'M2_CAPACITY_REDUCTION'})
    event=with_m2_capacity_contract(loaded,event)
    state=_build_event_state_from_loaded(loaded,event)
    result=run_event_repair(state=state,max_closure_iterations=30,max_local_operations=150,
        max_machine_sets=3,max_worker_crews=3,
        progress=lambda x: print(json.dumps(x),flush=True))
    acc=result['acceptance']; summary={
        'status':'CRAFT_M2_REPAIR_SOLVED' if acc['status']=='ACCEPTED' else 'CRAFT_M2_REPAIR_FAILED',
        'event':event.as_dict(),'initial_region':list(result['initial_keys']),
        'final_region':list(result['state']['keys']),'closure':result['closure'],
        'resource_configuration_count':sum(len(x) for x in result['resource_configurations'].values()),
        'resource_audit':result['resource_audit'],'calendar_regime_count':result['regime_count'],
        'calendar_equivalence_mismatches':result['calendar_equivalence']['mismatches'],
        'timing':{**result.get('timing',{}),**acc.get('timing',{})},
        'regime_generation_by_operation':result['regime_summary'],
        'acceptance':acc,'database_writes':0}
    write('m2_result.json',summary)
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in OUT.rglob('*') if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'status':summary['status'],'files':files,'database_writes':0})
    print(json.dumps({'status':summary['status'],'initial_region':len(summary['initial_region']),
        'final_region':len(summary['final_region']),'configurations':summary['resource_configuration_count'],
        'regimes':summary['calendar_regime_count'],'winning_stage':acc['winning_stage'],
        'materialization_mismatches':acc['materialization']['mismatch_count'],
        'replay':acc['replay']['status'],'validator':acc['validator']['status'],
        'factory_isolation_violations':acc['factory_isolation']['violation_count'],
        'event_violations':len(acc['event_violations']),'database_writes':0},indent=2),flush=True)

if __name__=='__main__': main()
