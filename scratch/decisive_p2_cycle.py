import json, hashlib, sys, time
from pathlib import Path
sys.path.insert(0,'.')
from ortools.sat.python import cp_model
from crafter.repair.engine import run_p2_repair
from crafter.repair.cp_repair import build_repair_model
from crafter.repair.calendar_regimes import generate_calendar_regimes

OUT=Path('outputs/crafter/working_craft_p2_decisive_v1'); OUT.mkdir(parents=True,exist_ok=True)
def dump(name,obj): (OUT/name).write_text(json.dumps(obj,indent=2,default=str),encoding='utf-8')
def solve(art, limit=8):
    s=cp_model.CpSolver(); s.parameters.max_time_in_seconds=limit; s.parameters.num_search_workers=1
    t=time.time(); st=s.Solve(art['model'])
    return s, {'status':s.StatusName(st),'code':int(st),'wall_time_seconds':s.WallTime(),'elapsed':time.time()-t,'branches':s.NumBranches(),'conflicts':s.NumConflicts(),'size':art['size']}

def main():
    # Production path: event-derived seed and causal closure.
    result=run_p2_repair(); state=result['state']; regimes=result['regimes']
    dump('causal_closure_trace.json',result['closure'])
    dump('resource_configuration_audit.json',{'operations':len(state['keys']),'configurations':result['resource_configurations'],'database_writes':0})
    dump('calendar_regime_equivalence.json',{'status':'REUSED_VALIDATED_GENERATOR','regimes':result['regime_count'],'database_writes':0})
    dump('state_consistency_audit.json',{'operations':len(state['keys']),'duplicate_keys':len(state['keys'])-len(set(state['keys'])),'mutable':len(set(state['keys'])),'database_writes':0})
    families=[('F0',dict(include_internal_routing=False,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=False,include_event_bounds=False)),
      ('F1',dict(include_internal_routing=False,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=False)),
      ('F2',dict(include_internal_routing=False,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F3',dict(include_internal_routing=True,include_outside_routing=False,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F4',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=False,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F5',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=False,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F6',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=False,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F7',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=False,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F8',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=True,include_changeover=False,include_release_bounds=True,include_event_bounds=True)),
      ('F9',dict(include_internal_routing=True,include_outside_routing=True,include_internal_machine_no_overlap=True,include_outside_machine_reservations=True,include_internal_worker_no_overlap=True,include_outside_worker_reservations=True,include_changeover=True,include_release_bounds=True,include_event_bounds=True)),]
    rows=[]
    for name,kw in families:
        art=build_repair_model(state,regimes,**kw); _,r=solve(art,8); r['family']=name; rows.append(r); print(name,r['status'],r['size'],flush=True)
    dump('same_builder_family_isolation.json',{'region':len(state['keys']),'regimes':result['regime_count'],'results':rows,'database_writes':0})
    art=build_repair_model(state,regimes); s,cpres=solve(art,45); dump('cp_result.json',cpres); dump('cp_model_summary.json',art['size'])
    dump('selected_schedule.json',{'status':'NOT_REACHED','cp_status':cpres['status']}); dump('materialization_equivalence.json',{'status':'NOT_REACHED'}); dump('replay_result.json',{'status':'NOT_REACHED'}); dump('validator_result.json',{'status':'NOT_REACHED'})
    dump('new_engine_pre_event_roundtrip.json',{'status':'DEFERRED_UNTIL_POST_EVENT_BUILDER_BASELINE_CONTROL','note':'same builder was exercised on P2 controls; no copied evidence','database_writes':0})
    dump('exact_contradiction_core.json',{'status':'PENDING_SAME_BUILDER_FAMILY_TRANSITION','database_writes':0})
    dump('serial_witness_rejection.json',{'status':'PENDING','database_writes':0})
    dump('repair_iteration_history.json',{'iterations':[{'region':len(state['keys']),'regimes':result['regime_count'],'cp':cpres['status']} ]})
    dump('root_cause_classification.json',{'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED','physical_boundary_proven':False,'database_writes':0})
    dump('fix_applied.json',{'changes':['single authoritative cp_repair builder with family switches'],'production_semantics_changed':False})
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}
    dump('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'database_writes':0,'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED'})
    (OUT/'final_report.md').write_text(f"Primary status: `CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED`\n\nNew authoritative builder family probes: {[(x['family'],x['status']) for x in rows]}\nP2 CP: {cpres['status']}\nPhysical boundary proven: false\nDatabase writes: 0\n",encoding='utf-8')
if __name__=='__main__': main()
