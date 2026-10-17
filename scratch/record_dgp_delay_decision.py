import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
a=json.loads((root/'outputs/integration_audit/dgp_delay_sanity_audit.json').read_text())
c=json.loads((root/'outputs/integration_audit/dgp_planned_start_floor_counterfactual.json').read_text())
s999=json.loads((root/'outputs/integration_audit/graph_profile_actual_seed999_staging.json').read_text())
result={
 'decision':'ACCEPT_FOR_SYNTHETIC_AUTHORITATIVE_REBUILD_WITH_CALIBRATION_QUALIFICATION',
 'classification':{
   'implementation_amplification_or_double_counting':'NOT_FOUND',
   'causal_propagation':'CONFIRMED',
   'calibration_realism':'SYNTHETIC_REASONED_NOT_EMPIRICALLY_FITTED_TO_FACTORY_OUTCOMES',
   'real_factory_prevalence_claim_permitted':False,
 },
 'dataset':{'database':a['database'],'dataset_version':a['dataset_version'],'origin_database':a['origin_database']},
 'operation_delay':a['overall'],
 'alternate_actual_seed':{
   'database':s999['database'],'actual_seed':999,'operations':s999['delay']['valid'],
   'positive':s999['delay']['positive'],'prevalence':s999['delay']['prevalence']},
 'causal_evidence':{
   'lot_lateness_additive_identity':a['additive_lot_lateness'],
   'readiness_and_propagation':a['causal_indicators'],
   'recurrence_checks':a['recurrence_checks'],
   'planned_start_floor_counterfactual':c,
   'simulator_contract':'ready=max(machine_free, employee_free, predecessor_end, planned_start); queue=dispatch-predecessor_end; one execution draw and at most one repair per row; clocks updated once to actual_end',
   'no_double_counting_basis':[
     'All 176187 lot rows satisfy actual_end-planned_end = start_shift + productive_delta + pause_delta with maximum absolute residual 1.36e-11 minutes.',
     'Exact replay using the unrounded production duration formula matches every stored start/end within one-second calendar quantization.',
     'Readiness combines causal parents by max, never sum; queue is measured from dispatch and is not separately added to execution time.',
     'Removing the hard planned-start floor changes only 10 of 8211 labels (91.2313% to 91.1095%).'
   ]
 },
 'contributions':{
   'positive_lateness_positive_component_share':a['additive_lot_lateness']['positive_lateness_positive_component_share'],
   'positive_operations_mostly_inherited_predecessor_fraction':a['causal_indicators']['fraction_positive_operations_mostly_inherited_predecessor'],
   'dynamic_breakdown_rows':a['causal_indicators']['rows_with_dynamic_breakdown_inferred'],
   'prior_breakdown_rows':a['causal_indicators']['rows_with_prior_breakdown'],
   'employee_leave_rows':a['causal_indicators']['employee_leave_rows'],
   'calendar_pause_delta_distribution':a['additive_lot_lateness']['component_distributions']['pause_delta'],
 },
 'order_summary':a['order_summary'],
 'by_factory':a['by_factory'],'by_operation_type':a['by_operation_type'],
 'by_planned_start_month':a['by_planned_start_month'],'by_planned_start_week':a['by_planned_start_week'],
 'queue_by_factory':a['queue_by_factory'],'queue_by_operation_type':a['queue_by_operation_type'],
 'spans':a['spans'],'ml_consequences':a['ml_consequences'],
 'representative_order_traces':a['representative_order_traces'],
 'qualification':'The observed prevalence is a property of this synthetic frozen-plan stress simulation. Duration scale is company-data calibrated, but execution SD, breakdown process, prior-breakdown mean shift, and queue-SD widening are synthetic assumptions; the result must not be presented as measured real-factory delay prevalence.'
}
(root/'outputs/integration_audit/dgp_delay_sanity_decision.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
ledger_path=root/'outputs/integration_audit/ledger.json'; ledger=json.loads(ledger_path.read_text())
ledger['findings'].append({
 'id':'IA-014','area':'corrected two-phase DGP delay prevalence','historical_classification':'NEW_DATASET_IMPACT_GATE',
 'severity':'FOUNDATIONAL_CALIBRATION_QUALIFICATION','status':'ACCEPTED_NO_IMPLEMENTATION_DEFECT_SYNTHETIC_CALIBRATION_LIMITATION',
 'measurement':{'operations':8211,'positive':7491,'negative':720,'prevalence':a['overall']['prevalence'],
  'alternate_actual_seed_prevalence':s999['delay']['prevalence'],'mostly_inherited_predecessor_fraction':a['causal_indicators']['fraction_positive_operations_mostly_inherited_predecessor'],
  'start_shift_positive_component_share':a['additive_lot_lateness']['positive_lateness_positive_component_share']['start_shift'],
  'planned_floor_labels_removed':c['positive_labels_removed_without_floor'],'replay_max_timestamp_error_seconds':1.0,
  'employee_leave_rows':0,'dynamic_breakdown_rows':a['causal_indicators']['rows_with_dynamic_breakdown_inferred']},
 'evidence':['outputs/integration_audit/dgp_delay_sanity_audit.json','outputs/integration_audit/dgp_planned_start_floor_counterfactual.json','outputs/integration_audit/dgp_replay_first_divergence.json','outputs/integration_audit/graph_profile_actual_seed999_staging.json','outputs/integration_audit/dgp_delay_sanity_decision.json'],
 'affected_results':['old GNN metrics are superseded','new delay AP no-skill baseline is about 0.91','synthetic prevalence cannot be claimed as a real factory rate'],
 'recommended_solution':'Train and evaluate against the accepted corrected synthetic DGP, report imbalance-aware metrics and the synthetic calibration limitation; fit execution/disruption parameters only if real outcome data becomes available.'})
ledger['subsystems']['authoritative_dataset_rebuild']={
 'status':'PROMOTED_VALIDATED_DGP_ACCEPTED_GRAPH_RETRAIN_PENDING','planning_seed':42,'actual_execution_seed':42,
 'staging_database':'garment_fyp_db_seed_v2_20260914_142905','staging_dataset_version':'b72b7231-c2bf-4e82-b952-5609fdf853ec',
 'live_database':'garment_fyp_db','live_dataset_version':'9fa2915b-5f28-4463-8e4f-d21b5b57dc8d',
 'promotion_id':'94d02cf41c924991a588','backup_database':'promotion_backup_94d02cf41c924991a588',
 'full_plan_rows':176187,'plan_hash':'d423143a8926e0d2e671456f8cded8c1afa8bf9f0b3fed205ba9ea608135ad71',
 'plan_independent_across_actual_seeds':True,'live_validation_passed':True,'backup_validation_passed':True,
 'evidence':['outputs/integration_audit/full_staging_plan_independence.json','outputs/promotions/94d02cf41c924991a588.json','outputs/integration_audit/dgp_delay_sanity_decision.json']}
ledger['pre_reseed_gates']['full_reseed_started']=True
ledger['pre_reseed_gates']['full_reseed_promoted']=True
ledger_path.write_text(json.dumps(ledger,indent=2),encoding='utf-8')
print(root/'outputs/integration_audit/dgp_delay_sanity_decision.json')
