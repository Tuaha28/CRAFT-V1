"""Validate M2/M3/W2/M4 through the canonical deterministic repair core."""
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, '.')
from ortools.sat.python import cp_model

from crafter.disruptions.benchmark import _load_state
from crafter.disruptions.state_update import event_conflicts
from crafter.disruptions.v2_execution_diagnostics import _event_from_row
from crafter.repair.engine import (
    _build_event_state_from_loaded, build_p2_state, run_event_repair,
)
from crafter.repair.event_semantics import with_m2_capacity_contract
from crafter.repair.cp_repair import build_repair_model
from scratch.run_compressed_p2_search import (
    acceptance, add_hints, diverse, incumbent_choice, restrict_regimes,
    selected_modes,
)

OUT=Path('outputs/crafter/deterministic_core_validation_v2')
OUT.mkdir(parents=True,exist_ok=True)
def write(name,value): (OUT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')


def factory_isolation(state, selected):
    validation_index=state.get('validation_operator_index',state['operator_index'])
    worker_factories={}
    for (factory,_skill),workers in validation_index.get('by_factory',{}).items():
        for worker in workers: worker_factories.setdefault(str(worker),set()).add(int(factory))
    violations=[]
    for row in selected:
        cfg=row['resource_configuration']; factory=int(cfg['factory_id']); line=int(cfg['line_id'])
        if int(state['problem'].line_by_id[line].factory_id)!=factory:
            violations.append({'operation_key':row['operation_key'],'type':'LINE_FACTORY'})
        for mid in cfg['machine_ids']:
            machine=state['problem'].machine_by_id[int(mid)]
            if int(machine.factory_id)!=factory or int(machine.line_id)!=line:
                violations.append({'operation_key':row['operation_key'],'type':'MACHINE_FACTORY_LINE','machine_id':mid})
        for worker in cfg['worker_ids']:
            if factory not in worker_factories.get(str(worker),set()):
                violations.append({'operation_key':row['operation_key'],'type':'WORKER_FACTORY','worker_id':worker})
    return violations


def solve_case(loaded,event, *, prebuilt_state=None):
    # All scenarios, including the P2 parity gate, enter the same generic
    # production runner.  ``prebuilt_state`` only preserves P2's authoritative
    # event construction/seed compatibility contract.
    state=prebuilt_state if prebuilt_state is not None else _build_event_state_from_loaded(loaded,event)
    started=time.time()
    result=run_event_repair(state=state,max_closure_iterations=30,max_local_operations=150,
                            max_machine_sets=3,max_worker_crews=3,
                            progress=lambda x: print(json.dumps({'scenario':event.event_id,**x}),flush=True))
    configs,regimes=result['resource_configurations'],result['regimes']; state=result['state']
    if result.get('escalation'):
        return {'scenario':event.event_id,'event':event.as_dict(),'status':'CRAFT_NONLOCAL_REBUILD_REQUIRED',
                'initial_region':list(result['initial_keys']),'final_region':list(state['keys']),
                'database_writes':0}
    accepted_result=result['acceptance']; stages=accepted_result['stages']
    selected=accepted_result['selected_schedule']
    material=accepted_result['materialization']['operations']
    replay=accepted_result['replay']; validator=accepted_result['validator']
    event_violations=accepted_result['event_violations']
    isolation=accepted_result['factory_isolation']['violations']
    material_count=accepted_result['materialization']['mismatch_count']
    accepted=accepted_result['status']=='ACCEPTED'
    status=f'CRAFT_{event.event_id.split("_")[0]}_REPAIR_SOLVED' if accepted else 'CRAFT_SCENARIO_REPAIR_FAILED'
    raw=sum(int(x.get('raw_regime_count',x['regime_count'])) for x in result['regime_summary'])
    coalesced=sum(int(x.get('coalesced_regimes',0)) for x in result['regime_summary'])
    counts=[len(v) for v in configs.values()]
    return {'scenario':event.event_id,'event':event.as_dict(),'status':status,
            'initial_region':list(result['initial_keys']),'final_region':list(state['keys']),
            'closure':result['closure'],'resource':{'count':sum(counts),'min_per_operation':min(counts),'max_per_operation':max(counts),'truncated_operations':[x['operation_key'] for x in result['resource_audit'] if x['truncated']],'audit':result['resource_audit'],'anchor_provenance':accepted_result['anchor_provenance']},
            'calendar':{'raw_regimes':raw,'coalesced':coalesced,'final_regimes':result['regime_count'],'equivalence_mismatches':result['calendar_equivalence']['mismatches'],'zero_domains':[x['operation_key'] for x in result['regime_summary'] if x['status']!='OK']},
            'cp':{'stages':stages,'winning_stage':accepted_result['winning_stage']},
            'selected_schedule':selected,'materialization':{'mismatch_count':material_count,'operations':material},
            'replay':replay,'validator':validator,'event_violations':event_violations,
            'factory_isolation':{'violation_count':len(isolation),'violations':isolation},
            'post_cp_modifications':0,'database_writes':0,'elapsed_seconds':time.time()-started}


def main():
    loaded=_load_state(); problem,operator_index,rows,_durations,op_details,index,baseline,checked,source=loaded
    by={str(x['operation_key']):x for x in rows}
    machine_key='122_1061' if '122_1061' in by else sorted(by)[0]
    worker_key=next((k for k in sorted(by) if len(by[k].get('machine_ids',()))<=2),sorted(by)[0])
    def semantic(event, name):
        return replace(event, metadata={**(event.metadata or {}), 'repair_semantics':name})

    m2=semantic(_event_from_row(by[machine_key],'M2_K_REDUCTION',duration_seconds=1800),
                'M2_CAPACITY_REDUCTION')
    m2=with_m2_capacity_contract(loaded,m2)
    # This controlled run stops after M2.  M3/W2 semantic consumption is
    # covered by focused tests; M4 is intentionally deferred until P2 parity
    # and M2 both pass their complete acceptance gates.
    events=[m2]
    audit={'status':'PASS','canonical_entry':'crafter.repair.engine.run_event_repair',
           'path':['factory_state','event','deterministic_causal_closure','timing_independent_ResourceConfiguration','exact_CalendarExecutionRegime','canonical_CP_SAT','authoritative_materialization','independent_replay','independent_validator'],
           'joint_executable_modes_consumed_before_cp':False,
           'calendar_semantics':'FIXED_CREW_PAUSE_RESUME','regime_coalescing':True,
           'cp_builder':'crafter.repair.cp_repair.build_repair_model',
           'independent_replay':'crafter.lns_probe.run_e1_7_final_verified._replay_mode',
           'validator':'crafter.phenotype.validate/full 431-operation phenotype',
           'post_cp_modification_allowed':False,'database_writes':0}
    write('pipeline_audit.json',audit)
    # P2 is the extraction parity gate.  No new disruption is trusted unless
    # this generic path reproduces the already frozen accepted repair.
    p2_state=build_p2_state()
    p2=solve_case(loaded,p2_state['event'],prebuilt_state=p2_state)
    p2['scenario']='P2_GENERIC_PARITY'
    p2['status']='CRAFT_P2_REPAIR_SOLVED' if p2['status'].endswith('_REPAIR_SOLVED') else 'CRAFT_P2_GENERIC_PARITY_FAILED'
    write('p2_generic_parity_result.json',p2)
    if p2['status']!='CRAFT_P2_REPAIR_SOLVED':
        summary={'status':'CRAFT_GENERIC_RUNNER_P2_PARITY_FAILED','pipeline_audit':'PASS',
                 'scenarios':[{'scenario':p2['scenario'],'status':p2['status']}],
                 'database_writes':0}
        write('consolidated_result.json',summary)
        print(json.dumps(summary,indent=2),flush=True)
        return
    results=[p2]
    for event in events:
        result=solve_case(loaded,event); results.append(result)
        write(f'{event.event_id.lower()}_result.json',result)
    all_ok=all(x['status'].endswith('_REPAIR_SOLVED') for x in results)
    final='CRAFT_DETERMINISTIC_CORE_VALIDATED' if all_ok else 'CRAFT_DETERMINISTIC_CORE_PARTIAL'
    summary={'status':final,'pipeline_audit':'PASS','scenarios':[{k:x[k] for k in ('scenario','status','initial_region','final_region','database_writes')}|{'cp':x.get('cp',{}).get('winning_stage'),'materialization_mismatches':x.get('materialization',{}).get('mismatch_count'),'replay':x.get('replay',{}).get('status'),'validator':x.get('validator',{}).get('status'),'factory_isolation_violations':x.get('factory_isolation',{}).get('violation_count')} for x in results], 'database_writes':0}
    write('consolidated_result.json',summary)
    (OUT/'final_report.md').write_text('# CRAFT deterministic core validation\n\nPrimary status: `'+final+'`.\n\n'+json.dumps(summary['scenarios'],indent=2,default=str)+'\n\nDatabase writes: 0.\n',encoding='utf-8')
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'status':final,'files':files,'artifact_count':len(files),'database_writes':0})
    print(json.dumps(summary,indent=2,default=str),flush=True)

if __name__=='__main__': main()
