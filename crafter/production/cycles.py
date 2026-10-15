"""Deterministic cycle accounting around bounded CRAFT windows (no optimizer).

Checkpoints contain tentative planning decisions, never factory actuals or
manager approval. The caller must independently validate before committing a
window and persist the returned JSON atomically with its job lease.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


def seal(value):
    body = {k: v for k, v in value.items() if k != 'fingerprint'}
    return {**body, 'fingerprint': digest(body)}


def policy(value=None):
    result = {'cycle_target': 200, 'window_size': 4}
    if value and 'adaptive_retries' in value:
        if type(value['adaptive_retries']) is not bool:
            raise ValueError('CYCLE_POLICY_INVALID')
        result['adaptive_retries'] = value['adaptive_retries']
    if value:
        if set(value) - set(result):
            raise ValueError('CYCLE_POLICY_UNKNOWN_FIELD')
        result.update(value)
    if any(type(result[k]) is not int or result[k] < 1 for k in ('cycle_target', 'window_size')):
        raise ValueError('CYCLE_POLICY_INVALID')
    if result['window_size'] > result['cycle_target'] or result['window_size'] > 25:
        raise ValueError('CYCLE_WINDOW_LIMIT')
    return result


def new_checkpoint(*, factory_id, input_fingerprint, operation_keys, options=None):
    if (options or {}).get('adaptive_retries'):
        from .adaptive_cycles import create
        return create(factory_id=factory_id, input_fingerprint=input_fingerprint,
                      operation_keys=operation_keys, options=options)
    keys = list(operation_keys)
    if not keys or len(set(keys)) != len(keys) or not input_fingerprint:
        raise ValueError('CYCLE_OPERATION_IDENTITY')
    return seal({'contract': 'CRAFT_CYCLE_CHECKPOINT_V1', 'factory_id': factory_id,
                 'input_fingerprint': input_fingerprint, 'policy': policy(options),
                 'operation_keys': keys, 'windows': [], 'accepted_keys': [],
                 'deferred': {}, 'approval_status': 'TENTATIVE_NOT_APPROVED',
                 'status': 'PENDING', 'revision': 0})


def verify_checkpoint(checkpoint, *, factory_id, input_fingerprint, operation_keys, options=None):
    if (options or {}).get('adaptive_retries'):
        from .adaptive_cycles import verify
        return verify(checkpoint, factory_id=factory_id, input_fingerprint=input_fingerprint,
                      operation_keys=operation_keys, options=options)
    if checkpoint != seal(checkpoint):
        raise ValueError('CYCLE_CHECKPOINT_HASH')
    if (checkpoint.get('contract') != 'CRAFT_CYCLE_CHECKPOINT_V1' or
            checkpoint.get('factory_id') != factory_id or
            checkpoint.get('input_fingerprint') != input_fingerprint or
            checkpoint.get('operation_keys') != list(operation_keys) or
            checkpoint.get('policy') != policy(options)):
        raise ValueError('CYCLE_CHECKPOINT_STALE')
    if checkpoint.get('approval_status') != 'TENTATIVE_NOT_APPROVED':
        raise ValueError('CYCLE_CHECKPOINT_NOT_APPROVAL')
    accepted, attempted = [], []
    for number, row in enumerate(checkpoint['windows'], 1):
        if row['number'] != number or not row['keys'] or len(row['keys']) > checkpoint['policy']['window_size']:
            raise ValueError('CYCLE_WINDOW_ACCOUNTING')
        attempted.extend(row['keys'])
        if row['status'] == 'ACCEPTED':
            _accepted_evidence(row)
            accepted.extend(row['keys'])
        elif not row.get('failure_reason'):
            raise ValueError('CYCLE_FAILURE_REASON_MISSING')
    if (len(attempted) != len(set(attempted)) or not set(attempted) <= set(operation_keys) or
            sorted(accepted) != checkpoint['accepted_keys'] or
            set(accepted) & set(checkpoint['deferred']) or
            not set(checkpoint['deferred']) <= set(operation_keys) or
            checkpoint['revision'] != len(checkpoint['windows'])):
        raise ValueError('CYCLE_OPERATION_ACCOUNTING')
    return True


def _accepted_evidence(window):
    rows = window.get('selected_schedule', [])
    if (window.get('replay') != 'PASS' or window.get('physical') != 'VALID' or
            window.get('scope') != 'SCOPE_VALID' or
            window.get('materialization_mismatch_count') != 0 or
            len(rows) != len(window['keys']) or
            {r['operation_key'] for r in rows} != set(window['keys'])):
        raise ValueError('CYCLE_WINDOW_NOT_VALIDATED')


def next_window(checkpoint, *, predecessors, completed_or_approved=()):
    """Precedence-safe bounded selection; failed routes cannot starve other work.

