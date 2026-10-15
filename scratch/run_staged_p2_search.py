import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.contracts import CrafterPhenotype,CrafterStatus,OperationAssignment,PhenotypeOperation
from crafter.phenotype import validate
from crafter.repair.calendar_regimes import _execution
from crafter.repair.cp_repair import build_repair_model
from crafter.repair.engine import run_p2_repair,_routing_aware_earliest

OUT=Path('outputs/crafter/p2_staged_search_v1'); OUT.mkdir(parents=True,exist_ok=True)
def write(n,v): (OUT/n).write_text(json.dumps(v,indent=2,default=str),encoding='utf-8')

def config_sig(c): return (c.line_id,c.k,c.machine_ids,c.worker_ids)

def incumbent_config(state,key,configs):
    row=state['after_index'].by_key[key]; sig=(int(row['line_id']),len(row.get('machine_ids',())),tuple(sorted(int(x) for x in row.get('machine_ids',()))),tuple(sorted(str(x) for x in row.get('employee_ids',()))))
    return next((c for c in configs[key] if config_sig(c)==sig),configs[key][0])

def diverse(configs,anchor,limit):
    def score(c):
        return (len(set(c.worker_ids)-set(anchor.worker_ids))*4+len(set(c.machine_ids)-set(anchor.machine_ids))*3+(c.k!=anchor.k)*2+(c.line_id!=anchor.line_id),config_sig(c))
    rest=sorted((c for c in configs if c!=anchor),key=score,reverse=True)
    return tuple([anchor]+rest[:max(0,limit-1)])

def add_hints(state,regimes,art):
    bounds=_routing_aware_earliest(state,regimes); hints=[]
    for key in sorted(state['keys']):
        start=bounds['earliest_start'].get(key)
        if start is None: continue
        candidates=[(i,r) for i,r in enumerate(regimes[key]) if int(r['lo'])<=start<=int(r['hi'])]
        if not candidates: continue
        i,r=candidates[0]; a,b=r['formula'][0]; end=int(a)*int(start)+int(b)
        art['model'].AddHint(art['starts'][key],int(start)); art['model'].AddHint(art['ends'][key],end); art['model'].AddHint(art['reg_lit'][key][i],1)
        hints.append({'operation_key':key,'regime_index':i,'start_seconds':start,'end_seconds':end})
    return hints

def extract(state,art,solver):
    rows=[]; mism=[]
    for key in sorted(art['reg_lit']):
        i=next(i for i,x in enumerate(art['reg_lit'][key]) if solver.Value(x)); r=art['reg_info'][(key,i)]; c=r['config']; start=solver.Value(art['starts'][key]); end=solver.Value(art['ends'][key]); ae,segs=_execution(state,c,start); f=r['formula']; cps=[(f[1+2*j][0]*start+f[1+2*j][1],f[2+2*j][0]*start+f[2+2*j][1]) for j in range(r['segments'])]
        row={'operation_key':key,'resource_configuration':asdict(c),'regime_index':i,'start_seconds':start,'productive_start_seconds':start+c.setup_seconds,'end_seconds':end,'worker_segments':cps,'machine_occupancy':[start,end]}; rows.append(row)
        if end!=ae or cps!=list(segs): mism.append({'operation_key':key,'cp_end':end,'authoritative_end':ae,'cp_segments':cps,'authoritative_segments':segs})
    return rows,mism

def make_phenotype(state,rows):
    by={x['operation_key']:x for x in rows}; origin=state['problem'].origin; out=[]
    for old in state['baseline'].operations:
        key=old.assignment.operation_key; row=by.get(key)
        if row is None: out.append(old); continue
        cfg=row['resource_configuration']; op=state['problem'].operation_by_key[key]
        assignment=OperationAssignment(key,op.order_id,op.operation_id,int(cfg['line_id']),tuple(cfg['machine_ids']),int(cfg['k']),int(row['start_seconds']),int(row['productive_start_seconds']),int(row['end_seconds']),int(cfg['setup_seconds']),int(cfg['productive_seconds']))
        segs=tuple((str(w),tuple((origin+timedelta(seconds=a),origin+timedelta(seconds=b)) for a,b in row['worker_segments'])) for w in cfg['worker_ids'])
        out.append(PhenotypeOperation(assignment,tuple(str(w) for w in cfg['worker_ids']),segs,'repair_resource_configuration'))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE,tuple(out))

