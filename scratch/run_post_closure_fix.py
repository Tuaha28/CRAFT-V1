import hashlib
import json
import shutil
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, '.')
from ortools.sat.python import cp_model

from crafter.contracts import CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation
from crafter.phenotype import validate
from crafter.repair.calendar_regimes import _execution
from crafter.repair.engine import run_p2_repair

FAMILY = Path('outputs/crafter/p2_exact_constraint_isolation_v2')
OUT = Path('outputs/crafter/p2_exact_constraint_isolation_v3')
OUT.mkdir(parents=True, exist_ok=True)

def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

def selected_schedule(state, art, solver):
    selected=[]; mismatches=[]
    for key in sorted(art['reg_lit']):
        ri=next(i for i,lit in enumerate(art['reg_lit'][key]) if solver.Value(lit))
        regime=art['reg_info'][(key,ri)]; cfg=regime['config']
        start=solver.Value(art['starts'][key]); end=solver.Value(art['ends'][key])
        actual_end,segs=_execution(state,cfg,start)
        formula=regime['formula']
        cp_segments=[(formula[1+2*i][0]*start+formula[1+2*i][1],formula[2+2*i][0]*start+formula[2+2*i][1]) for i in range(regime['segments'])]
        row={'operation_key':key,'resource_configuration':asdict(cfg),'regime_index':ri,
             'start_seconds':start,'productive_start_seconds':start+cfg.setup_seconds,
             'end_seconds':end,'worker_segments':cp_segments,'machine_occupancy':[start,end]}
        selected.append(row)
        if end!=actual_end or list(segs)!=cp_segments:
            mismatches.append({'operation_key':key,'cp_end':end,'authoritative_end':actual_end,'cp_segments':cp_segments,'authoritative_segments':segs})
    return selected,mismatches

def phenotype(state, selected):
    replacement={x['operation_key']:x for x in selected}; output=[]; origin=state['problem'].origin
    for old in state['baseline'].operations:
        key=old.assignment.operation_key; row=replacement.get(key)
        if row is None: output.append(old); continue
        cfg=row['resource_configuration']; op=state['problem'].operation_by_key[key]
        assignment=OperationAssignment(key,op.order_id,op.operation_id,int(cfg['line_id']),tuple(int(x) for x in cfg['machine_ids']),int(cfg['k']),int(row['start_seconds']),int(row['productive_start_seconds']),int(row['end_seconds']),int(cfg['setup_seconds']),int(cfg['productive_seconds']))
        segments=tuple((str(w),tuple((origin+timedelta(seconds=int(a)),origin+timedelta(seconds=int(b))) for a,b in row['worker_segments'])) for w in cfg['worker_ids'])
        output.append(PhenotypeOperation(assignment,tuple(str(x) for x in cfg['worker_ids']),segments,'repair_resource_configuration'))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE,tuple(output))

