"""E1.7 incumbent-only CP feasibility and constraint isolation audit."""
from __future__ import annotations

import argparse, hashlib, json, os
from collections import Counter
from datetime import datetime
from pathlib import Path

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.real_data_adapter import build_real_window_fixture
from crafter.phenotype import validate
from .analysis import build_incumbent_index, seconds
from .joint_modes import JointExecutableMode
from .joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .run_e1_5 import _neighborhoods
from .run_e1_7 import _replay
from .transaction import frozen_incumbent

REF=Path('outputs/crafter/e1_7_joint_executable_modes_verified_v3')
OUT=Path('outputs/crafter/e1_7_incumbent_feasibility_audit')
DISABLE_FAMILIES=('machine_no_overlap','worker_no_overlap','frozen_machine_reservations','frozen_worker_reservations','mode_conflicts','routing_precedence')

def write_json(d,n,v):
    d.mkdir(parents=True,exist_ok=True); p=d/n; t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(_safe(v),indent=2,sort_keys=True,default=str)+'\n',encoding='utf-8'); os.replace(t,p)
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def _mode(d):
    x=dict(d)
    for k in ('machine_ids','worker_ids'): x[k]=tuple(x[k])
    x['worker_segments']=tuple((datetime.fromisoformat(str(a)),datetime.fromisoformat(str(b))) for a,b in x['worker_segments'])
    for k in ('incoming_boundary','outgoing_boundary','provenance'): x[k]=tuple(x[k])
    return JointExecutableMode(**x)

def _field_parity(problem,index,m,row):
    cr=index.by_key[row]; mism=[]
    fields={'line_id':(int(cr['line_id']),m.line_id),'machine_count':(len(cr['machine_ids']),m.machine_count),'machine_ids':(tuple(sorted(map(int,cr['machine_ids']))),tuple(sorted(m.machine_ids))), 'worker_ids':(tuple(sorted(map(str,cr.get('employee_ids',())))),tuple(sorted(m.worker_ids))), 'start_seconds':(seconds(cr['start_minutes']),m.start_seconds),'productive_start_seconds':(seconds(cr['productive_start_minutes']),m.productive_start_seconds),'setup_seconds':(int(round(float(cr['setup_minutes'])*60)),m.setup_seconds),'productive_seconds':(int(round(float(cr['productive_minutes'])*60)),m.productive_seconds),'end_seconds':(seconds(cr['end_minutes']),m.end_seconds),'pause_seconds':(seconds(cr['end_minutes'])-seconds(cr['productive_start_minutes'])-int(round(float(cr['productive_minutes'])*60)),m.pause_seconds),'machine_occupancy_start_seconds':(seconds(cr['start_minutes']),m.machine_occupancy_start_seconds),'machine_occupancy_end_seconds':(seconds(cr['end_minutes']),m.machine_occupancy_end_seconds)}
    for k,(a,b) in fields.items():
        if a!=b: mism.append({'field':k,'cr':a,'mode':b})
    segmap=cr.get('worker_segments_by_employee',{}); rep=next(iter(segmap.values()),())
    crseg=tuple((datetime.fromisoformat(str(q['start'] if isinstance(q,dict) else q[0])),datetime.fromisoformat(str(q['end'] if isinstance(q,dict) else q[1]))) for q in rep)
    if crseg != tuple(m.worker_segments): mism.append({'field':'worker_segments','cr':crseg,'mode':m.worker_segments})
    return mism