def main():
    t=time.time(); result=run_p2_repair(max_machine_sets=3,max_worker_crews=3,max_closure_iterations=30,max_local_operations=150,progress=lambda x:print(json.dumps(x),flush=True)); state=result['state']; configs=result['resource_configurations']; regimes=result['regimes']; prepare=time.time()-t
    profile=[]
    for key in sorted(state['keys']):
        seg_intervals=sum(int(r['segments'])*len(r['config'].worker_ids)+len(r['config'].machine_ids) for r in regimes[key])
        profile.append({'operation_key':key,'configurations':len(configs[key]),'regimes':len(regimes[key]),'estimated_optional_intervals':seg_intervals})
    write('model_profile.json',{'operations':len(state['keys']),'configurations':sum(map(len,configs.values())),'regimes':sum(map(len,regimes.values())),'prepare_seconds':prepare,'per_operation':profile,'dominant_source':'CALENDAR_REGIME_OPTIONAL_INTERVAL_EXPANSION'})
    stages=[]; selected=[]; mismatches=[]; replay={'status':'NOT_REACHED'}; validator={'status':'NOT_REACHED'}
    for stage_limit in (1,2,3):
        chosen={}; filtered={}
        for key in sorted(state['keys']):
            anchor=incumbent_config(state,key,configs); chosen[key]=diverse(configs[key],anchor,stage_limit); allowed=set(chosen[key]); filtered[key]=tuple(r for r in regimes[key] if r['config'] in allowed)
        t=time.time(); art=build_repair_model(state,filtered); build=time.time()-t; hints=add_hints(state,filtered,art)
        solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=300; solver.parameters.num_search_workers=8; solver.parameters.random_seed=42; solver.parameters.use_optimization_hints=True
        t=time.time(); code=solver.Solve(art['model']); row={'stage':stage_limit,'resource_configurations':sum(len(v) for v in chosen.values()),'regimes':sum(len(v) for v in filtered.values()),'hints':len(hints),'build_seconds':build,'status':solver.StatusName(code),'wall_time_seconds':solver.WallTime(),'elapsed_seconds':time.time()-t,'branches':solver.NumBranches(),'conflicts':solver.NumConflicts(),**art['size']}; stages.append(row); write('stage_results.json',stages); print(json.dumps({'phase':'stage','limit':stage_limit,'status':row['status'],'regimes':row['regimes'],'variables':row['variables'],'branches':row['branches']},flush=True))
        if code in (cp_model.FEASIBLE,cp_model.OPTIMAL):
            selected,mismatches=extract(state,art,solver)
            replay={'status':'PASS' if not mismatches else 'FAIL','mismatch_count':len(mismatches),'mismatches':mismatches}
            if not mismatches:
                checked=validate(state['problem'],make_phenotype(state,selected),state['operator_index']); validator={'status':'VALID' if checked.status.value=='WORKER_FEASIBLE' else checked.status.value,'phenotype_status':checked.status.value,'validation_report':checked.validation_report}
            break
        if row['status']=='UNKNOWN':
            # A larger stage would only enlarge this unresolved search; retain
            # the strongest small-domain search result for evidence.
            break
    write('selected_schedule.json',{'status':'SELECTED' if selected else 'NOT_REACHED','operations':selected}); write('materialization_equivalence.json',{'status':'PASS' if selected and not mismatches else 'FAIL' if selected else 'NOT_REACHED','mismatch_count':len(mismatches),'mismatches':mismatches}); write('replay_result.json',replay); write('validator_result.json',validator)
    solved=bool(selected) and not mismatches and replay['status']=='PASS' and validator['status']=='VALID'; status='CRAFT_P2_REPAIR_SOLVED' if solved else 'CRAFT_P2_SEARCH_LIMIT'
    write('root_cause_classification.json',{'status':status,'search_reduction':'STAGED_LEGAL_RESOURCE_SUBSETS_WITH_ROUTE_AWARE_HINTS','physical_constraints_relaxed':False,'database_writes':0})
    (OUT/'final_report.md').write_text(f'# P2 staged exact search\n\nPrimary status: `{status}`\n\nThe corrected causal region contains {len(state["keys"])} operations. The full model profile is dominated by CalendarExecutionRegime optional intervals. Staged results: {json.dumps([{k:x[k] for k in ("stage","resource_configurations","regimes","variables","constraints","status","wall_time_seconds","branches") } for x in stages])}. Materialization mismatches: {len(mismatches)}; replay: `{replay["status"]}`; validator: `{validator["status"]}`; database writes: 0.\n',encoding='utf-8')
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; write('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':status,'database_writes':0})
    print(json.dumps({'status':status,'stages':stages,'materialization_mismatches':len(mismatches),'replay':replay['status'],'validator':validator['status']},indent=2),flush=True)

if __name__=='__main__': main()
