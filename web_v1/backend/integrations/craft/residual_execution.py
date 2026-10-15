"""Isolated remaining-work normalization for one indivisible unstarted tail.

Consumes CRAFT data contracts without importing the heavy engine. No database
access, solving, or production admission occurs on import. Source records are
immutable; the quantity ledger supplements the existing physical validators.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime

from .contracts import digest


class ResidualEvidenceError(ValueError):
    pass


def check_ledger(evidence, decision_time):
    """Validate source evidence, including its knowledge-time cutoff."""
    def require(condition, code):
        if not condition:
            raise ResidualEvidenceError(code)

    decision = datetime.fromisoformat(decision_time)
    for name in ('as_of', 'event_time', 'captured_at', 'available_at', 'valid_until'):
        require(name in evidence, 'MISSING_' + name.upper())
        value = datetime.fromisoformat(evidence[name])
        require(value.tzinfo == decision.tzinfo, 'CLOCK_MISMATCH')
        if name != 'valid_until':
            require(value <= decision, 'FUTURE_KNOWN_EVIDENCE')
        else:
            require(value >= decision, 'STALE_EVIDENCE')
    require(evidence['event_time'] <= evidence['as_of'], 'FUTURE_OBSERVATION')
    require(evidence['captured_at'] <= evidence['available_at'], 'INVALID_KNOWLEDGE_ORDER')
    require(bool(evidence.get('authorization_id')), 'MISSING_AUTHORIZATION')
    require(evidence.get('residual_transfer_authorized') is True, 'TRANSFER_NOT_AUTHORIZED')
    require(evidence.get('execution_mode') == 'SINGLE_UNSTARTED_TAIL', 'UNSUPPORTED_EXECUTION_MODE')
    require(not evidence.get('in_flight') and not evidence.get('future_holds'), 'ONGOING_EXECUTION_UNRESOLVED')
    lots = evidence['lots']
    require(bool(lots), 'EMPTY_LEDGER')
    require(len({x['lot_id'] for x in lots}) == len(lots), 'DUPLICATE_LOT')
    require(all(type(x['quantity']) is int and x['quantity'] > 0 for x in lots), 'INVALID_QUANTITY')
    require(all(x['state'] in ('COMPLETED', 'NOT_STARTED') for x in lots), 'UNSUPPORTED_LOT_STATE')
    for lot in lots:
        if lot['state'] == 'COMPLETED':
            require(datetime.fromisoformat(lot['completed_at']) <= datetime.fromisoformat(evidence['as_of']), 'FUTURE_COMPLETION')
    require(type(evidence['required_quantity']) is int, 'INVALID_REQUIRED_QUANTITY')
    require(sum(x['quantity'] for x in lots) == evidence['required_quantity'], 'QUANTITY_MISMATCH')
    remaining = sum(x['quantity'] for x in lots if x['state'] == 'NOT_STARTED')
    require(remaining > 0, 'NO_REMAINING_WORK')
    work = evidence['remaining_work']
    require(type(work['serial_productive_seconds']) is int and work['serial_productive_seconds'] > 0, 'INVALID_WORK')
    require(work['source'] in ('OBSERVED_REMAINING', 'AUTHORIZED_ESTIMATE', 'STANDARD_WORK_MODEL'), 'INVALID_WORK_SOURCE')
    require(bool(work.get('assumptions')) and bool(work.get('model_version')), 'UNDOCUMENTED_WORK_MODEL')
    require(type(evidence.get('remaining_setup_seconds')) is int and evidence['remaining_setup_seconds'] >= 0, 'SETUP_UNRESOLVED')
    return remaining


def normalize_problem(problem, evidence, *, decision_time):
    """Replace only the source operation's executable work; retain topology.

    Entire predecessor work still precedes the residual, and the residual end
    still precedes every successor. No lot-transfer semantics are introduced.
    """
    remaining = check_ledger(evidence, decision_time)
    key = evidence['operation_key']
    op = problem.operation_by_key[key]
    if problem.order_by_id[op.order_id].factory_id != evidence['factory_id']:
        raise ResidualEvidenceError('FACTORY_MISMATCH')
    if str(op.order_id) != str(evidence['order_id']):
        raise ResidualEvidenceError('ORDER_MISMATCH')
    if evidence['source_operation_hash'] != digest(asdict(op)):
        raise ResidualEvidenceError('SOURCE_OPERATION_CHANGED')
    cutoff = (datetime.fromisoformat(decision_time) - problem.origin).total_seconds()
    if cutoff != 0:
        raise ResidualEvidenceError('INSTANCE_ORIGIN_MUST_EQUAL_DECISION')
    updated = replace(op, serial_productive_seconds=evidence['remaining_work']['serial_productive_seconds'],
                      setup_seconds=evidence['remaining_setup_seconds'])
    normalized = replace(problem, operations=tuple(updated if x.operation_key == key else x for x in problem.operations))
    return normalized, {'contract': 'RESIDUAL_EXECUTION_V1', 'source_evidence': deepcopy(evidence),
                        'evidence_hash': digest(evidence), 'remaining_quantity': remaining,
                        'production_enabled': False}


def validate_residual_output(source, rows, *, decision_time):
    """Independent quantity/work check from source ledger, never candidate totals.

    Resource/calendar checks must ALSO pass the existing CRAFT validators.
    The returned completion is projected, never an observed actual.
    """
    remaining = check_ledger(source, decision_time)
    matches = [r for r in rows if r['operation_key'] == source['operation_key']]
    if len(matches) != 1:
        raise ResidualEvidenceError('RESIDUAL_IDENTITY_MISMATCH')
    row = matches[0]
    cfg = row['resource_configuration']
    k = int(cfg['k'])
    if k < 1:
        raise ResidualEvidenceError('INVALID_MACHINE_COUNT')
    expected = (source['remaining_work']['serial_productive_seconds'] + k - 1) // k
    segments = row['worker_segments']
    if cfg['setup_seconds'] != source['remaining_setup_seconds']:
        raise ResidualEvidenceError('SOURCE_SETUP_MISMATCH')
    if any(b > c for (_, b), (c, _) in zip(segments, segments[1:])):
        raise ResidualEvidenceError('OVERLAPPING_EXECUTION_SEGMENTS')
    if any(a < row['start_seconds'] or b <= a or b > row['end_seconds'] for a, b in segments):
        raise ResidualEvidenceError('INVALID_EXECUTION_SEGMENTS')
    if sum(b-a for a, b in segments) != expected or cfg['productive_seconds'] != expected:
        raise ResidualEvidenceError('SOURCE_WORK_MISMATCH')
    if row['start_seconds'] < 0:
        raise ResidualEvidenceError('HISTORICAL_WORK_RESCHEDULED')
    completed = sum(x['quantity'] for x in source['lots'] if x['state'] == 'COMPLETED')
    return {'status': 'PASS', 'operation_key': source['operation_key'],
            'required_quantity': source['required_quantity'], 'completed_quantity': completed,
            'scheduled_remaining_quantity': remaining, 'projected_completion_seconds': row['end_seconds'],
            'immutable_history': deepcopy([x for x in source['lots'] if x['state'] == 'COMPLETED']),
            'source_evidence_hash': digest(source), 'actual_completion_record_created': False}
