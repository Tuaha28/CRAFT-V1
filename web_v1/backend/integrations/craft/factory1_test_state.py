"""Pinned Factory 1 simulation state with application-owned TEST approvals.

The captured source and simulated holds are immutable inputs. Only the ACTIVE
governance version supplies approved future assignments; planned source lots
never become reservations here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .global_resource_state import assignment_from_row, seal, verify

ROOT = Path(__file__).resolve().parents[4]
AUDIT = ROOT / 'web_v1/audits/global_resource_state_stage_a'
SOURCE = ROOT / 'web_v1/audits/execution_boundary_v1/factory1_source.json'
SCENARIO = ROOT / 'web_v1/audits/factory1_simulated_continuation_v1/scenario.json'
BASE = AUDIT / 'factory1_global_state_snapshot.json'


def pinned_base():
    base = json.loads(BASE.read_text(encoding='utf-8'))
    verify(base)
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != base['source_hash']:
        raise ValueError('FACTORY1_SOURCE_FINGERPRINT_CHANGED')
    scenario = json.loads(SCENARIO.read_text(encoding='utf-8'))
    content = {key: value for key, value in scenario.items() if key != 'scenario_hash'}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (digest != scenario.get('scenario_hash') or digest != base.get('scenario_hash') or
            scenario.get('source_fingerprint') != base['source_hash'] or
            not scenario.get('simulation_only') or scenario.get('production_admission_enabled')):
        raise ValueError('FACTORY1_SIMULATION_PROVENANCE_CHANGED')
    return base


def admission_blocker(base, operation_key):
    """Explain why a captured identity is not admitted to this TEST solver.

    The global ledger is broader than the independently prepared execution
    domain. A ledger entry alone supplies neither a crew-size requirement nor
    the exact eligibility/calendar/routing contract needed by CP-SAT.
    """
    if operation_key not in base['operation_ledger']:
        return 'UNKNOWN_OPERATION_IDENTITY'
    if operation_key in base['completed_keys']:
        return 'COMPLETED_WORK_MUST_NOT_BE_RESCHEDULED'
    if operation_key in base['selected_keys']:
        # These keys alone have a checked simulation-only continuation and
        # resource-domain adapter. Real production admission is still blocked.
        return None
    if operation_key in base['production_admission_blocked_keys']:
        return 'REAL_PARTIAL_CONTINUATION_EVIDENCE_MISSING'
    return 'SOURCE_EXECUTION_DOMAIN_NOT_INDEPENDENTLY_VALIDATED'


def export_state(scope, selected_keys):
    base = pinned_base()
    selected = set(selected_keys)
    if not selected:
        raise ValueError('FACTORY1_SELECTION_EMPTY')
    for key in sorted(selected):
        reason = admission_blocker(base, key)
        if reason and key not in base['selected_keys']:
            raise ValueError(f'FACTORY1_ADMISSION_BLOCKED:{key}:{reason}')
    active = scope.active_version
    if active and (active.status != 'ACTIVE' or active.payload_hash != _payload_hash(active)):
        raise ValueError('FACTORY1_ACTIVE_VERSION_CHANGED')
    if active and any(row['operation_key'] in selected for row in active.operations):
        raise ValueError('FACTORY1_APPROVED_OPERATION_IN_REPAIR_SCOPE')
    approved = [{**assignment_from_row(row, 1), 'approved_version_id': str(active.pk)}
                for row in (active.operations if active else ())]
    dispositions = dict(base['schedule_disposition'])
    reasons = dict(base['disposition_reasons'])
    for key in base['selected_keys']:
        dispositions[key] = 'DEFERRED'
        reasons[key] = 'OUTSIDE_BOUNDED_SIMULATION_WINDOW_NO_FIXED_FUTURE_ASSIGNMENT'
    for row in approved:
        dispositions[row['operation_key']] = 'APPROVED'
        reasons.pop(row['operation_key'], None)
    for key in selected:
        dispositions[key] = 'SELECTED'
        reasons.pop(key, None)
    packet = {key: value for key, value in base.items() if key != 'fingerprint'}
    packet.update(selected_keys=sorted(selected), approved_outside=approved,
                  active_version_id=str(active.pk) if active else None,
                  resource_revision=scope.resource_revision,
                  schedule_disposition=dispositions, disposition_reasons=reasons)
    packet = seal(packet)
    verify(packet)
    return packet


def approved_boundary(snapshot):
    """Adapt the sealed state to CRAFT's existing exact outside-boundary contract."""
    rows = [{**row, 'worker_segments': [[int(wid), a, b]
                                           for wid, a, b in row['worker_segments']]}
            for row in snapshot['approved_outside']]
    if not rows:
        return None
    # The boundary contract contains only the fields used by the independent
    # captured-source validator. Its hash binds every exported interval.
    from hashlib import sha256
    assignments = [{k: v for k, v in row.items()
                    if k not in ('factory_id', 'line_id', 'approved_version_id')}
                   for row in rows]
    digest = sha256(json.dumps(assignments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'classification': 'APPROVED_TEST_SCHEDULE_SNAPSHOT', 'status': 'ACTIVE',
            'active_version_id': snapshot['active_version_id'],
            'source_sha256': snapshot['source_hash'],
            'assignments_sha256': digest, 'assignments': assignments}


def _payload_hash(version):
    value = {'operations': version.operations, 'evidence': version.evidence}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()
