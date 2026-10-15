import hashlib,json,sys,time
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.calendar_regimes import _execution
from crafter.repair.resource_configs import serialize_configurations

OUT=Path('outputs/crafter/p2_independent_resource_repair_v7'); OUT.mkdir(parents=True,exist_ok=True)
def w(name,value): (OUT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')
def main():
 t=time.time(); r=run_p2_repair(max_machine_sets=3,max_worker_crews=3,progress=lambda x: print(json.dumps(x),flush=True)); build=time.time()-t; state=r['state']; art=r['model']
 w('event_input.json',{'event_id':state['event'].event_id,'operation_keys':list(state['event'].operation_keys),'metadata':state['event'].metadata,'database_writes':0})
 w('initial_repair_region.json',{'operation_keys':list(r['initial_keys']),'size':len(r['initial_keys'])})
 w('causal_closure_trace.json',r['closure']); w('final_repair_region.json',{'operation_keys':list(state['keys']),'size':len(state['keys'])})
 w('resource_configuration_domain.json',serialize_configurations(r['resource_configurations']))
 w('resource_configuration_audit.json',r['resource_audit']); w('resource_generation_summary.json',r['resource_summaries'])
 w('calendar_regime_summary.json',{'operations':r['regime_summary'],'total_regimes':r['regime_count']}); w('calendar_regime_equivalence.json',r['calendar_equivalence'])
 if art is None:
  w('cp_model_summary.json',{'status':'NOT_BUILT_NONLOCAL_REGION','build_seconds':build}); w('cp_result.json',{'status':'NOT_RUN_NONLOCAL_REGION'}); w('selected_schedule.json',{'status':'NOT_REACHED'}); w('materialization_equivalence.json',{'status':'NOT_REACHED'}); w('replay_result.json',{'status':'NOT_REACHED'}); w('validator_result.json',{'status':'NOT_REACHED'}); w('resource_expansion_history.json',{'iterations':r['resource_summaries']}); w('repair_iteration_history.json',{'closure_iterations':len(r['closure']['iterations']),'stop_reason':'NONLOCAL_REGION'}); w('regression_results.json',{'status':'DEFERRED_UNTIL_P2_VALIDATED'}); root='CRAFT_P2_NONLOCAL_REBUILD_REQUIRED'; w('root_cause_classification.json',{'status':root,'region_size':len(state['keys']),'local_limit':r['escalation']['local_limit'],'physical_impossibility_proven':False,'database_writes':0}); (OUT/'final_report.md').write_text(f'# P2 independent-resource repair\n\nPrimary status: `{root}`\n\nThe production generator is timing-independent and consumes no JointExecutableModes. Initial region: {len(r["initial_keys"])}; causal closure: {len(state["keys"])} operations ({len(state["keys"])/431:.1%} of the benchmark); configurations: {sum(len(v) for v in r["resource_configurations"].values())}. This exceeds the configured local-repair ceiling of {r["escalation"]["local_limit"]}, so calendar-regime expansion and local CP were intentionally not launched. Database writes: 0.\n',encoding='utf-8'); files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; w('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':root,'database_writes':0}); print(json.dumps({'status':root,'region':len(state['keys']),'configs':sum(len(v) for v in r['resource_configurations'].values())},indent=2)); return
 w('cp_model_summary.json',{**art['size'],'build_seconds':build})
 solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=180; solver.parameters.num_search_workers=8
 t=time.time(); status=solver.Solve(art['model']); cp={'status':solver.StatusName(status),'code':int(status),'wall_time_seconds':solver.WallTime(),'elapsed_seconds':time.time()-t,'branches':solver.NumBranches(),'conflicts':solver.NumConflicts(),**art['size']}; w('cp_result.json',cp)
 selected=[]; mismatches=[]
 if status in (cp_model.FEASIBLE,cp_model.OPTIMAL):
  for key,lits in art['reg_lit'].items():
   ri=next(i for i,l in enumerate(lits) if solver.Value(l)); rg=art['reg_info'][(key,ri)]; cfg=rg['config']; start=solver.Value(art['starts'][key]); end=solver.Value(art['ends'][key]); exact_end,segs=_execution(state,cfg,start); formula=rg['formula']; cpsegs=[(formula[1+2*i][0]*start+formula[1+2*i][1],formula[2+2*i][0]*start+formula[2+2*i][1]) for i in range(rg['segments'])]
   row={'operation_key':key,'resource_configuration':asdict(cfg),'start_seconds':start,'productive_start_seconds':start+cfg.setup_seconds,'end_seconds':end,'worker_segments':cpsegs,'machine_occupancy':[start,end]}; selected.append(row)
   if end!=exact_end or list(segs)!=cpsegs: mismatches.append({'operation_key':key,'cp_end':end,'authoritative_end':exact_end,'cp_segments':cpsegs,'authoritative_segments':segs})
 w('selected_schedule.json',{'operations':selected,'status':'SELECTED' if selected else 'NOT_REACHED'}); w('materialization_equivalence.json',{'mismatches':mismatches,'mismatch_count':len(mismatches),'status':'PASS' if selected and not mismatches else 'NOT_REACHED' if not selected else 'FAIL'})
 w('replay_result.json',{'status':'NOT_REACHED'}); w('validator_result.json',{'status':'NOT_REACHED'}); w('resource_expansion_history.json',{'iterations':r['resource_summaries']}); w('repair_iteration_history.json',{'closure_iterations':len(r['closure']['iterations']),'cp_status':cp['status']}); w('regression_results.json',{'status':'DEFERRED_UNTIL_P2_VALIDATED'})
 root='CRAFT_P2_SEARCH_LIMIT' if cp['status']=='UNKNOWN' else 'CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE' if any(x.get('truncated') for x in r['resource_audit']) else 'CRAFT_P2_COMPLETE_MODEL_INFEASIBLE' if cp['status']=='INFEASIBLE' else 'CRAFT_P2_REPAIR_SOLVED' if selected and not mismatches else 'CRAFT_P2_SEARCH_LIMIT'
 w('root_cause_classification.json',{'status':root,'resource_domain_complete':not any(x.get('truncated') for x in r['resource_audit']),'physical_impossibility_proven':False,'database_writes':0})
 (OUT/'final_report.md').write_text(f'# P2 independent-resource repair\n\nPrimary status: `{root}`\n\nInitial region: {len(r["initial_keys"])}; final causal region: {len(state["keys"])}. Timing-independent configurations: {sum(len(v) for v in r["resource_configurations"].values())}; regimes: {r["regime_count"]}. Calendar equivalence mismatches: {r["calendar_equivalence"]["mismatches"]}. CP: {cp["status"]}. Materialization mismatches: {len(mismatches)}. Replay and validator: not reached unless a feasible schedule is selected. Database writes: 0.\n',encoding='utf-8')
 files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; w('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':root,'database_writes':0}); print(json.dumps({'status':root,'region':len(state['keys']),'configs':sum(len(v) for v in r['resource_configurations'].values()),'regimes':r['regime_count'],'cp':cp['status']},indent=2))
if __name__=='__main__':main()
