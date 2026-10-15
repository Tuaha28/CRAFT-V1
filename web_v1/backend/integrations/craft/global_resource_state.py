"""Pure, TEST-safe resource snapshot and independent outside-conflict gate.

Offsets are integer seconds from reference_time. A proposed schedule is never
a commitment; only validated fixed evidence and the application ACTIVE version
enter the immutable snapshot. No database or solver imports belong here.
"""
from __future__ import annotations

import hashlib
import json


class GlobalStateError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def seal(packet):
    return {**packet, 'fingerprint': digest(packet)}


def verify(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('contract') != 'GLOBAL_RESOURCE_STATE_V1':
        raise GlobalStateError('GLOBAL_STATE_CONTRACT')
    if snapshot.get('classification') not in ('APPLICATION_APPROVED_TEST', 'SIMULATED_TEST'):
        raise GlobalStateError('GLOBAL_STATE_CLASSIFICATION')
    if snapshot.get('fingerprint') != digest({k: v for k, v in snapshot.items() if k != 'fingerprint'}):
        raise GlobalStateError('GLOBAL_STATE_FINGERPRINT')
    if not isinstance(snapshot.get('factory_id'), int) or snapshot['factory_id'] < 1:
        raise GlobalStateError('GLOBAL_STATE_FACTORY')
    if (not snapshot.get('reference_time') or not snapshot.get('timezone') or
            not snapshot.get('source_hash') or not snapshot.get('source_identity') or
            snapshot.get('calendar_revision') is None or snapshot.get('eligibility_revision') is None):
        raise GlobalStateError('GLOBAL_STATE_PROVENANCE')
    if len(set(snapshot.get('selected_keys', ()))) != len(snapshot.get('selected_keys', ())) or not snapshot.get('selected_keys'):
        raise GlobalStateError('GLOBAL_STATE_SELECTION')
    ledger = snapshot.get('operation_ledger', {})
    if not ledger or (set(ledger) != set(snapshot.get('execution_states', {})) or
                      set(ledger) != set(snapshot.get('schedule_disposition', {})) or
                      set(ledger) != set(snapshot.get('order_identities', {}))):
        raise GlobalStateError('GLOBAL_STATE_OPERATION_ACCOUNTING')
    for key, row in ledger.items():
        if not row or any(not isinstance(row.get(name), int) or row[name] < 0
                          for name in ('required', 'completed', 'in_flight', 'remaining')):
            raise GlobalStateError('GLOBAL_STATE_QUANTITY_LEDGER')
        if row['required'] != row['completed'] + row['in_flight'] + row['remaining']:
            raise GlobalStateError('GLOBAL_STATE_QUANTITY_LEDGER')
        disposition = snapshot['schedule_disposition'][key]
        if (not snapshot['execution_states'][key] or not snapshot['order_identities'][key] or
                disposition not in ('COMPLETED', 'EXECUTING', 'APPROVED', 'SELECTED',
                                    'PROPOSED', 'UNSCHEDULED', 'DEFERRED', 'BLOCKED') or
                (key in snapshot['selected_keys']) != (disposition == 'SELECTED')):
            raise GlobalStateError('GLOBAL_STATE_OPERATION_STATE')
        if disposition in ('DEFERRED', 'BLOCKED') and not snapshot.get('disposition_reasons', {}).get(key):
            raise GlobalStateError('GLOBAL_STATE_DISPOSITION_REASON')
    for key in snapshot['selected_keys']:
        if ledger[key]['remaining'] == 0:
            raise GlobalStateError('GLOBAL_STATE_QUANTITY_LEDGER')
        if key not in snapshot.get('route_edges', {}):
            raise GlobalStateError('GLOBAL_STATE_ROUTE_GRAPH_MISSING')
    if not isinstance(snapshot.get('approved_outside'), list) or not isinstance(snapshot.get('fixed_commitments'), list):
        raise GlobalStateError('GLOBAL_STATE_COMMITMENT_SHAPE')
    for row in snapshot.get('approved_outside', ()) + snapshot.get('fixed_commitments', ()):
        _interval(row)
        if row.get('factory_id') != snapshot['factory_id']:
            raise GlobalStateError('GLOBAL_STATE_CROSS_FACTORY')
        if snapshot.get('machine_lines', {}).get(str(row['machine_id'])) != row.get('line_id'):
            raise GlobalStateError('GLOBAL_STATE_MACHINE_LINE')
        if any(seg[0] not in snapshot.get('worker_ids', ()) for seg in row['worker_segments']):
            raise GlobalStateError('GLOBAL_STATE_WORKER_FACTORY')
    for row in snapshot['fixed_commitments']:
        if not row.get('provenance'):
            raise GlobalStateError('GLOBAL_STATE_FIXED_EVIDENCE_MISSING')
    fixed = snapshot.get('approved_outside', ()) + snapshot.get('fixed_commitments', ())
    approved_keys = [row.get('operation_key') for row in snapshot['approved_outside']]
    if len(approved_keys) != len(set(approved_keys)):
        raise GlobalStateError('GLOBAL_STATE_DUPLICATE_APPROVED_OPERATION')
    for row in snapshot['approved_outside']:
        key = row.get('operation_key')
        if (key in snapshot['selected_keys'] or row.get('approved_version_id') != snapshot.get('active_version_id') or
                snapshot.get('schedule_disposition', {}).get(key) != 'APPROVED'):
            raise GlobalStateError('GLOBAL_STATE_APPROVAL_PROVENANCE')
        allowed = snapshot.get('eligibility', {}).get(key)
        if not allowed or (row['machine_id'] not in allowed.get('machines', ()) or
                           row.get('line_id') not in allowed.get('lines', ()) or
                           any(seg[0] not in allowed.get('workers', ()) for seg in row['worker_segments'])):
            raise GlobalStateError('GLOBAL_STATE_APPROVED_INELIGIBLE')
        windows = snapshot.get('calendar_windows', {}).get(key)
        if not windows or not any(a <= row['start_seconds'] and row['end_seconds'] <= b for a, b in windows):
            raise GlobalStateError('GLOBAL_STATE_APPROVED_OFF_CALENDAR')
        release = snapshot.get('route_releases', {}).get(key)
        if release is None or row['start_seconds'] < release:
            raise GlobalStateError('GLOBAL_STATE_APPROVED_ROUTE')
    for i, a in enumerate(fixed):
        for b in fixed[i + 1:]:
            if a['machine_id'] == b['machine_id'] and _overlap(
                    (a['start_seconds'], a['end_seconds']), (b['start_seconds'], b['end_seconds'])):
                raise GlobalStateError('GLOBAL_STATE_EXISTING_MACHINE_CONFLICT')
            if any(aw == bw and _overlap((aa, ab), (ba, bb))
                   for aw, aa, ab in a['worker_segments'] for bw, ba, bb in b['worker_segments']):
                raise GlobalStateError('GLOBAL_STATE_EXISTING_WORKER_CONFLICT')
    return True


def _interval(row):
    if not isinstance(row.get('start_seconds'), int) or not isinstance(row.get('end_seconds'), int):
        raise GlobalStateError('GLOBAL_STATE_INTERVAL')
    if row['start_seconds'] < 0 or row['end_seconds'] <= row['start_seconds']:
        raise GlobalStateError('GLOBAL_STATE_INTERVAL')
    if not isinstance(row.get('machine_id'), int) or not isinstance(row.get('worker_segments'), list):
        raise GlobalStateError('GLOBAL_STATE_RESOURCES')
    for segment in row['worker_segments']:
        if (not isinstance(segment, list) or len(segment) != 3 or
                not isinstance(segment[0], str) or not all(isinstance(n, int) for n in segment[1:]) or
                segment[1] < row['start_seconds'] or segment[2] > row['end_seconds'] or
                segment[2] <= segment[1]):
            raise GlobalStateError('GLOBAL_STATE_WORKER_SEGMENT')


def assignment_from_row(row, factory_id):
    cfg = row.get('resource_configuration') or {}
    if cfg.get('factory_id') != factory_id:
        raise GlobalStateError('CROSS_FACTORY_ASSIGNMENT')
    mids, wids = cfg.get('machine_ids', ()), cfg.get('worker_ids', ())
    if len(mids) != 1 or not wids:
        raise GlobalStateError('UNSUPPORTED_APPROVED_RESOURCE_SHAPE')
    segments = [[str(wid), int(a), int(b)] for wid in wids for a, b in row.get('worker_segments', ())]
    result = {'factory_id': factory_id, 'operation_key': row['operation_key'],
              'machine_id': int(mids[0]), 'worker_segments': segments,
              'start_seconds': int(row['start_seconds']), 'end_seconds': int(row['end_seconds']),
              'line_id': int(cfg['line_id'])}
    _interval(result)
    return result


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def validate_candidate(snapshot, rows):
    """Check saved materialization against independent versioned commitments.

    This is additional to CRAFT replay/physical/scope checks, not their replacement.
    Missing eligibility/calendar/route evidence fails closed for this contract.
    """
    verify(snapshot)
    selected = set(snapshot['selected_keys'])
    seen = set()
    intervals = []
    for row in snapshot['fixed_commitments'] + snapshot['approved_outside']:
        intervals.append((row, 'OUTSIDE'))
    for row in rows:
        key = row.get('operation_key')
        if key not in selected or key in seen:
            raise GlobalStateError('GLOBAL_STATE_OPERATION_ACCOUNTING')
        seen.add(key)
        item = assignment_from_row(row, snapshot['factory_id'])
        allowed = snapshot.get('eligibility', {}).get(key)
        if not allowed:
            raise GlobalStateError('GLOBAL_STATE_ELIGIBILITY_MISSING')
        if (item['machine_id'] not in allowed.get('machines', ()) or
                item['line_id'] not in allowed.get('lines', ()) or
                any(segment[0] not in allowed.get('workers', ()) for segment in item['worker_segments'])):
            raise GlobalStateError('GLOBAL_STATE_INELIGIBLE_RESOURCE')
        if snapshot.get('machine_lines', {}).get(str(item['machine_id'])) != item['line_id']:
            raise GlobalStateError('GLOBAL_STATE_MACHINE_LINE')
        calendar = snapshot.get('calendar_windows', {}).get(key)
        if not calendar:
            raise GlobalStateError('GLOBAL_STATE_CALENDAR_MISSING')
        if not any(a <= item['start_seconds'] and item['end_seconds'] <= b for a, b in calendar):
            raise GlobalStateError('GLOBAL_STATE_OFF_CALENDAR')
        release = snapshot.get('route_releases', {}).get(key)
        if release is None:
            raise GlobalStateError('GLOBAL_STATE_ROUTE_BOUNDARY_MISSING')
        if item['start_seconds'] < release:
            raise GlobalStateError('GLOBAL_STATE_EARLY_SUCCESSOR')
        intervals.append((item, 'CANDIDATE'))
    if seen != selected:
        raise GlobalStateError('GLOBAL_STATE_OPERATION_ACCOUNTING')
    selected_rows = {row['operation_key']: row for row in rows}
    outside_rows = {row['operation_key']: row for row in snapshot['approved_outside']}
    for key, row in selected_rows.items():
        edges = snapshot['route_edges'][key]
        predecessor, successor = edges.get('predecessor'), edges.get('successor')
        if predecessor in selected_rows and row['start_seconds'] < selected_rows[predecessor]['end_seconds']:
            raise GlobalStateError('GLOBAL_STATE_EARLY_SUCCESSOR')
        if predecessor in outside_rows and row['start_seconds'] < outside_rows[predecessor]['end_seconds']:
            raise GlobalStateError('GLOBAL_STATE_EXTERNAL_PREDECESSOR')
        if (predecessor and predecessor not in selected_rows and predecessor not in outside_rows and
                predecessor not in snapshot.get('completed_keys', ())):
            raise GlobalStateError('GLOBAL_STATE_EXTERNAL_PREDECESSOR_UNRESOLVED')
        if successor in outside_rows and row['end_seconds'] > outside_rows[successor]['start_seconds']:
            raise GlobalStateError('GLOBAL_STATE_EXTERNAL_SUCCESSOR')
    for i, (a, kind_a) in enumerate(intervals):
        for b, kind_b in intervals[i + 1:]:
            if kind_a == kind_b == 'OUTSIDE':
                continue  # Existing active-state consistency is checked at export.
            if a['machine_id'] == b['machine_id'] and _overlap(
                    (a['start_seconds'], a['end_seconds']), (b['start_seconds'], b['end_seconds'])):
                raise GlobalStateError('GLOBAL_STATE_MACHINE_CONFLICT')
            for aw, aa, ab in a['worker_segments']:
                for bw, ba, bb in b['worker_segments']:
                    if aw == bw and _overlap((aa, ab), (ba, bb)):
                        raise GlobalStateError('GLOBAL_STATE_WORKER_CONFLICT')
    return {'status': 'PASS', 'snapshot_fingerprint': snapshot['fingerprint'],
            'selected_operations': len(seen), 'outside_commitments': len(intervals) - len(seen)}
