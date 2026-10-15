"""Run one named benchmark disruption through canonical CRAFT acceptance."""
import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0,'.')
from crafter.disruptions.benchmark import _load_state
from crafter.disruptions.v2_execution_diagnostics import _event_from_row
from crafter.repair.engine import _build_event_state_from_loaded, run_event_repair


def _event(loaded,name):
    rows=loaded[2]; by={str(x['operation_key']):x for x in rows}; machine='122_1061'
    worker='1029_292'
    if name=='M3':
        raw=_event_from_row(by[machine],'M3_TEMPORAL_RECOVERY',duration_seconds=1800)
        return replace(raw,metadata={**raw.metadata,'repair_semantics':'M3_TEMPORARY_MACHINE_OUTAGE'})
    if name=='W2':
        raw=_event_from_row(by[worker],'W2_BORROWED_WORKER',duration_seconds=28800,worker=True)
        return replace(raw,metadata={**raw.metadata,'repair_semantics':'W2_WORKER_ABSENCE'})
    if name=='M4':
        raw=_event_from_row(by[machine],'M4_CROSS_LINE',duration_seconds=0)
        return replace(raw,metadata={**raw.metadata,'repair_semantics':'M4_CROSS_LINE_RECOVERY'})
    raise ValueError(name)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('scenario',choices=('M3','W2','M4'))
    parser.add_argument('--suffix',default='v1'); args=parser.parse_args(); name=args.scenario
    out=Path(f'outputs/crafter/deterministic_core_{name.lower()}_{args.suffix}')
    if out.exists() and any(out.iterdir()): raise RuntimeError(f'refusing nonempty immutable output: {out}')
    out.mkdir(parents=True); write=lambda n,v:(out/n).write_text(json.dumps(v,indent=2,default=str),encoding='utf-8')
    started=time.perf_counter(); loaded=_load_state(); event=_event(loaded,name)
    prepared=time.perf_counter(); state=_build_event_state_from_loaded(loaded,event); state_seconds=time.perf_counter()-prepared
    result=run_event_repair(state=state,max_closure_iterations=30,max_local_operations=150,
        max_machine_sets=3,max_worker_crews=3,progress=lambda x:print(json.dumps(x),flush=True))
    acc=result.get('acceptance',{}); final=f'CRAFT_{name}_REPAIR_SOLVED' if acc.get('status')=='ACCEPTED' else f'CRAFT_{name}_REPAIR_FAILED'
    payload={'status':final,'event':event.as_dict(),'initial_region':list(result['initial_keys']),
      'final_region':list(result['state']['keys']),'closure':result['closure'],
      'resource_configuration_count':sum(len(x) for x in result['resource_configurations'].values()),
      'resource_audit':result['resource_audit'],'calendar_regime_count':result['regime_count'],
      'calendar_equivalence_mismatches':result['calendar_equivalence']['mismatches'],
      'regime_generation_by_operation':result['regime_summary'],
      'timing':{'state_event_preparation_seconds':state_seconds,**result.get('timing',{}),**acc.get('timing',{}),'total_wall_seconds':time.perf_counter()-started},
      'benchmark_contract_provenance':{
          'source':'crafter.disruptions.v2_execution_diagnostics._event_from_row',
          'machine_target_operation_key':'122_1061',
          'worker_target_operation_key':'1029_292',
          'M3_duration_seconds':1800,'W2_duration_seconds':28800,'M4_duration_seconds':0},
      'acceptance':acc,'database_writes':0}
    write(f'{name.lower()}_result.json',payload)
    files={str(p.relative_to(out)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'status':final,'files':files,'database_writes':0})
    print(json.dumps({'status':final,'initial_region':len(payload['initial_region']),'final_region':len(payload['final_region']),
      'configs':payload['resource_configuration_count'],'regimes':payload['calendar_regime_count'],
      'winning_stage':acc.get('winning_stage'),'materialization':acc.get('materialization',{}).get('mismatch_count'),
      'replay':acc.get('replay',{}).get('status'),'validator':acc.get('validator',{}).get('status'),
      'event_violations':len(acc.get('event_violations',())),'factory_isolation':acc.get('factory_isolation',{}).get('violation_count'),
      'database_writes':0},indent=2),flush=True)

if __name__=='__main__': main()
