"""V2 attempt accounting for bounded retries; owns no scheduling semantics."""
from copy import deepcopy

from .cycles import seal, policy, _accepted_evidence


def create(*, factory_id, input_fingerprint, operation_keys, options):
    keys = list(operation_keys)
    if not keys or len(keys) != len(set(keys)) or not input_fingerprint:
        raise ValueError('CYCLE_OPERATION_IDENTITY')
    opts = policy(options)
    pending = []
    index = 0
    while index < len(keys):
        cycle = index // opts['cycle_target'] + 1
        end = min(len(keys), index + opts['window_size'], cycle * opts['cycle_target'])
        pending.append({'keys': keys[index:end], 'cycle_number': cycle})
        index = end
    return seal({'contract': 'CRAFT_CYCLE_CHECKPOINT_V2', 'factory_id': factory_id,
                 'input_fingerprint': input_fingerprint, 'policy': opts,
                 'operation_keys': keys, 'windows': [], 'pending_windows': pending,
                 'accepted_keys': [], 'deferred': {}, 'revision': 0, 'status': 'PENDING',
                 'approval_status': 'TENTATIVE_NOT_APPROVED'})


def record(checkpoint, window, *, independent_status):
    cp = deepcopy(checkpoint)
    if not cp['pending_windows']:
        raise ValueError('CYCLE_NO_PENDING_WINDOW')
    head = cp['pending_windows'].pop(0)
    row = deepcopy(window)
    if list(row['keys']) != head['keys']:
        raise ValueError('CYCLE_RETRY_ORDER_OR_IDENTITY')
    row.update(number=len(cp['windows']) + 1, cycle_number=head['cycle_number'])
    row['keys'] = head['keys']
    keys = row['keys']
    if set(keys) & (set(cp['accepted_keys']) | set(cp['deferred'])):
        raise ValueError('CYCLE_DUPLICATE_OR_INVALID_WINDOW')
    if row['status'] == 'ACCEPTED':
        _accepted_evidence(row)
        if independent_status != 'PASS':
            raise ValueError('CYCLE_INDEPENDENT_VALIDATION_REQUIRED')
        cp['accepted_keys'] = sorted(cp['accepted_keys'] + keys)
        row['independent_status'] = 'PASS'
    elif row['status'] == 'REJECTED':
        if row.get('selected_schedule'):
            raise ValueError('CYCLE_REJECTED_ASSIGNMENTS')
        row['failure_reason'] = row.get('failure_reason') or 'BOUNDED_ATTEMPT_LIMIT_NOT_PHYSICAL_PROOF'
        row['independent_status'] = 'NOT_ACCEPTED'
        if len(keys) > 1:
            size = 2 if len(keys) > 2 else 1
            cp['pending_windows'][0:0] = [
                {'keys': keys[i:i + size], 'cycle_number': head['cycle_number']}
                for i in range(0, len(keys), size)]
            row['recovery'] = 'RETRY_SMALLER_WINDOW'
        else:
            cp['deferred'][keys[0]] = row['failure_reason']
            row['recovery'] = 'TERMINAL_DEFERRED'
    else:
        raise ValueError('CYCLE_ATTEMPT_STATUS')
    cp['windows'].append(row)
    cp['revision'] += 1
    cp['status'] = ('PENDING' if cp['pending_windows'] else
                    'COMPLETE' if set(cp['accepted_keys']) == set(cp['operation_keys']) else 'PARTIAL')
    return seal(cp)


def verify(checkpoint, **identity):
    if checkpoint != seal(checkpoint):
        raise ValueError('CYCLE_CHECKPOINT_HASH')
    replayed = create(**identity)
    for field in ('contract', 'factory_id', 'input_fingerprint', 'operation_keys', 'policy'):
        if checkpoint.get(field) != replayed[field]:
            raise ValueError('CYCLE_CHECKPOINT_STALE')
    for window in checkpoint['windows']:
        replayed = record(replayed, window, independent_status=window.get('independent_status'))
    if replayed != checkpoint:
        raise ValueError('CYCLE_ATTEMPT_LEDGER_MISMATCH')
    return True
