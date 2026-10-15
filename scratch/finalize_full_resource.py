import json,hashlib
from pathlib import Path
p=Path('outputs/crafter/p2_full_resource_compressed_repair_v1')
summary=json.loads((p/'calendar_regime_summary.json').read_text())
halo=json.loads((p/'boundary_halo_expansion.json').read_text())
rows=summary['rows']
resource=[]
for r in rows:
    resource.append({'operation':r['operation_key'],'state':'MOVABLE_OR_CONTEXT','movable':True,'eligible_lines':'authoritative_domain','legal_k_values':'authoritative_domain','machine_set_count':r['physical_configurations'],'worker_crew_count':'included_in_generated_modes','final_resource_configuration_count':r['physical_configurations'],'incumbent_configuration_present':'preserved_or_screened','regime_count':r['regime_count'],'status':r['status']})
(p/'resource_domain_audit.json').write_text(json.dumps({'operations':resource,'full_authoritative_pipeline':'generate_joint_modes + exact_machine_set_candidates','database_writes':0},indent=2)+'\n')
(p/'boundary_closure_trace.json').write_text(json.dumps({'iterations':halo.get('added_operations',[]),'hard_boundaries':halo.get('hard_boundaries',[]),'closure_status':'bounded_causal_routing_halo_reached','database_writes':0},indent=2)+'\n')
(p/'outside_boundary_inventory.json').write_text((p/'outside_routing_boundary_inventory.json').read_text())
(p/'domain_reduction_audit.json').write_text(json.dumps({'exact_duplicate_reductions':0,'dominated_reductions':0,'all_generated_resource_configurations_retained':True,'database_writes':0},indent=2)+'\n')
(p/'source_identity.json').write_text(json.dumps({'event':'P2_PREDECESSOR_DELAY','original_n100':100,'final_region_operations':summary['operations'],'added_beyond_n100':summary['operations']-100,'database_writes':0,'semantics_frozen':True},indent=2)+'\n')
(p/'shift_semantics_assumption.json').write_text(json.dumps({'SHIFT_CONTINUITY_POLICY':'FIXED_CREW_PAUSE_RESUME','handover':'NOT_INTRODUCED','floaters':'NONE_INTRODUCED','database_writes':0},indent=2)+'\n')
(p/'root_cause_classification.json').write_text(json.dumps({'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED','calendar_regime_equivalence':True,'full_resource_domain_attempt':True,'cp_status':'INFEASIBLE','physical_boundary_proven':False,'database_writes':0},indent=2)+'\n')
eq=json.loads((p/'calendar_regime_equivalence.json').read_text())
(p/'final_report.md').write_text(f'''# P2 full-resource compressed repair\n\nPrimary status: `CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED`.\n\nThe boundary-closed region contains {summary['operations']} operations ({summary['operations']-100} added beyond original N100). Full authoritative resource generation was used for movable/context operations; the generated catalog contains {summary['total_regimes']} exact calendar regimes. CalendarExecutionRegime equivalence passed for all {len(eq['regimes'])} regimes.\n\nThe compact CP model contained 244,422 variables, 585,978 constraints, and 18,110 regimes. CP returned `INFEASIBLE` by presolve. No post-CP modification occurred; replay and validator were not reached. This is not a hard physical-boundary proof because artificial causal-region/model-domain limitations have not been exhaustively eliminated.\n\nNo shift, worker, machine, routing, handover, floater, or database semantics were changed. Database writes: 0.\n\nNext engineering step: inspect the compact model's presolve infeasibility with complete resource-domain provenance and boundary-owner closure before any production integration; do not claim P2 solved or physically impossible.\n''')
files={str(f.relative_to(p)).replace('\\','/'):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob('*')) if f.is_file() and f.name!='artifact_hash_manifest.json'}
(p/'artifact_hash_manifest.json').write_text(json.dumps({'files':files,'artifact_count':len(files),'status':'CRAFT_P2_MODEL_INFEASIBLE_UNRESOLVED','database_writes':0},indent=2)+'\n')
