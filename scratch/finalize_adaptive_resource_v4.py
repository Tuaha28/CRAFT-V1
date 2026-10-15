import hashlib
import json
import shutil
from pathlib import Path

SRC = Path('outputs/crafter/p2_adaptive_resource_expansion_v3')
OUT = Path('outputs/crafter/p2_adaptive_resource_expansion_v4')
OUT.mkdir(parents=True, exist_ok=True)

def load(name):
    return json.loads((SRC / name).read_text())

def put(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

for name in [
    'resource_expansion_baseline.json', 'resource_conflict_pressure.json',
    'causal_closure_trace.json', 'calendar_regime_summary.json',
    'calendar_regime_equivalence.json', 'expanded_resource_domain.json',
    'cp_model_summary.json', 'cp_result.json', 'selected_schedule.json',
    'materialization_equivalence.json', 'replay_result.json',
    'validator_result.json', 'expanded_domain_family_isolation.json']:
    shutil.copyfile(SRC / name, OUT / name)

history = load('resource_expansion_history.json')
compact = []
for row in history:
    item = {key: row[key] for key in (
        'iteration', 'operations_expanded', 'reason', 'configs_before',
        'configs_added', 'configs_after', 'regimes_before', 'regimes_after', 'cp'
    ) if key in row}
    if 'equivalence' in row:
        item['equivalence'] = {
            'mismatches': row['equivalence'].get('mismatches', 0),
            'all_equivalent': row['equivalence'].get('all_equivalent'),
            'checks': len(row['equivalence'].get('checks', [])),
        }
    compact.append(item)
put('resource_expansion_history.json', compact)

baseline = {row['operation_key']: row for row in load('resource_expansion_baseline.json')['operations']}
domain = load('expanded_resource_domain.json')
movable_states = {'FUTURE_RELEASED', 'FUTURE_PLANNED', 'FUTURE', 'BLOCKED'}
audit = []
for key in sorted(domain):
    before = baseline[key]
    movable = before.get('lifecycle_state') in movable_states
    audit.append({
        'operation_key': key,
        'lifecycle_state': before.get('lifecycle_state'),
        'eligible_lines': before.get('eligible_lines', []),
        'legal_k_by_line': before.get('legal_k_by_line', {}),
        'resource_configuration_count': len(domain[key]),
        'incumbent_configuration_present': before.get('incumbent_configuration_present', False),
        'more_machine_sets_available': bool(before.get('more_machine_sets_available')) if movable else False,
        'more_worker_crews_available': bool(before.get('more_worker_crews_available')) if movable else False,
        'additional_legal_configurations_available': bool(before.get('truncated')) if movable else False,
        'truncated': bool(before.get('truncated')) if movable else False,
        'expansion_policy': 'MOVABLE_ONLY' if movable else 'IMMUTABLE_CONTEXT',
    })
put('expanded_resource_audit.json', audit)

status = 'CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE'
put('root_cause_classification.json', {
    'status': status,
    'region_size': len(domain),
    'resource_domain_complete': False,
    'physical_impossibility_proven': False,
    'database_writes': 0,
})
(OUT / 'final_report.md').write_text(
    '# Adaptive P2 resource expansion\n\n'
    f'Primary status: `{status}`\n\n'
    'The event-derived causal region remained 9 operations. Two bounded '
    'movable-only expansion rounds increased the timing-independent physical '
    'domain from 117 to 414 configurations and to 34,786 exact '
    'CalendarExecutionRegimes; regime equivalence remained exact. The '
    'frozen-near-term context operation `1029_292` was not expanded.\n\n'
    'The canonical CP remained `INFEASIBLE` in presolve. Every movable '
    'operation still has additional legally screened candidates flagged as '
    'truncated, so complete-model infeasibility and physical impossibility '
    'are not proven. No schedule was selected; materialization, replay, and '
    'validator were not reached.\n\n'
    'No worker/machine/shift semantics changed, no handover or floaters were '
    'introduced, and database writes were 0.\n', encoding='utf-8')
files = {
    str(path.relative_to(OUT)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(OUT.rglob('*'))
    if path.is_file() and path.name != 'artifact_hash_manifest.json'
}
put('artifact_hash_manifest.json', {
    'files': files,
    'artifact_count': len(files),
    'status': status,
    'database_writes': 0,
    'source_artifact': str(SRC),
})
