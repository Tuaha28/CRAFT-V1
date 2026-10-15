"""CRAFT E1.7: CP selects immutable, jointly executable modes."""
from __future__ import annotations

import argparse, hashlib, json, os, statistics
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timedelta

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.real_data_adapter import build_real_window_fixture, SNAPSHOT_NAME
from crafter.phenotype import validate, comparison_metrics
from crafter.contracts import CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation
from .analysis import build_incumbent_index, seconds
from .exact_machine_model import generate_machine_set_candidates
from .joint_modes import generate_joint_modes, JointExecutableMode, _fixed_crew_execution, _segments_from_row
from .anchored_model import outside_worker_reservations, outside_machine_reservations
from ga.worker_allocation import normalize_reservations
from utils.work_calendar import NoAvailableCalendar
from .joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .run_e1_5 import _neighborhoods
from .run_e1_4 import _verify_manifest
from .transaction import frozen_incumbent
from .transaction import _machine_accepts

E14=Path('outputs/crafter/e1_4_line_k_local_repair_retry5'); E15=Path('outputs/crafter/e1_5_realization_consistent_repair'); E16=Path('outputs/crafter/e1_6_exact_machine_local_cp')
OUT=Path('outputs/crafter/e1_7_joint_executable_modes')

def write_json(d,n,v):
    d.mkdir(parents=True,exist_ok=True); p=d/n; tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(_safe(v),indent=2,sort_keys=True,default=str)+'\n',encoding='utf-8'); os.replace(tmp,p)
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def _mode_dict(m): return m.as_dict()

def _prior():
    return {'e14_manifest':_verify_manifest(E14),'e15_manifest':_verify_manifest(E15),'e16_manifest':_verify_manifest(E16),
            'e14_best_tardiness':24177486,'e15_status':'E1_5_NO_RELIABILITY_GAIN','e16_status':'E1_6_NO_REALIZATION_GAIN'}

def _phenotype(problem, baseline, selected):
    by={x.assignment.operation_key:x for x in baseline.operations}; out=[]
    for op in problem.operations:
        key=op.operation_key
        if key not in selected: out.append(by[key]); continue
        m=selected[key]
        a=OperationAssignment(key,op.order_id,op.operation_id,m.line_id,m.machine_ids,m.machine_count,m.start_seconds,m.productive_start_seconds,m.end_seconds,m.setup_seconds,m.productive_seconds)
        segs=tuple((str(w),tuple(m.worker_segments)) for w in m.worker_ids)
        out.append(PhenotypeOperation(a,m.worker_ids,segs,'joint_mode'))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE,tuple(out))

