"""Application-owned approvals over immutable, explicitly hypothetical captures."""
import hashlib
import json
from pathlib import Path
from .global_resource_state import assignment_from_row, digest, seal, verify

ROOT = Path(__file__).resolve().parents[4]
CAPTURE = ROOT / 'web_v1/audits/factory_source_integration_v1/capture_20261012T223420800478Z'
CONTRACT = 'CAPTURE_GOVERNED_TEST_V1'
SOURCE = 'PINNED_CAPTURE_TEST_ONLY_NOT_PRODUCTION'


def base_for(scope):
    base = scope.resource_state.get('capture_state')
    verify(base)
    if (scope.factory_external_id not in range(1, 7) or
            base['factory_id'] != scope.factory_external_id or
            scope.resource_state.get('classification') != 'SIMULATED_TEST' or
            scope.resource_state.get('production_admission_enabled') is not False):
        raise ValueError('CAPTURE_TEST_ISOLATION')
    path = CAPTURE / f'factory{scope.factory_external_id}_source.json'
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = json.loads((CAPTURE / 'manifest.json').read_text())
    if actual != base['source_hash'] or actual != manifest[path.name]:
        raise ValueError('CAPTURE_SOURCE_CHANGED')
    return base


def recipe(scope):
    base = base_for(scope)
    keys = scope.resource_state.get('selected_keys', ())
    if not keys or len(keys) != len(set(keys)) or not set(keys) <= set(base['selected_keys']):
        raise ValueError('CAPTURE_SELECTION_OUTSIDE_PREPARED_DOMAIN')
    return {'contract': CONTRACT, 'source': SOURCE, 'factory_id': scope.factory_external_id,
            'source_fingerprint': base['source_hash'], 'scenario_hash': base['scenario_hash'],
            'selected_keys': list(keys), 'capture_base_fingerprint': base['fingerprint'],
            'cycle_target': 200, 'window_size': 5, 'adaptive_retries': True}


def export_state(scope, keys):
    base = base_for(scope)
    if not set(keys) <= set(base['selected_keys']):
        raise ValueError('CAPTURE_SELECTION_OUTSIDE_PREPARED_DOMAIN')
    active = scope.active_version
    if active and (active.status != 'ACTIVE' or active.payload_hash != digest(
            {'operations': active.operations, 'evidence': active.evidence})):
        raise ValueError('CAPTURE_ACTIVE_PAYLOAD_CHANGED')
    rows = active.operations if active else []
    if any(row['operation_key'] in keys for row in rows):
        raise ValueError('CAPTURE_APPROVED_OPERATION_IN_SCOPE')
    approved = [{**assignment_from_row(row, scope.factory_external_id),
                 'approved_version_id': str(active.pk)} for row in rows]
    packet = {k: v for k, v in base.items() if k != 'fingerprint'}
    dispositions = dict(base['schedule_disposition'])
    reasons = dict(base['disposition_reasons'])
    for key in base['selected_keys']:
        dispositions[key] = 'DEFERRED'
        reasons[key] = 'OUTSIDE_CURRENT_REQUEST_UNCOMMITTED_TEST_DEMAND'
    for row in approved:
        dispositions[row['operation_key']] = 'APPROVED'
        reasons.pop(row['operation_key'], None)
    for key in keys:
        dispositions[key] = 'SELECTED'
        reasons.pop(key, None)
    packet.update(selected_keys=list(keys), approved_outside=approved,
                  approved_rows=rows, active_version_id=str(active.pk) if active else None,
                  resource_revision=scope.resource_revision,
                  schedule_disposition=dispositions, disposition_reasons=reasons)
    packet = seal(packet)
    verify(packet)
    return packet


def verify_progress(checkpoint, previous, snapshot):
    if (not isinstance(checkpoint, dict) or
            checkpoint.get('fingerprint') != digest({k: v for k, v in checkpoint.items() if k != 'fingerprint'}) or
            checkpoint.get('contract') != 'CRAFT_TENTATIVE_CHECKPOINT_V1' or
            checkpoint.get('approval_status') != 'TENTATIVE_NOT_APPROVED'):
        raise ValueError('CAPTURE_CHECKPOINT_INTEGRITY')
    identity, payload = checkpoint['identity'], checkpoint['payload']
    if (identity['factory_id'] != snapshot['factory_id'] or
            identity['source_sha256'] != snapshot['source_fingerprint'] or
            identity['approval_fingerprint'] != snapshot['global_resource_state']['fingerprint'] or
            set(identity['selected_keys']) != set(snapshot['selected_keys'])):
        raise ValueError('CAPTURE_CHECKPOINT_STALE')
    accepted = [r['operation_key'] for r in payload['selected_schedule']]
    pending = [k for w in payload['pending_windows'] for k in w]
    all_keys = accepted + pending + list(payload['deferred_operations'])
    recorded = [k for w in payload['windows'] if w['accepted'] for k in w['operation_keys']]
    if (len(all_keys) != len(set(all_keys)) or set(all_keys) != set(snapshot['selected_keys']) or
            sorted(recorded) != sorted(accepted) or
            any(not 1 <= len(w) <= 25 for w in payload['pending_windows'])):
        raise ValueError('CAPTURE_CHECKPOINT_ACCOUNTING')
    if previous:
        old = previous['payload']
        if (identity != previous['identity'] or payload['attempt_number'] != old['attempt_number'] + 1 or
                payload['windows'][:len(old['windows'])] != old['windows'] or
                payload['selected_schedule'][:len(old['selected_schedule'])] != old['selected_schedule']):
            raise ValueError('CAPTURE_CHECKPOINT_INVALID_PROGRESS')
    elif payload['attempt_number'] != 1:
        raise ValueError('CAPTURE_CHECKPOINT_INVALID_PROGRESS')
