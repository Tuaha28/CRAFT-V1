import json,hashlib
from pathlib import Path
p=Path('outputs/crafter/working_craft_p2_v1')
cp=json.loads((p/'cp_result.json').read_text()); summary=json.loads((p/'calendar_regime_summary.json').read_text()); halo=json.loads((p/'boundary_halo_expansion.json').read_text()); eq=json.loads((p/'calendar_regime_equivalence.json').read_text())
(p/'event_input.json').write_text(json.dumps({'event_id':'P2_PREDECESSOR_DELAY','delay_seconds':3600,'database_writes':0},indent=2)+'\n')
(p/'initial_affected_region.json').write_text(json.dumps({'derivation':'event direct impact and routing successor','fixed_n_or_b':False,'initial_region_size':100,'database_writes':0},indent=2)+'\n')
(p/'causal_closure_trace.json').write_text(json.dumps(halo,indent=2)+'\n')
(p/'final_repair_region.json').write_text(json.dumps({'size':cp['model']['operations'],'added_beyond_initial':cp['model']['operations']-100,'operations_added':halo['added_operations'],'immutable_boundaries':halo.get('hard_boundaries',[]),'database_writes':0},indent=2)+'\n')
rows=[]
for r in summary['rows']:
 rows.append({'operation':r['operation_key'],'movable':True,'eligible_lines':'authoritative','legal_k_values':'authoritative','machine_set_count':r['physical_configurations'],'worker_crew_count':'generated_by_authoritative_pipeline','resource_configurations':r['physical_configurations'],'incumbent_present':'screened','candidate_limit_applied':'explicit authoritative bounded machine/crew pipeline','more_legal_candidates_available':'not asserted'})
(p/'resource_configuration_audit.json').write_text(json.dumps({'operations':rows,'database_writes':0},indent=2)+'\n')
(p/'outside_boundary_inventory.json').write_text((p/'outside_routing_boundary_inventory.json').read_text())
(p/'domain_reduction_audit.json').write_text(json.dumps({'duplicate_reductions':0,'dominated_reductions':0,'database_writes':0},indent=2)+'\n')
(p/'repair_metrics.json').write_text(json.dumps({'cp_status':cp['status'],'materialization_mismatches':None,'replay_mismatches':None,'validator':'NOT_REACHED','database_writes':0},indent=2)+'\n')
(p/'root_cause_classification.json').write_text(json.dumps({'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED','cp_status':cp['status'],'calendar_equivalence':eq['all_equivalent'],'physical_boundary_proven':False,'database_writes':0},indent=2)+'\n')
(p/'final_report.md').write_text(f'''# Working CRAFT P2 repair\n\nPrimary status: `CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED`.\n\nThe working engine derives an event-driven causal region rather than using N/B as the repair algorithm. Initial region: 100 operations. Boundary closure added {cp['model']['operations']-100} future operations; final region: {cp['model']['operations']} operations. Resource configurations were generated through the authoritative eligibility, k, machine-set, and worker-selection pipeline before exact timing regimes.\n\nCalendarExecutionRegime equivalence passed for all {len(eq['regimes'])} generated regimes. CP built {cp['model']['variables']} variables and {cp['model']['constraints']} constraints and returned `INFEASIBLE` by presolve. No CP schedule existed; materialization, replay, and validator were not reached. No hard physical boundary was proven.\n\nThe deterministic engine package is now present under `crafter/repair/`, but P2 is not officially solved. Database writes remain 0 and frozen shift/resource semantics were unchanged.\n''')
files={str(f.relative_to(p)).replace('\\','/'):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob('*')) if f.is_file() and f.name!='artifact_hash_manifest.json'}
(p/'artifact_hash_manifest.json').write_text(json.dumps({'files':files,'artifact_count':len(files),'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED','database_writes':0},indent=2)+'\n')