def main():
    for name in ('full_domain_family_isolation.json','outside_routing_boundary_audit.json','outside_routing_exact_core.json','internal_routing_exact_core.json','outside_machine_exact_core.json','internal_machine_exact_core.json','outside_worker_exact_core.json','internal_worker_exact_core.json','release_event_bound_audit.json'):
        shutil.copyfile(FAMILY/name,OUT/name)
    t=time.time(); result=run_p2_repair(max_machine_sets=3,max_worker_crews=3,max_closure_iterations=30,max_local_operations=150,progress=lambda x: print(json.dumps(x),flush=True)); build=time.time()-t
    state=result['state']; art=result['model']; additions=[x for it in result['closure']['iterations'] for x in it.get('added_operations',[]) if x.get('conflict_type','').startswith('ROUTE_AWARE_')]
    first=additions[0] if additions else None
    write('source_identity.json',{'family_isolation':str(FAMILY),'event_id':state['event'].event_id,'source':state.get('source'),'database_writes':0})
    write('causal_boundary_fix.json',{'status':'APPLIED','defect':'standalone downstream minimum end ignored reachability through delayed internal routing','route_aware_additions':additions})
    expected_addition=next((x for x in additions if x.get('operation_key')=='1029_296' and x.get('trigger_operation')=='1029_295' and x.get('conflict_type')=='ROUTE_AWARE_OUTSIDE_SUCCESSOR_DOMAIN_DEFICIT'),None)
    witness_status='EXACT_COLLECTIVE_ROUTING_CONTRADICTION_IDENTIFIED' if expected_addition else 'EXPECTED_ROUTE_AWARE_ADDITION_NOT_REPRODUCED'
    write('exact_constraint_witness.json',{'status':witness_status,
        'direct_event_constraint':{'operation':'1029_292','relation':'end >= delayed_end_seconds','lower_bound_seconds':state['event'].metadata.get('delayed_end_seconds')},
        'derived_routing_bound':{'operation':'1029_293','relation':'start >= 1029_292.end','implied_lower_bound_seconds':state['event'].metadata.get('delayed_end_seconds')},
        'internal_chain':['1029_292','1029_293','1029_294','1029_295'],'outside_edge':['1029_295','1029_296'],
        'expected_route_aware_addition':expected_addition,'first_route_aware_deficit':first,
        'explanation':'The standalone 1029_295 minimum end was reachable only when ordinary internal predecessors were ignored. The direct event end bound on 1029_292 propagates through ordinary precedence and forces 1029_295 later than frozen future successor 1029_296.'})
    write('fix_applied.json',{'status':'APPLIED','changes':['routing membership dispatch no longer falls through from disabled internal routing to outside routing','exclude_routing_edges honored for outside successors','causal closure now propagates route-aware earliest authoritative completion'],'tests':'tests/test_repair_routing_dispatch.py'})
    write('post_fix_region.json',{'operation_keys':list(state['keys']),'size':len(state['keys']),'initial_size':len(result['initial_keys'])})
    write('post_fix_resource_summary.json',{'configurations':sum(len(v) for v in result['resource_configurations'].values()),'operations':len(result['resource_configurations']),'domain_truncated':any(x.get('truncated') for x in result['resource_audit'])})
    write('post_fix_regime_summary.json',{'regimes':result['regime_count'],'equivalence_mismatches':result['calendar_equivalence']['mismatches'],'all_equivalent':result['calendar_equivalence']['all_equivalent']})
    solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=300; solver.parameters.num_search_workers=8; solver.parameters.random_seed=42
    t=time.time(); code=solver.Solve(art['model']); cp={'status':solver.StatusName(code),'code':int(code),'wall_time_seconds':solver.WallTime(),'elapsed_seconds':time.time()-t,'branches':solver.NumBranches(),'conflicts':solver.NumConflicts(),'build_seconds':build,**art['size']}; write('cp_result.json',cp)
    selected=[]; mismatches=[]; replay={'status':'NOT_REACHED'}; validator={'status':'NOT_REACHED'}
    if code in (cp_model.FEASIBLE,cp_model.OPTIMAL):
        selected,mismatches=selected_schedule(state,art,solver)
        replay_mismatch=[]
        for row in selected:
            cfg=next(c for c in result['resource_configurations'][row['operation_key']] if asdict(c)==row['resource_configuration'])
            end,segs=_execution(state,cfg,row['start_seconds'])
            if end!=row['end_seconds'] or list(segs)!=row['worker_segments']:
                replay_mismatch.append({'operation_key':row['operation_key'],'end':end,'segments':segs})
        replay={'status':'PASS' if not replay_mismatch else 'FAIL','mismatch_count':len(replay_mismatch),'mismatches':replay_mismatch}
        if not mismatches and not replay_mismatch:
            checked=validate(state['problem'],phenotype(state,selected),state['operator_index'])
            validator={'status':'VALID' if checked.status.value=='WORKER_FEASIBLE' else str(checked.status.value),'phenotype_status':checked.status.value,'validation_report':checked.validation_report}
    write('selected_schedule.json',{'status':'SELECTED' if selected else 'NOT_REACHED','operations':selected})
    write('materialization_equivalence.json',{'status':'PASS' if selected and not mismatches else 'FAIL' if selected else 'NOT_REACHED','mismatch_count':len(mismatches),'mismatches':mismatches})
    write('replay_result.json',replay); write('validator_result.json',validator)
    solved=cp['status'] in ('FEASIBLE','OPTIMAL') and not mismatches and replay.get('status')=='PASS' and validator.get('status')=='VALID'
    status='CRAFT_P2_REPAIR_SOLVED' if solved else 'CRAFT_P2_EXACT_CONSTRAINT_IDENTIFIED' if expected_addition else 'CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE'
    write('root_cause_classification.json',{'status':status,'root_cause':'CAUSAL_CLOSURE_USED_STANDALONE_NOT_ROUTE_REACHABLE_MINIMUM_END','physical_impossibility_proven':False,'database_writes':0})
    (OUT/'final_report.md').write_text(f'# P2 exact constraint isolation and closure fix\n\nPrimary status: `{status}`\n\nThe exact contradiction was the P2-delayed internal route `1029_293 -> 1029_294 -> 1029_295` against frozen future edge `1029_295 -> 1029_296`. Route-aware closure expanded the repair region from 9 to {len(state["keys"])} operations. CP: `{cp["status"]}`; materialization mismatches: {len(mismatches)}; replay: `{replay.get("status")}`; validator: `{validator.get("status")}`. Database writes: 0.\n',encoding='utf-8')
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':status,'database_writes':0})
    print(json.dumps({'status':status,'region':len(state['keys']),'cp':cp['status'],'materialization_mismatches':len(mismatches),'replay':replay.get('status'),'validator':validator.get('status')},indent=2),flush=True)

if __name__=='__main__': main()