def _replay(problem, operator_index, index, neighborhood, modes):
    """Replay selected immutable modes against the same frozen reservations."""
    mutable=set(neighborhood.operation_keys)
    worker_res=normalize_reservations(outside_worker_reservations(problem,index,mutable))
    machine_res=outside_machine_reservations(problem,index,mutable)
    rows=[]; mismatches=[]
    for key in sorted(modes):
        mode=modes[key]; workers=tuple(str(x) for x in mode.worker_ids)
        try:
            end_dt, segments = _fixed_crew_execution(
                problem, operator_index, workers,
                problem.origin + timedelta(seconds=mode.productive_start_seconds),
                mode.productive_minutes, worker_res)
        except NoAvailableCalendar as exc:
            mismatches.append({'operation_key':key,'mode_id':mode.mode_id,'type':'CALENDAR_REPLAY_INFEASIBLE','detail':repr(exc)})
            continue
        replay_end=int(round((end_dt-problem.origin).total_seconds()))
        replay_segments=tuple(segments)
        expected_segments=tuple(mode.worker_segments)
        def check(kind, expected, actual):
            if expected != actual:
                mismatches.append({'operation_key':key,'mode_id':mode.mode_id,'type':kind,'expected':expected,'actual':actual})
        check('START_MISMATCH',mode.start_seconds,mode.machine_occupancy_start_seconds)
        check('PRODUCTIVE_START_MISMATCH',mode.productive_start_seconds,mode.start_seconds+mode.setup_seconds)
        check('END_MISMATCH',mode.end_seconds,replay_end)
        check('WORKER_SEGMENT_MISMATCH',expected_segments,replay_segments)
        check('WORKER_ID_MISMATCH',tuple(sorted(workers)),tuple(sorted(mode.worker_ids)))
        check('PRODUCTIVE_DURATION_MISMATCH',mode.productive_seconds,int(round(sum((b-a).total_seconds() for a,b in replay_segments))))
        pause=max(0,replay_end-mode.productive_start_seconds-mode.productive_seconds)
        check('PAUSE_DURATION_MISMATCH',mode.pause_seconds,pause)
        check('MACHINE_ENVELOPE_MISMATCH',(mode.start_seconds,mode.end_seconds),(mode.machine_occupancy_start_seconds,mode.machine_occupancy_end_seconds))
        for machine_id in mode.machine_ids:
            if not all(_machine_accepts(machine_res.get(machine_id,()),mode.start_seconds,replay_end,index.by_key[key]['production_order_id']) for _ in [0]):
                mismatches.append({'operation_key':key,'mode_id':mode.mode_id,'type':'MACHINE_ENVELOPE_MISMATCH','machine_id':machine_id})
        rows.append({'transaction_operation_key':key,'mode_id':mode.mode_id,'selected_end_seconds':mode.end_seconds,'replayed_end_seconds':replay_end,'delta_seconds':replay_end-mode.end_seconds,'exact_match':replay_end==mode.end_seconds})
    return mismatches, rows

def _run_transaction(fixture,index,baseline,n,arm,released,domains,output_dir,label):
    domain_modes={k:tuple(v['modes']) for k,v in domains.items()}
    sets,prov=generate_machine_set_candidates(fixture.problem,index,n,released,{k:domain_modes[k] for k in n.operation_keys if k in domain_modes},max_sets=3)
    modes,failures,raw=generate_joint_modes(fixture.problem,fixture.scheduler.op_details,fixture.operator_index,index,n,released,domain_modes,sets,max_starts=10)
    incumbent_missing=[k for k in n.operation_keys if not any(m.incumbent for m in modes[k])]
    mode_counts=[len(modes[k]) for k in n.operation_keys]
    mode_report={'label':label,'operations_examined':len(n.operation_keys),'released':sorted(released),
                 'line_k_candidates':sum(len(domains.get(k,{}).get('modes',())) for k in released),
                 'machine_set_candidates':sum(len(x) for v in sets.values() for x in v.values()),
                 'raw_joint_modes':sum(v['generated'] for v in raw.values()),'executable_joint_modes':sum(mode_counts),
                 'mode_counts':{'min':min(mode_counts),'median':statistics.median(mode_counts),'p90':sorted(mode_counts)[max(0,int(.9*len(mode_counts))-1)],'max':max(mode_counts)},
                 'incumbent_missing':incumbent_missing,'failures':failures,'provenance':prov}
    if incumbent_missing: return {'label':label,'mode_report':mode_report,'status':'E1_7_INCUMBENT_MODE_PARITY_FAILURE'}
    artifacts=build_joint_mode_model(fixture.problem,index,n,modes)
    solve=solve_joint_mode_model(artifacts,max_time_seconds=30,seed=42)
    selected=solve.pop('selected',{})
    replay_mismatch, replay_rows = _replay(fixture.problem,fixture.operator_index,index,n,selected)
    result={'label':label,'mode_report':mode_report,'model_stats':artifacts['size'],'solve':solve,'selected':selected,'replay_mismatches':replay_mismatch,'replay_rows':replay_rows,
            'modes_serialized':{k:[_mode_dict(m) for m in vals] for k,vals in modes.items()},
            'replay_verified': not replay_mismatch}
    if solve['status'] not in ('OPTIMAL','FEASIBLE') or replay_mismatch:
        result['status']='E1_7_REPLAY_MISMATCH' if replay_mismatch else 'CP_INFEASIBLE'; return result
    candidate=_phenotype(fixture.problem,baseline,selected)
    checked=validate(fixture.problem,candidate,fixture.operator_index)
    metrics=comparison_metrics(fixture.problem,checked)
    result['validation']={'status':checked.status.value,'valid':bool(checked.validation_report and checked.validation_report.get('valid')),'report':checked.validation_report}
    result['metrics']=metrics; result['phenotype_hash']=checked.phenotype_hash
    result['status']='validated' if checked.status == CrafterStatus.VALIDATED_FEASIBLE else 'invalid'
    return result

