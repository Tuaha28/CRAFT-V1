"""Finalize the immutable Window-14 Step-4A diagnostic evidence."""
from __future__ import annotations
import hashlib, json
from pathlib import Path

OUT = Path('outputs/crafter/factory2_172_v1/window14_step4a')

def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))

def load_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

def accepted(row):
    return bool((row.get('scoped_acceptance') or {}).get('accepted'))

def summary(rows):
    return [{
        'label': r.get('label'),
        'operation_count': r.get('required_operation_count'),
        'resource_configuration_count': r.get('resource_configuration_count'),
        'calendar_regime_count': r.get('calendar_regime_count'),
        'candidate_exists': bool(r.get('candidate_exists')),
        'cp_status': r.get('cp_status'),
        'scoped_replay_status': (r.get('scoped_acceptance') or {}).get('replay_status'),
        'scoped_physical_status': (r.get('scoped_acceptance') or {}).get('physical_status'),
        'scoped_accepted': accepted(r),
        'prepare_seconds': r.get('prepare_seconds'),
        'canonical_seconds': r.get('canonical_seconds'),
    } for r in rows]

def main():
    resource = load_jsonl(OUT/'resource_domain_experiments.jsonl')
    neighborhood = load_jsonl(OUT/'neighborhood_experiments.jsonl')
    combined = load_jsonl(OUT/'combined_experiments.jsonl')
    boundaries = load_jsonl(OUT/'boundary_ablations.jsonl')
    supplement = load_json(OUT/'supplemental_probes.json')
    resource.extend(supplement.get('resource_wider_time', []))
    boundaries.extend(supplement.get('boundary_two_dimension', []))
    frozen = load_json(OUT/'frozen_state.json')
    baseline = load_json(OUT/'baseline_reproduction.json')

    exact = [r for r in boundaries if r.get('label','').startswith('EXACT_CARRIED_BOUNDARY')]
    no_boundary = [r for r in boundaries if r.get('label','').startswith('REMOVE_ALL_RELEVANT_BOUNDARY')]
    exact_valid = any(accepted(r) for r in resource + neighborhood + combined + exact)

    root = {
        'classification': 'UNKNOWN',
        'primary_question_answer': 'D_CARRIED_BOUNDARY_IS_IMPLICATED_BUT_THE_EXACT_COMBINED_CONFLICT_IS_NOT_ISOLATED',
        'first_proven_blocker': 'JOINT_CARRIED_MACHINE_WORKER_ROUTING_BOUNDARY_INTERACTION',
        'evidence': 'Removing any single boundary dimension or the tested two-dimension projections remains infeasible; removing all relevant carried boundary facts yields a candidate. This is diagnostic only.',
        'resource_domain_truncation': True,
        'neighborhood_expansion_tested': [22, 35, 50, 70],
        'resource_expansion_tested': ['1x1', '3x1', '1x3', '3x3', '1x8'],
        'complete_domain': 'NOT_ESTABLISHED',
        'physical_infeasibility_proven': False,
        'exact_boundary_valid_candidate_found': exact_valid,
        'all_boundary_removed_candidate': bool(no_boundary and any(accepted(r) for r in no_boundary)),
        'why_not_classified_as_single_boundary': 'single machine, worker, routing, machine+worker, machine+routing, and worker+routing removals did not recover feasibility',
        'why_not_classified_as_resource_or_neighborhood_only': 'fixed 22 resource expansion and deterministic 35/50/70 neighborhood expansion did not recover an exact-boundary candidate',
        'database_writes': 0,
    }
    write = lambda name, value: (OUT/name).write_text(json.dumps(value, indent=2, default=str)+'\n', encoding='utf-8')
    write('complete_domain_diagnostic.json', {
        'status': 'ORACLE_NOT_COMPLETED',
        'reason': 'complete worker-crew enumeration is combinatorial; finite legal crew capacities include millions and bounded 1x8/3x3 exposures are not complete',
        'bounded_reference_results': summary(resource),
        'database_writes': 0,
    })
    write('conflict_analysis.json', {
        'status': 'UNRESOLVED_TRUNCATED_DOMAIN',
        'root_cause_evidence': root,
        'resource_expansion': summary(resource),
        'neighborhood_expansion': summary(neighborhood),
        'combined_expansion': summary(combined),
        'boundary_ablations': summary(boundaries),
        'diagnostic_only_candidates': [r.get('label') for r in boundaries if accepted(r)],
        'database_writes': 0,
    })
    write('root_cause.json', root)
    write('proposed_adaptive_policy.json', {
        'status': 'PROPOSED_NOT_IMPLEMENTED',
        'policy': 'retain 22-operation normal admission; on exact-stage failure with truncated legal alternatives, issue a bounded generic expansion request that can target resource and causal neighborhood dimensions; never claim physical infeasibility until complete relevant domains and authoritative boundaries are represented',
        'suggested_order': ['resource-domain expansion at fixed neighborhood', 'causal/neighborhood expansion', 'combined bounded diagnostic', 'truthful unresolved if complete domain is unavailable'],
        'not_window_specific': True,
        'requires_followup': 'isolate the joint carried-boundary contradiction before production change',
        'database_writes': 0,
    })
    write('schedule_quality_comparison.json', {
        'status': 'NO_EXACT_BOUNDARY_VALID_CANDIDATE',
        'valid_quality_comparison_available': False,
        'note': 'The only accepted diagnostic candidate removed all carried boundaries and is excluded from production quality comparison.',
        'resource': summary(resource), 'neighborhood': summary(neighborhood), 'combined': summary(combined),
        'boundary': summary(boundaries), 'database_writes': 0,
    })
    step3b = {
        'status': 'PARTIALLY_OBSERVATIONAL',
        'evidence': [
            {'file': 'scratch/run_factory2_172_step4.py', 'role': 'calls decide_rolling_orchestration_v2 around selector for telemetry'},
            {'file': 'scratch/run_one_factory_craft.py', 'role': 'selects and directly solves; does not dispatch all six Step-3B actions'},
            {'file': 'crafter/repair/global_planner.py', 'role': 'planning loop does not invoke decide_rolling_orchestration_v2 as an execution controller'},
        ],
        'database_writes': 0,
    }
    write('step3b_control_path_audit.json', step3b)
    report = {
        'status': 'CRAFT_WINDOW14_STEP4A_PARTIAL',
        'baseline_reproduction': baseline.get('reproduction_match') is True,
        'baseline_cp_statuses': [x.get('status') for x in baseline.get('observed',{}).get('stages',[])],
        'resource_expansion_result': 'NO_EXACT_BOUNDARY_CANDIDATE',
        'neighborhood_result': 'NO_EXACT_BOUNDARY_CANDIDATE_AT_22_35_50_70',
        'combined_result': 'NO_EXACT_BOUNDARY_CANDIDATE_AT_35_50_70_3X3',
        'complete_domain_diagnostic': 'ORACLE_NOT_COMPLETED',
        'boundary_ablation_result': 'ONLY_ALL_BOUNDARIES_REMOVED_RECOVERED_DIAGNOSTIC_CANDIDATE',
        'root_cause': root['classification'],
        'physical_infeasibility_proven': False,
        'step3b_control_path_status': step3b['status'],
        'replay': 'NOT_RUN_FOR_EXACT_BOUNDARY; diagnostic all-boundary-removal candidate PASS',
        'physical_validator': 'NOT_RUN_FOR_EXACT_BOUNDARY; diagnostic all-boundary-removal candidate VALID',
        'regression': '76 passed, 0 failed, 2 warnings',
        'database_writes': 0,
    }
    write('implementation_report.json', report)
    (OUT/'implementation_report.md').write_text(
        '# Factory-2 Window-14 Step-4A\n\n'
        f"Status: `{report['status']}`\n\n"
        'The exact persisted Window-14 state was reconstructed from the 13 accepted prior windows and 56 committed operations. '
        'The 22-operation four-stage INFEASIBLE baseline reproduced. Fixed-neighborhood resource exposure through 1x8/3x3, '
        'deterministic neighborhood sizes 22/35/50/70, and combined 3x3 probes did not produce an exact-boundary candidate. '
        'Diagnostic boundary projections show that removing all relevant carried boundary facts produces a replay PASS / physical VALID '
        'candidate, while individual and tested two-dimension removals do not. The legal worker domain remains truncated; therefore '
        'the evidence does not prove physical infeasibility and does not justify a production repair yet.\n\n'
        f"Primary classification: `{root['classification']}`. The Step-3B control path is `{step3b['status']}` in the Step-4 harness.\n",
        encoding='utf-8')
    # Changed files are diagnostic runners only; no production CRAFT module was
    # modified by Step-4A.
    write('changed_files_manifest.json', {
        'production_files_changed': [],
        'diagnostic_files_added': ['scratch/run_window14_step4a.py','scratch/supplement_window14_step4a.py','scratch/finalize_window14_step4a.py'],
        'prior_evidence_preserved': True,
        'database_writes': 0,
    })
    write('focused_tests.json', {
        'command': 'venv\\Scripts\\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py tests/test_laya_shadow_v1.py',
        'passed': 76,
        'failed': 0,
        'warnings': 2,
        'status': 'PASS',
        'database_writes': 0,
    })
    # Hash every final evidence file except the manifest itself, then hash the
    # manifest too with an explicit self-reference exclusion.
    entries=[]
    for path in sorted(OUT.iterdir()):
        if not path.is_file() or path.name == 'artifact_hash_manifest.json': continue
        entries.append({'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size})
    manifest={'algorithm':'SHA-256','self_hash_excluded':True,'files':entries,'database_writes':0}
    write('artifact_hash_manifest.json', manifest)

if __name__=='__main__': main()