Input order is a stable ranking supplied by the source adapter. Only genuinely
completed/approved predecessors or previously validated tentative work release
a route. No reservation is fabricated for future unscheduled demand.
"""
    if checkpoint.get('contract') == 'CRAFT_CYCLE_CHECKPOINT_V2':
        selected = tuple(checkpoint['pending_windows'][0]['keys']) if checkpoint['pending_windows'] else ()
        available = set(checkpoint['accepted_keys']) | set(completed_or_approved)
        for key in selected:
            if predecessors.get(key) is not None and predecessors[key] not in available:
                raise ValueError('ADAPTIVE_WINDOW_PREDECESSOR_UNRESOLVED')
            available.add(key)
        return deepcopy(checkpoint), selected
    cp = deepcopy(checkpoint)
    accepted = set(cp['accepted_keys'])
    available = accepted | set(completed_or_approved)
    pending = [k for k in cp['operation_keys'] if k not in accepted and k not in cp['deferred']]
    target = cp['policy']['cycle_target']
    used = sum(len(w['keys']) for w in cp['windows'])
    capacity = min(cp['policy']['window_size'], target - used % target)
    selected = []
    while pending and len(selected) < capacity:
        ready = next((k for k in pending if predecessors.get(k) is None or
                      predecessors[k] in available), None)
        if ready is None:
            break
        selected.append(ready)
        available.add(ready)
        pending.remove(ready)
    if not selected:
        for key in pending:
            cp['deferred'][key] = 'PREDECESSOR_UNRESOLVED:' + str(predecessors.get(key))
        cp['status'] = 'COMPLETE' if accepted == set(cp['operation_keys']) else 'PARTIAL'
        return seal(cp), ()
    cp['status'] = 'PENDING'
    return seal(cp), tuple(selected)


def record_window(checkpoint, window, *, independent_status):
    if checkpoint.get('contract') == 'CRAFT_CYCLE_CHECKPOINT_V2':
        from .adaptive_cycles import record
        return record(checkpoint, window, independent_status=independent_status)
    cp = deepcopy(checkpoint)
    keys = list(window['keys'])
    prior = {k for w in cp['windows'] for k in w['keys']}
    if (prior & set(keys) or not set(keys) <= set(cp['operation_keys']) or
            len(keys) != len(set(keys)) or not keys or len(keys) > cp['policy']['window_size']):
        raise ValueError('CYCLE_DUPLICATE_OR_INVALID_WINDOW')
    row = deepcopy(window)
    row['keys'] = keys
    row['number'] = len(cp['windows']) + 1
    row['cycle_number'] = 1 + sum(len(w['keys']) for w in cp['windows']) // cp['policy']['cycle_target']
    if row['status'] == 'ACCEPTED':
        _accepted_evidence(row)
        if independent_status != 'PASS':
            raise ValueError('CYCLE_INDEPENDENT_VALIDATION_REQUIRED')
        cp['accepted_keys'] = sorted(set(cp['accepted_keys']) | set(keys))
    else:
        reason = row.get('failure_reason') or 'BOUNDED_WINDOW_NOT_ACCEPTED_NOT_PHYSICAL_PROOF'
        row['failure_reason'] = reason
        cp['deferred'].update({key: reason for key in keys})
    cp['windows'].append(row)
    cp['revision'] += 1
    cp['status'] = 'COMPLETE' if set(cp['accepted_keys']) == set(cp['operation_keys']) else 'PENDING'
    return seal(cp)
