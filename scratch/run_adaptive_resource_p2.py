import hashlib, json, sys, time
from dataclasses import asdict
from pathlib import Path
from collections import Counter

sys.path.insert(0, '.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.resource_configs import expand_resource_configurations, serialize_configurations, generate_resource_configurations
from crafter.repair.causal_closure import MOVABLE
from crafter.repair.calendar_regimes import generate_calendar_regimes, _execution
from crafter.repair.cp_repair import build_repair_model

OUT = Path('outputs/crafter/p2_adaptive_resource_expansion_v3')
OUT.mkdir(parents=True, exist_ok=True)

def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

def solve(model, seconds=75):
    s = cp_model.CpSolver(); s.parameters.max_time_in_seconds = float(seconds)
    s.parameters.num_search_workers = 8; s.parameters.random_seed = 42
    t=time.time(); code=s.Solve(model)
    return s, {'status':s.StatusName(code), 'code':int(code), 'wall_time_seconds':s.WallTime(),
               'elapsed_seconds':time.time()-t, 'branches':s.NumBranches(), 'conflicts':s.NumConflicts()}

def audit_compact(audit):
    rows=[]
    for a in audit:
        machines=[]; workers=[]
        for d in a.get('domain_rows',[]):
            machines.extend(tuple(tuple(x) for x in d.get('candidate_machine_sets_after_pruning',[])))
            workers.extend(tuple(x) for x in d.get('worker_crews_after_pruning',[]))
        rows.append({'operation_key':a['operation_key'],'lifecycle_state':a.get('lifecycle_state'),
                     'eligible_lines':a.get('eligible_lines',[]),'legal_k_by_line':a.get('legal_k_by_line',{}),
                     'machine_sets':sorted({tuple(x) for x in machines}), 'worker_crews':sorted({tuple(x) for x in workers}),
                     'resource_configuration_count':a.get('total_resource_configurations',0),
                     'incumbent_configuration_present':a.get('incumbent_configuration_present',False),
                     'more_machine_sets_available':any(d.get('more_machine_sets_available') for d in a.get('domain_rows',[])),
                     'more_worker_crews_available':any(d.get('more_worker_crews_available') for d in a.get('domain_rows',[])),
                     'truncated':a.get('truncated',False)})
    return rows

def pressure(state, configs, audit):
    keys=sorted(configs); machine_count=Counter(); worker_count=Counter()
    for vals in configs.values():
        for c in vals:
            machine_count.update(c.machine_ids); worker_count.update(c.worker_ids)
    rows=[]
    for a in audit:
        key=a['operation_key']; vals=configs.get(key,()); ms=set(x for c in vals for x in c.machine_ids); ws=set(x for c in vals for x in c.worker_ids)
        shared_m=sum(1 for x in ms if machine_count[x]>1); shared_w=sum(1 for x in ws if worker_count[x]>1)
        rows.append({'operation_key':key,'configuration_count':len(vals),'shared_machine_count':shared_m,
                     'shared_worker_count':shared_w,'machine_ids':sorted(ms),'worker_ids':sorted(ws),
                     'truncated':a.get('truncated',False),'score':shared_w*3+shared_m*2+(20 if a.get('truncated') else 0),
                     'lifecycle_state':a.get('lifecycle_state')})
    return sorted(rows,key=lambda x:(-x['score'],x['operation_key']))

def selected_schedule(state, regimes, art, solver):
    selected=[]; mism=[]
    for key in sorted(art['reg_lit']):
        lits=art['reg_lit'][key]; ri=next(i for i,l in enumerate(lits) if solver.Value(l)); rg=art['reg_info'][(key,ri)]
        cfg=rg['config']; start=solver.Value(art['starts'][key]); end=solver.Value(art['ends'][key]); auth_end,segs=_execution(state,cfg,start)
        f=rg['formula']; cpsegs=[(f[1+2*i][0]*start+f[1+2*i][1],f[2+2*i][0]*start+f[2+2*i][1]) for i in range(rg['segments'])]
        row={'operation_key':key,'resource_configuration':asdict(cfg),'regime_index':ri,'start_seconds':start,'end_seconds':end,
             'productive_start_seconds':start+cfg.setup_seconds,'worker_segments':cpsegs,'machine_occupancy':[start,end]}
        selected.append(row)
        if int(end)!=int(auth_end) or list(cpsegs)!=list(segs): mism.append({'operation_key':key,'cp_end':end,'authoritative_end':auth_end,'cp_segments':cpsegs,'authoritative_segments':segs})
    return selected,mism

def main():
    progress=lambda x: print(json.dumps(x),flush=True)
    t=time.time(); r=run_p2_repair(max_machine_sets=3,max_worker_crews=3,progress=progress); state=r['state']; configs=r['resource_configurations']; audit=r['resource_audit']
    write('resource_expansion_baseline.json',{'operations':audit_compact(audit),'region_size':len(state['keys']),'configurations':sum(map(len,configs.values())),'regimes':r['regime_count']})
    write('causal_closure_trace.json',r['closure'])
    p=pressure(state,configs,audit); write('resource_conflict_pressure.json',p)
    history=[]; final_solver=None; final_art=None; final_cp=None; final_regimes=r['regimes']; final_eq=r['calendar_equivalence']; expanded_audit=audit
    # Include the baseline solve as round 0.
    art= r['model']; solver,cp=solve(art['model'],75); cp.update(art['size']); history.append({'iteration':0,'operations_expanded':[],'configs_before':sum(map(len,configs.values())),'configs_added':0,'configs_after':sum(map(len,configs.values())),'regimes_before':r['regime_count'],'regimes_after':r['regime_count'],'cp':cp})
    rounds=0; expanded_once=set()
    while cp['status'] in ('INFEASIBLE','UNKNOWN') and rounds<5:
        rows=[x for x in p if x['truncated'] and x.get('lifecycle_state') in MOVABLE]
        if not rows: break
        # First pass expands every currently truncated movable operation so one
        # contested operation cannot hide behind another's unchanged domain.
        chosen=[x['operation_key'] for x in rows if x['operation_key'] not in expanded_once] or [x['operation_key'] for x in rows[:3]]
        rounds+=1; before=sum(map(len,configs.values())); old_regimes=sum(map(len,final_regimes.values())); before_by={k:len(v) for k,v in configs.items()}
        cap=30 if rounds==1 else 30
        configs,selected_audit,summary=expand_resource_configurations(state,configs,chosen,max_machine_sets=cap,max_worker_crews=cap)
        expanded_once.update(chosen)
        _, expanded_audit, _ = generate_resource_configurations(state,max_machine_sets=cap,max_worker_crews=cap)
        final_regimes,regsum,total,final_eq=generate_calendar_regimes(state,configs)
        final_art=build_repair_model(state,final_regimes); final_solver,cp=solve(final_art['model'],90)
        p=pressure(state,configs,expanded_audit)
        history.append({'iteration':rounds,'operations_expanded':chosen,'reason':'all currently truncated legally movable operations, ranked by current conflict pressure',
                        'configs_before':before,'configs_added':sum(len(configs[k])-before_by.get(k,0) for k in chosen),'configs_after':sum(map(len,configs.values())),
                        'regimes_before':old_regimes,'regimes_after':total,'cp':cp,'equivalence':final_eq})
        if sum(len(configs[k])-before_by.get(k,0) for k in chosen) == 0: break
        if cp['status'] in ('FEASIBLE','OPTIMAL'): break
    write('resource_expansion_history.json',history); write('resource_conflict_pressure.json',p)
    write('expanded_resource_domain.json',serialize_configurations(configs)); write('resource_expansion_audit.json',audit_compact(expanded_audit))
    write('calendar_regime_summary.json',{'operations':final_regimes and [{'operation_key':k,'regime_count':len(v)} for k,v in final_regimes.items()] or [],'total_regimes':sum(map(len,final_regimes.values()))})
    write('calendar_regime_equivalence.json',{'mismatches':final_eq.get('mismatches',0),'all_equivalent':final_eq.get('all_equivalent'),'checks':len(final_eq.get('checks',[]))})
    write('cp_model_summary.json',final_art['size'] if final_art else {})
    write('cp_result.json',cp)
    selected=[]; mism=[]
    if cp['status'] in ('FEASIBLE','OPTIMAL') and final_art:
        selected,mism=selected_schedule(state,final_regimes,final_art,final_solver)
    write('selected_schedule.json',{'status':'SELECTED' if selected else 'NOT_REACHED','operations':selected})
    write('materialization_equivalence.json',{'status':'PASS' if selected and not mism else 'FAIL' if selected else 'NOT_REACHED','mismatch_count':len(mism),'mismatches':mism})
    # Exact replay/validator are only claimed after materialization. The canonical direct-time
    # path currently exposes no post-CP shifting; preserve explicit not-reached if infeasible.
    write('replay_result.json',{'status':'NOT_REACHED','reason':'CP not feasible' if not selected else 'requires authoritative phenotype adapter'})
    write('validator_result.json',{'status':'NOT_REACHED'})
    write('expanded_domain_family_isolation.json',{'status':'DEFERRED','reason':'adaptive expansion remains primary; same canonical builder retained'})
    if cp['status']=='FEASIBLE' or cp['status']=='OPTIMAL': root='CRAFT_P2_REPAIR_SOLVED' if selected and not mism else 'CRAFT_P2_SEARCH_LIMIT'
    elif any(x.get('truncated') for x in expanded_audit): root='CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE'
    else: root='CRAFT_P2_SEARCH_LIMIT' if cp['status']=='UNKNOWN' else 'CRAFT_P2_COMPLETE_MODEL_INFEASIBLE'
    write('root_cause_classification.json',{'status':root,'region_size':len(state['keys']),'resource_domain_complete':not any(x.get('truncated') for x in expanded_audit),'physical_impossibility_proven':False,'database_writes':0})
    report=f'''# Adaptive P2 resource expansion\n\nPrimary status: `{root}`\n\nRegion: {len(state['keys'])} operations. Baseline configurations: {sum(map(len,r['resource_configurations'].values())) if isinstance(r.get('resource_configurations'),dict) else 'see baseline'}. Final configurations: {sum(map(len,configs.values()))}; regimes: {sum(map(len,final_regimes.values()))}. CP: `{cp['status']}`. Materialization mismatches: {len(mism)}. Replay: not reached; validator: not reached.\n\nAdaptive rounds: {rounds}; database writes: 0; shift semantics remained FIXED_CREW_PAUSE_RESUME; no floaters or handover semantics.\n'''
    (OUT/'final_report.md').write_text(report,encoding='utf-8')
    files={str(x.relative_to(OUT)).replace('\\','/'):hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted(OUT.rglob('*')) if x.is_file() and x.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':root,'database_writes':0})
    print(json.dumps({'status':root,'rounds':rounds,'region':len(state['keys']),'configs':sum(map(len,configs.values())),'regimes':sum(map(len,final_regimes.values())),'cp':cp['status']},indent=2))

if __name__=='__main__': main()