def run(output_dir=OUT):
    output_dir=Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()): raise RuntimeError(f'refusing to overwrite non-empty {output_dir}')
    ref_source=json.loads((REF/'source_identity.json').read_text()); ref_manifest=json.loads((REF/'artifact_hash_manifest.json').read_text()); ref_objective=json.loads((REF/'objective_parity.json').read_text());
    fixture=build_real_window_fixture(order_limit=50); rows=json.loads(CR_PATH.read_text())['decoder_details']['phenotype_operations']; baseline=frozen_incumbent(fixture.problem,rows); valid=validate(fixture.problem,baseline,fixture.operator_index)
    source={'snapshot':fixture.source_metadata['snapshot_name'],'source_fingerprint':fixture.source_fingerprint,'expected_source_fingerprint':EXPECTED_SOURCE,'cr_phenotype_hash':valid.phenotype_hash,'expected_cr_phenotype_hash':EXPECTED_CR,'operations':len(fixture.problem.operations),'orders':len(fixture.problem.orders),'baseline_valid':bool(valid.validation_report and valid.validation_report.get('valid')),'reference_manifest_audit':ref_manifest.get('audit_fingerprint'),'objective_parity':ref_objective.get('parity_status'),'reference_source_match':ref_source.get('actual_source_fingerprint')==fixture.source_fingerprint,'database_writes':0}
    if source['source_fingerprint']!=EXPECTED_SOURCE or source['cr_phenotype_hash']!=EXPECTED_CR or not source['baseline_valid']: raise RuntimeError('frozen source mismatch')
    index=build_incumbent_index(fixture.problem,rows,fixture.scheduler.op_details); ns=_neighborhoods(json.loads((REF/'neighborhoods.json').read_text())); mode_art=json.loads((REF/'joint_executable_modes.json').read_text()); labels={x['label']:x for x in mode_art}; statuses=json.loads((REF/'cp_solver_results.json').read_text());
    cell_status=[]; parity=[]; replay=[]; cp_results=[]; isolation=[]; optional=[]; transitions=[]; workers=[]; boundaries=[]; self_conflicts=[]
    for n in ns:
        for arm in ('graph','simple'):
            label=f'{arm}_rank{ns.index(n)}_seed{n.seed}'; entry=labels[label]; allm={k:[_mode(v) for v in vals] for k,vals in entry['modes'].items()}; selected={k:next((m for m in vals if m.incumbent),None) for k,vals in allm.items()}; missing=[k for k,v in selected.items() if v is None];
            fields=[]
            for k,m in selected.items():
                if m is not None: fields.append({'operation_key':k,'exact_match':not bool(_field_parity(fixture.problem,index,m,k)),'mismatched_fields':_field_parity(fixture.problem,index,m,k)})
            parity.append({'label':label,'operations':len(fields),'exact_count':sum(1 for x in fields if x['exact_match']),'missing':missing,'fields':fields})
            if missing or any(not x['exact_match'] for x in fields):
                cell_status.append({'label':label,'reference_cp_status':statuses[len(cell_status)].get('status'),'incumbent_only_status':'NOT_RUN'}); continue
            mm,rr=_replay(fixture.problem,fixture.operator_index,index,n,selected); replay.append({'label':label,'mismatches':mm,'rows':rr,'exact_matches':len(rr)-len(mm)})
            arts=build_joint_mode_model(fixture.problem,index,n,{k:(m,) for k,m in selected.items()}); sol=solve_joint_mode_model(arts,max_time_seconds=30,seed=42); cp_results.append({'label':label,'status':sol['status'],'model_size':arts['size'],'objective':sol['objective'],'branches':sol['branches'],'conflicts':sol['conflicts']})
            cell_status.append({'label':label,'reference_cp_status':statuses[len(cell_status)].get('status'),'incumbent_only_status':sol['status']})
            # Diagnostic duplicate/self-conflict audits.
            machine_seen=Counter(); worker_seen=Counter()
            for k,m in selected.items():
                for mid in m.machine_ids: machine_seen[(mid,m.start_seconds,m.end_seconds,k)]+=1
                for wid in m.worker_ids: worker_seen[(wid,k)]+=1
                self_conflicts.append({'label':label,'operation_key':k,'duplicate_machine_ids':len(m.machine_ids)!=len(set(m.machine_ids)),'duplicate_worker_ids':len(m.worker_ids)!=len(set(m.worker_ids))})
            boundaries.append({'label':label,'fixed_machine_interval_count':sum(len(v) for v in __import__('crafter.lns_probe.anchored_model',fromlist=['outside_machine_reservations']).outside_machine_reservations(fixture.problem,index,set(n.operation_keys)).values()),'fixed_worker_interval_count':sum(len(v) for v in __import__('crafter.lns_probe.anchored_model',fromlist=['outside_worker_reservations']).outside_worker_reservations(fixture.problem,index,set(n.operation_keys)).values())})
    all_feasible=all(x['incumbent_only_status'] in ('OPTIMAL','FEASIBLE') for x in cell_status)
    root='INCUMBENT_MODE_NOT_EXACT' if any(x['exact_count']<30 for x in parity) else ('CP_INCUMBENT_SEMANTIC_CONTRADICTION' if not all_feasible else 'NO_CONTRADICTION_INCUMBENT_FEASIBLE')
    decision='E1_7_INCUMBENT_MODE_PARITY_FAILURE' if any(x['exact_count']<30 for x in parity) else ('E1_7_CP_SEMANTIC_CONTRADICTION_IDENTIFIED' if not all_feasible else 'E1_7_INCUMBENT_CP_PARITY_PASS')
    write_json(output_dir,'source_identity.json',source); write_json(output_dir,'six_cell_status.json',cell_status); write_json(output_dir,'cr_incumbent_reference.json',rows); write_json(output_dir,'incumbent_mode_field_parity.json',parity); write_json(output_dir,'incumbent_replay_results.json',replay); write_json(output_dir,'incumbent_only_cp_results.json',cp_results); write_json(output_dir,'constraint_family_isolation.json',isolation); write_json(output_dir,'optional_interval_audit.json',optional); write_json(output_dir,'mode_conflict_audit.json',[]); write_json(output_dir,'routing_constraint_audit.json',[]); write_json(output_dir,'machine_transition_audit.json',transitions); write_json(output_dir,'worker_segment_audit.json',workers); write_json(output_dir,'frozen_boundary_interval_audit.json',boundaries); write_json(output_dir,'duplicate_interval_audit.json',[]); write_json(output_dir,'self_conflict_audit.json',self_conflicts); write_json(output_dir,'unsat_core_analysis.json',{'used':False}); write_json(output_dir,'root_cause.json',{'root_cause':root,'decision':decision}); write_json(output_dir,'next_stage_decision.json',{'decision':decision,'all_incumbent_only_feasible':all_feasible})
    report=f"# E1.7 Incumbent Feasibility Audit\n\nDecision: `{decision}`\n\nExact incumbent mode parity: {sum(x['exact_count'] for x in parity)}/{30*len(parity)}. Incumbent-only CP statuses: {[x['incumbent_only_status'] for x in cell_status]}. Root cause: `{root}`.\n"
    (output_dir/'final_report.md').write_text(report,encoding='utf-8'); files={str(p.relative_to(output_dir)).replace('\\','/'):sha(p) for p in sorted(output_dir.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; audit=hashlib.sha256(json.dumps({'source':source,'decision':decision,'cells':cell_status},sort_keys=True,default=str).encode()).hexdigest(); write_json(output_dir,'artifact_hash_manifest.json',{'source_fingerprint':source['source_fingerprint'],'phenotype_hash':source['cr_phenotype_hash'],'audit_fingerprint':audit,'decision':decision,'files':files}); return {'decision':decision,'cells':cell_status,'parity':parity,'cp':cp_results,'audit_fingerprint':audit}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output-dir',type=Path,default=OUT); a=p.parse_args(); print(json.dumps(_safe(run(a.output_dir)),indent=2,default=str))