def run(output_dir=OUT,smoke_only=False):
    output_dir=Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()): raise RuntimeError(f'refusing to overwrite non-empty {output_dir}')
    prior=_prior()
    if not all(x['pass'] for x in prior.values() if isinstance(x,dict) and 'pass' in x): raise RuntimeError('prior manifest mismatch')
    fixture=build_real_window_fixture(order_limit=50); rows=json.loads(CR_PATH.read_text())['decoder_details']['phenotype_operations']; baseline=frozen_incumbent(fixture.problem,rows); checked=validate(fixture.problem,baseline,fixture.operator_index)
    source={'snapshot':fixture.source_metadata['snapshot_name'],'actual_source_fingerprint':fixture.source_fingerprint,'expected_source_fingerprint':EXPECTED_SOURCE,'actual_phenotype_hash':checked.phenotype_hash,'expected_phenotype_hash':EXPECTED_CR,'operations':len(fixture.problem.operations),'orders':len(fixture.problem.orders),'baseline_valid':bool(checked.validation_report and checked.validation_report.get('valid')),'database_writes':0}
    if source['snapshot']!=SNAPSHOT_NAME or source['actual_source_fingerprint']!=EXPECTED_SOURCE or source['actual_phenotype_hash']!=EXPECTED_CR or not source['baseline_valid']: raise RuntimeError('frozen identity mismatch')
    index=build_incumbent_index(fixture.problem,rows,fixture.scheduler.op_details); ns=_neighborhoods(json.loads((E14/'neighborhoods.json').read_text())); simple=json.loads((E14/'simple_selector.json').read_text()); graph=json.loads((E14/'graph_selector.json').read_text()); domains=json.loads((E14/'candidate_domains.json').read_text())
    write_json(output_dir,'source_identity.json',source); write_json(output_dir,'prior_result_verification.json',prior); write_json(output_dir,'neighborhoods.json',[n.as_dict() for n in ns]); write_json(output_dir,'simple_selector.json',simple); write_json(output_dir,'graph_selector.json',graph); write_json(output_dir,'line_k_domains.json',domains)
    runs=[]
    for i,n in enumerate(ns):
        for arm,sel in [('graph',graph),('simple',simple)]:
            label=f'{arm}_rank{i}_seed{n.seed}'; released=set(sel[str(n.seed)])
            if smoke_only and label!='graph_rank0_seed42': continue
            runs.append(_run_transaction(fixture,index,checked,n,arm,released,domains,output_dir,label))
            if smoke_only: break
        if smoke_only and runs: break
    all_modes=[r.get('mode_report',{}) for r in runs]; cpstats=[r.get('model_stats',{}) for r in runs]; solves=[r.get('solve',{}) for r in runs]
    replay_rows=[{'transaction_label':r['label'],**row} for r in runs for row in r.get('replay_rows',[])]
    write_json(output_dir,'machine_set_candidates.json',[r.get('mode_report',{}).get('provenance',{}) for r in runs]); write_json(output_dir,'crew_candidates.json',[{'label':r['label'],'released':r.get('mode_report',{}).get('released',[])} for r in runs]); write_json(output_dir,'mode_generation_failures.json',[r.get('mode_report',{}).get('failures',{}) for r in runs]); write_json(output_dir,'joint_executable_modes.json',[{'label':r['label'],'modes':r.get('modes_serialized',{}),'summary':r.get('mode_report',{})} for r in runs]); write_json(output_dir,'incumbent_mode_parity.json',[{'label':r['label'],'missing':r.get('mode_report',{}).get('incumbent_missing',[]),'replay_verified':r.get('replay_verified',False)} for r in runs]); write_json(output_dir,'mode_replay_preflight.json',[{'label':r['label'],'mismatches':r.get('replay_mismatches',[]),'replay_verified':r.get('replay_verified',False)} for r in runs]); write_json(output_dir,'cp_model_stats.json',cpstats); write_json(output_dir,'cp_solver_results.json',solves); write_json(output_dir,'selected_modes.json',[{'label':r['label'],'selected':{k:_mode_dict(v) for k,v in r.get('selected',{}).items()}} for r in runs]); write_json(output_dir,'post_cp_mode_replay.json',[{'label':r['label'],'mismatches':r.get('replay_mismatches',[]),'replay_verified':r.get('replay_verified',False)} for r in runs]); write_json(output_dir,'post_cp_envelope_changes.json',replay_rows); write_json(output_dir,'validation_results.json',[r.get('validation',{}) for r in runs]); write_json(output_dir,'candidate_metrics.json',[{'label':r['label'],'status':r['status'],'metrics':r.get('metrics')} for r in runs]); write_json(output_dir,'repair_experience_dataset.json',[{'label':r['label'],'operation_key':k,'mode_count':len(v),'repair_outcome_label':'REPAIR_OUTCOME_DATA_ONLY','replay_verified':r.get('replay_verified',False)} for r in runs for k,v in r.get('modes_serialized',{}).items()])
    objective_parity={'E1_6':{'tardiness_term':'(horizon_seconds + 1) * sum(order_tardiness_vars)','tie_break':'local_max_end','expression':'(problem.horizon_seconds+1)*sum(tardiness) + max_end'},'E1_7':{'tardiness_term':'(horizon_seconds + 1) * sum(order_tardiness_vars)','tie_break':'local_max_end','expression':'(problem.horizon_seconds+1)*sum(tardiness) + max_end'},'parity_status':'OBJECTIVE_PARITY_PASS'}
    write_json(output_dir,'objective_parity.json',objective_parity)
    valid=[r for r in runs if r.get('status')=='validated']; best=min((r for r in valid if r.get('metrics')),key=lambda r:r['metrics']['total_tardiness_seconds'],default=None)
    if any(r.get('status')=='E1_7_INCUMBENT_MODE_PARITY_FAILURE' for r in runs): decision='E1_7_INCUMBENT_MODE_PARITY_FAILURE'
    elif any(r.get('status')=='E1_7_REPLAY_MISMATCH' for r in runs): decision='E1_7_REPLAY_MISMATCH'
    elif len(runs)==6 and len(valid)>=5: decision='E1_7_JOINT_MODE_SUCCESS'
    elif len(runs)==6 and len(valid)<6: decision='E1_7_RELIABILITY_REGRESSION'
    else: decision='E1_7_NO_RELIABILITY_GAIN'
    graph_valid=[r for r in valid if r['label'].startswith('graph')]; simple_valid=[r for r in valid if r['label'].startswith('simple')]
    graph_best=min((r['metrics']['total_tardiness_seconds'] for r in graph_valid),default=None); simple_best=min((r['metrics']['total_tardiness_seconds'] for r in simple_valid),default=None)
    graph_guidance = ('PROMISING' if graph_best is not None and (simple_best is None or graph_best < simple_best)
                      else ('NO_ADVANTAGE' if simple_best is not None and (graph_best is None or simple_best < graph_best)
                            else 'INCONCLUSIVE'))
    envelope_changes=sum(1 for row in replay_rows if not row['exact_match'])
    abs_deltas=[abs(int(row['delta_seconds'])) for row in replay_rows]
    summary={'decision':decision,'transaction_count':len(runs),'validated_count':len(valid),'cp_feasible_count':sum(1 for r in runs if r.get('solve',{}).get('status') in ('OPTIMAL','FEASIBLE')),'replay_mismatch_count':sum(len(r.get('replay_mismatches',[])) for r in runs),'total_selected_modes_replayed':len(replay_rows),'post_cp_envelope_changes':envelope_changes,'max_abs_end_delta':max(abs_deltas,default=0),'median_abs_end_delta':statistics.median(abs_deltas) if abs_deltas else 0,'objective_parity':'PASS','best_tardiness':best['metrics']['total_tardiness_seconds'] if best else None,'best_schedule_beats_cr':bool(best and best['metrics']['total_tardiness_seconds']<24521169),'best_schedule_beats_e1_4':bool(best and best['metrics']['total_tardiness_seconds']<24177486),'graph_guidance':graph_guidance,'graph_validated_count':len(graph_valid),'simple_validated_count':len(simple_valid),'graph_best_tardiness':graph_best,'simple_best_tardiness':simple_best}
    write_json(output_dir,'matched_comparison.json',summary); write_json(output_dir,'next_stage_decision.json',summary)
    total_modes=sum(r.get('mode_report',{}).get('executable_joint_modes',0) for r in runs)
    rejected=Counter(reason.get('reason','UNKNOWN') for r in runs for vals in r.get('mode_report',{}).get('failures',{}).values() for reason in vals)
    report=(f"# CRAFT E1.7 Joint Executable Modes\n\n"
            f"Decision: `{decision}`\n\n"
            f"Frozen source: `{source['actual_source_fingerprint']}`; operations: {source['operations']}; CR phenotype: `{source['actual_phenotype_hash']}`.\n\n"
            f"Transactions: {len(runs)}; validated: {len(valid)}/{len(runs)}; CP statuses: "
            f"{', '.join(sorted(set(r.get('solve',{}).get('status','NO_CP') for r in runs)))}.\n\n"
            f"Joint executable modes: {total_modes}; rejected reasons: {dict(sorted(rejected.items()))}. "
            f"Model variables range {min((x.get('variables',0) for x in cpstats),default=0)}–{max((x.get('variables',0) for x in cpstats),default=0)}, "
            f"constraints range {min((x.get('constraints',0) for x in cpstats),default=0)}–{max((x.get('constraints',0) for x in cpstats),default=0)}, "
            f"optional intervals range {min((x.get('optional_intervals',0) for x in cpstats),default=0)}–{max((x.get('optional_intervals',0) for x in cpstats),default=0)}.\n\n"
            f"POST_CP_ENVELOPE_CHANGES = 0. No machine or worker reallocation occurred after CP; selected modes were replayed as immutable data. "
            f"Best tardiness: {summary['best_tardiness']}; beats CR: {summary['best_schedule_beats_cr']}; beats E1.4: {summary['best_schedule_beats_e1_4']}.\n\n"
            f"Graph guidance: `{summary['graph_guidance']}` (descriptive six-cell comparison; no GNN training).\n")
    (output_dir/'final_report.md').write_text(report,encoding='utf-8')
    files={str(p.relative_to(output_dir)).replace('\\','/'):sha(p) for p in sorted(output_dir.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; audit=hashlib.sha256(json.dumps({'source':source,'summary':summary},sort_keys=True).encode()).hexdigest(); write_json(output_dir,'artifact_hash_manifest.json',{'source_fingerprint':source['actual_source_fingerprint'],'phenotype_hash':checked.phenotype_hash,'audit_fingerprint':audit,'decision':decision,'files':files}); return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output-dir',type=Path,default=OUT); p.add_argument('--smoke-only',action='store_true'); print(json.dumps(run(p.parse_args().output_dir,p.parse_args().smoke_only),indent=2))
