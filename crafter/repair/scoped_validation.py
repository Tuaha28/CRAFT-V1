"""Planning-only adapters over the independent exported-schedule validator."""
from datetime import timedelta
import time

from ga.worker_allocation import operator_available_at
from validation.schedule_validator import ScheduleValidationContext, validate_exported_schedule
from crafter.authoritative_semantics import authoritative_changeover_seconds
from .calendar_regimes import _execution
from .resource_configs import ResourceConfiguration
from .window_phenotype import build_window_phenotype, validate_window_scope_accounting
from .window_phenotype import WindowPhenotypeV1
from .validation_scope import ValidationScopeV1


def _config(row):
    cfg = row['resource_configuration']
    return ResourceConfiguration(str(row['operation_key']), int(cfg['factory_id']),
        int(cfg['line_id']), int(cfg['k']), tuple(int(x) for x in cfg['machine_ids']),
        tuple(str(x) for x in cfg['worker_ids']), int(row.get('setup_seconds', 0)),
        int(row['productive_seconds']), ('WINDOW_SELECTED',),
        float(row.get('productive_minutes', row['productive_seconds'] / 60.0)),
        cfg.get('quantity'), cfg.get('execution_contract_version'),
        cfg.get('evidence_class'))


def replay_window_phenotype(state, window):
    """Replay selected work only against the planning state's exact boundaries."""
    mismatches = []
    for row in window.selected_assignments:
        config = _config(row); start = int(row['start_seconds'])
        end, segments = _execution(state, config, start)
        if int(end) != int(row['end_seconds']):
            mismatches.append({'operation_key':config.operation_key,'type':'END_MISMATCH','expected':row['end_seconds'],'actual':end})
        if tuple(segments) != tuple(tuple(x) for x in row['worker_segments']):
            mismatches.append({'operation_key':config.operation_key,'type':'WORKER_SEGMENT_MISMATCH'})
    return {'replay_status':'PASS' if not mismatches else 'FAIL',
            'replay_mismatch_count':len(mismatches),'replay_mismatches':mismatches,
            'historical_future_assignments_backfilled':0}


def replay_assembled_phenotype(state, window):
    """Replay a complete assembled plan in absolute chronological order.

    Each operation is replayed against the exact worker segments of already
    accepted operations, rather than against the old decoder phenotype or
    against a state containing the operation's own reservation.
    """
    mismatches = []
    base_worker_res = {str(worker): list(rows)
                       for worker, rows in state.get('_worker_res', {}).items()}
    origin = state['problem'].origin
    # Operation start is not sufficient to order replay when a worker pauses
    # or when two operations begin before their productive segments.  A
    # paused operation can interleave with another operation on the same
    # crew, so replay each row against the complete assembled reservation
    # context for *other* rows while excluding the row's own segments.  This
    # lets the authoritative fixed-crew walk reproduce both sides of an
    # interleave without treating the row's future segment as self-blocking;
    # the context consists only of accepted assembled assignments, never the
    # historical decoder phenotype.
    def _replay_key(row):
        segments = tuple(tuple(int(v) for v in pair)
                         for pair in row.get('worker_segments', ()))
        first_segment = segments[0][0] if segments else int(row['start_seconds'])
        return (first_segment, int(row['start_seconds']),
                int(row['end_seconds']), str(row['operation_key']))
    ordered = sorted(window.selected_assignments, key=_replay_key)
    all_segments = []
    for candidate in window.selected_assignments:
        candidate_key = str(candidate['operation_key'])
        cfg = _config(candidate)
        for left, right in candidate.get('worker_segments', ()):
            all_segments.append((candidate_key, cfg.worker_ids,
                                 int(left), int(right)))
    for row in ordered:
        config = _config(row)
        row_segments = tuple(tuple(int(v) for v in pair)
                             for pair in row.get('worker_segments', ()))
        first_segment = row_segments[0][0] if row_segments else int(row['start_seconds'])
        worker_res = {key: list(value) for key, value in base_worker_res.items()}
        for candidate_key, candidate_workers, _left, right in all_segments:
            if candidate_key == config.operation_key:
                continue
            for worker in candidate_workers:
                worker_res.setdefault(str(worker), []).append(
                    (origin + timedelta(seconds=_left),
                     origin + timedelta(seconds=right)))
        for values in worker_res.values():
            values.sort()
        local_state = dict(state)
        local_state['_worker_res'] = {key: tuple(value)
                                      for key, value in worker_res.items()}
        end, segments = _execution(local_state, config, int(row['start_seconds']))
        if int(end) != int(row['end_seconds']):
            mismatches.append({'operation_key': config.operation_key,
                               'type': 'END_MISMATCH',
                               'expected': row['end_seconds'], 'actual': end})
        if tuple(segments) != tuple(tuple(x) for x in row['worker_segments']):
            mismatches.append({'operation_key': config.operation_key,
                               'type': 'WORKER_SEGMENT_MISMATCH'})
    return {'replay_status': 'PASS' if not mismatches else 'FAIL',
            'replay_mismatch_count': len(mismatches),
            'replay_mismatches': mismatches,
            'historical_future_assignments_backfilled': 0}


def _boundary_checks(window):
    rows = {str(x['operation_key']): x for x in window.selected_assignments}
    errors = []
    for fact in window.boundary_facts:
        succ = fact.get('successor_operation_key')
        if succ in rows and fact.get('authoritative_end_seconds') is not None:
            if int(rows[succ]['start_seconds']) < int(fact['authoritative_end_seconds']):
                errors.append({'code':'routing_boundary_violation','operation_key':succ})
        # A boundary machine predecessor carries order identity.  Require the
        # configured changeover before the first selected use of that machine.
        if (fact.get('machine_id') is not None and fact.get('end_seconds') is not None
                and fact.get('production_order_id') is not None):
            uses = []
            for row in rows.values():
                cfg=row['resource_configuration']
                if int(fact['machine_id']) in {int(x) for x in cfg['machine_ids']}:
                    if int(row['start_seconds']) >= int(fact['end_seconds']):
                        uses.append(row)
            if uses:
                row = min(uses, key=lambda x: (int(x['start_seconds']), int(x['end_seconds']), str(x['operation_key'])))
                change=int(authoritative_changeover_seconds(str(fact['production_order_id']), str(row['order_id'])))
                if int(row['start_seconds']) < int(fact['end_seconds']) + change:
                    errors.append({'code':'changeover_boundary_violation','operation_key':row['operation_key']})
    return errors


def validate_window_physical(problem, operator_index, window, *, state=None):
    """Validate only selected rows; boundary facts remain external intervals."""
    selected = {str(x['operation_key']): x for x in window.selected_assignments}
    state = state or {}
    operation_contracts = state.get('operation_execution_contracts', {})
    expected = []
    for key in window.validation_scope.required_operation_keys:
        op = problem.operation_by_key[str(key)]
        order = problem.order_by_id[op.order_id]
        row = selected.get(str(key), {})
        contract = operation_contracts.get(str(key))
        if contract is not None:
            from crafter.execution_contract import OperationExecutionContract
            contract = (contract if isinstance(contract, OperationExecutionContract)
                        else OperationExecutionContract.from_mapping(contract))
        config = row.get('resource_configuration', {})
        selected_workers = tuple(str(x) for x in config.get('worker_ids', ()))
        selected_machines = tuple(int(x) for x in config.get('machine_ids', ()))
        try:
            expected_productive_seconds = (contract.expected_productive_seconds(
                selected_workers, selected_machines) if contract is not None
                else int(row.get('productive_seconds', 0)))
        except ValueError:
            expected_productive_seconds = int(row.get('productive_seconds', 0))
        expected.append({'production_order_id': op.order_id, 'operation_id': op.operation_id,
                         'operation_sequence_number': op.sequence,
                         'release_time': problem.origin + timedelta(seconds=order.release_seconds),
                         'deadline': None if order.deadline_seconds is None else problem.origin + timedelta(seconds=order.deadline_seconds),
                         'eligible_line_ids': op.eligible_line_ids,
                         'required_machine_type': op.required_capability,
                         'required_operation_type': op.required_capability,
                         'required_machine_count': (contract.required_machine_count if contract else len(selected_machines)),
                         'required_workers': (contract.required_worker_count if contract else len(selected_workers)),
                         'setup_minutes': (contract.setup_seconds if contract else row.get('setup_seconds', 0)) / 60.0,
                         'productive_minutes': expected_productive_seconds / 60.0})
    machines = {m.machine_id: {'line_id': m.line_id, 'factory_id': m.factory_id, 'capabilities': m.capabilities} for m in problem.machines}
    workers = {}
    for (factory, skill), ids in operator_index.get('by_factory', {}).items():
        for wid in ids: workers.setdefault(str(wid), {'factory_id': int(factory), 'skills': set()})['skills'].add(str(skill))
    def export(row):
        cfg=row['resource_configuration']; start=int(row['start_seconds']); end=int(row['end_seconds'])
        return {'production_order_id': str(row['order_id']), 'operation_id': str(row['operation_key']).split('_',1)[1],
                'assigned_line_id': cfg['line_id'], 'machines_used': cfg['k'], 'machine_ids_used': list(cfg['machine_ids']),
                'operators_used': len(cfg['worker_ids']), 'employee_ids_used': list(cfg['worker_ids']),
                'quantity_scheduled': row.get('quantity', cfg.get('quantity')),
                'worker_segments_by_employee': {str(w): [{'start': (problem.origin+timedelta(seconds=a)).isoformat(), 'end': (problem.origin+timedelta(seconds=b)).isoformat()} for a,b in row['worker_segments']] for w in cfg['worker_ids']},
                'proposed_start_time': (problem.origin+timedelta(seconds=start)).isoformat(),
                'base_end_time': (problem.origin+timedelta(seconds=end)).isoformat(),
                'cushioned_end_time': (problem.origin+timedelta(seconds=end)).isoformat()}
    cm=[]; cw=[]
    for b in window.boundary_facts:
        if b.get('machine_id') is not None: cm.append({'resource_id':b['machine_id'],'start':problem.origin+timedelta(seconds=b['start_seconds']),'end':problem.origin+timedelta(seconds=b['end_seconds']),'operation_key':b.get('operation_key','boundary')})
        if b.get('worker_id') is not None: cw.append({'resource_id':b['worker_id'],'start':problem.origin+timedelta(seconds=b['start_seconds']),'end':problem.origin+timedelta(seconds=b['end_seconds']),'operation_key':b.get('operation_key','boundary')})
    context=ScheduleValidationContext(expected, {o.order_id:o.factory_id for o in problem.orders},
        {l.line_id:l.factory_id for l in problem.lines}, machines, workers, cm, cw,
        lambda w,t: operator_available_at(operator_index, int(w) if str(w).isdigit() else w,t), True,
        operation_contracts=operation_contracts,
        allow_test_assumptions=bool(state.get('allow_test_execution_assumptions', False)),
        require_execution_contracts=bool(state.get('require_execution_contracts', False)),
        worker_interval_is_available=state.get('worker_interval_is_available'),
        machine_interval_is_available=state.get('machine_interval_is_available'),
        expected_source_fingerprint=(state.get('source_resource_provenance') or {}).get('source_sha256'),
        expected_snapshot_fingerprint=(state.get('source_resource_provenance') or {}).get('snapshot_hash'),
        expected_scenario_fingerprint=(state.get('source_resource_provenance') or {}).get('scenario_hash'))
    report=validate_exported_schedule([export(row) for row in window.selected_assignments],context).as_dict()
    boundary_errors = _boundary_checks(window)
    mode = window.validation_scope.mode
    return {'validation_scope':mode,'scope_status':'SCOPE_VALID',
            'physical_status':'VALID' if report['valid'] and not boundary_errors else 'INVALID',
            'factory_complete_claimed':mode in {'ASSEMBLED_FACTORY', 'FULL_GLOBAL'},'report':report,
            'boundary_errors':boundary_errors}


def validate_planning_window(state, window):
    """Single planning acceptance seam: scope → replay → physical validation."""
    started = time.perf_counter()
    scope_started = time.perf_counter()
    scope = validate_window_scope_accounting(window)
    scope_seconds = time.perf_counter() - scope_started
    replay_seconds = 0.0
    physical_seconds = 0.0
    replay_started = time.perf_counter()
    replay = replay_window_phenotype(state, window) if scope['scope_status'] == 'SCOPE_VALID' else {
        'replay_status':'NOT_RUN','replay_mismatch_count':0,'replay_mismatches':[]}
    if scope['scope_status'] == 'SCOPE_VALID':
        replay_seconds = time.perf_counter() - replay_started
    else:
        replay_seconds = 0.0
    physical_started = time.perf_counter()
    physical = validate_window_physical(state['problem'], state['operator_index'], window, state=state) if replay['replay_status'] == 'PASS' else {
        'physical_status':'NOT_RUN','boundary_errors':[]}
    if replay['replay_status'] == 'PASS':
        physical_seconds = time.perf_counter() - physical_started
    accepted = scope['scope_status'] == 'SCOPE_VALID' and replay['replay_status'] == 'PASS' and physical['physical_status'] == 'VALID'
    return {'scope_status':scope['scope_status'],'replay_status':replay['replay_status'],
            'physical_status':physical['physical_status'],'accepted':accepted,
            'factory_complete_claimed':False,'scope':scope,'replay':replay,'physical':physical,
            'timing': {'scope_seconds': scope_seconds,
                       'replay_seconds': replay_seconds,
                       'physical_validation_seconds': physical_seconds,
                       'total_seconds': time.perf_counter() - started}}


def _planning_rows_from_canonical(result, state):
    """Adapt canonical materialized rows to the immutable window contract.

    This is deliberately a planning-only seam.  It enriches rows with master
    data that the scoped replay/validator requires; it never calls the repair
    ``_phenotype(problem, baseline, selected)`` constructor and never copies
    omitted rows from the historical decoder phenotype.
    """
    rows = []
    problem = state['problem']
    for source in ((result.get('acceptance', {}) or {}).get('selected_schedule', ()) or ()):
        row = dict(source)
        key = str(row['operation_key'])
        op = problem.operation_by_key[key]
        cfg = dict(row.get('resource_configuration') or {})
        row['operation_key'] = key
        row['order_id'] = str(op.order_id)
        row['setup_seconds'] = int(row.get('setup_seconds', cfg.get('setup_seconds', 0)))
        row['productive_seconds'] = int(row.get(
            'productive_seconds', cfg.get('productive_seconds', 0)))
        row['productive_minutes'] = float(row.get(
            'productive_minutes', cfg.get('productive_minutes', row['productive_seconds'] / 60.0)))
        row['resource_configuration'] = cfg
        rows.append(row)
    return rows


def validate_canonical_planning_candidate(result, state, *, required_operation_keys,
                                          boundary_facts=(),
                                          future_outside_operation_keys=()):
    """Run the authoritative scoped acceptance path on canonical output.

    ``required_operation_keys`` is the pre-solve admission contract, not a
    set reconstructed from solver output.  This helper is used by the V3
    incumbent and candidate quality controller so a bounded candidate is
    accepted only after scope accounting, exact replay, and independent
    physical validation all pass.
    """
    from .validation_scope import ValidationScopeV1

    rows = _planning_rows_from_canonical(result, state)
    required = tuple(sorted(str(key) for key in required_operation_keys))
    future = tuple(sorted(str(key) for key in future_outside_operation_keys))
    scope = ValidationScopeV1('WINDOW', required, future)
    phenotype, _ = build_window_phenotype(
        rows, boundary_facts=tuple(dict(f) for f in boundary_facts),
        future_outside_operation_keys=future, validation_scope=scope)
    acceptance = validate_planning_window(state, phenotype)
    acceptance['materialization_mismatch_count'] = int(
        ((result.get('acceptance', {}) or {}).get('materialization', {}) or {})
        .get('mismatch_count', 0))
    acceptance['historical_future_assignments_backfilled'] = int(
        acceptance.get('scope', {}).get('historical_future_assignments_backfilled', 0) or
        acceptance.get('replay', {}).get('historical_future_assignments_backfilled', 0) or 0)
    acceptance['factory_complete_claimed'] = False
    acceptance['window_assignment_keys'] = tuple(sorted(phenotype.assignment_keys))
    acceptance['required_operation_keys'] = required
    return acceptance


def _violation_counts(errors):
    counts = {
        'precedence_violations': 0,
        'routing_boundary_violations': 0,
        'changeover_violations': 0,
        'machine_overlaps': 0,
        'worker_overlaps': 0,
        'factory_violations': 0,
        'line_eligibility_violations': 0,
        'machine_eligibility_violations': 0,
        'worker_eligibility_violations': 0,
        'calendar_violations': 0,
        'reservation_violations': 0,
        'release_violations': 0,
        'commit_violations': 0,
    }
    for error in errors:
        code = str(error.get('code', ''))
        if code == 'precedence_violation': counts['precedence_violations'] += 1
        elif code == 'routing_boundary_violation': counts['routing_boundary_violations'] += 1
        elif code == 'changeover_boundary_violation': counts['changeover_violations'] += 1
        elif code == 'machine_overlap': counts['machine_overlaps'] += 1
        elif code == 'worker_overlap': counts['worker_overlaps'] += 1
        elif code in {'line_factory_mismatch', 'machine_factory_mismatch',
                      'worker_factory_mismatch', 'missing_factory_ownership'}:
            counts['factory_violations'] += 1
        elif code == 'ineligible_line': counts['line_eligibility_violations'] += 1
        elif code in {'unknown_machine', 'machine_line_mismatch',
                      'machine_capability_mismatch', 'machine_count_mismatch',
                      'missing_machine_assignment'}:
            counts['machine_eligibility_violations'] += 1
        elif code in {'unknown_worker', 'worker_skill_mismatch', 'worker_shortage',
                      'worker_count_mismatch'}:
            counts['worker_eligibility_violations'] += 1
        elif 'calendar' in code: counts['calendar_violations'] += 1
        elif 'reservation' in code or code.startswith('invalid_committed_'):
            counts['reservation_violations'] += 1
        elif 'release' in code: counts['release_violations'] += 1
    return counts


def validate_two_window_assignments(state, assignments, *, required_operation_keys,
                                    boundary_facts=()):
    """Validate an assembled bounded continuity view using shared primitives.

    This deliberately does not claim factory completeness and never fills
    omitted future work.  The assembled rows are the only scheduled universe;
    the existing exported-schedule validator supplies the physical checks.
    """
    keys = tuple(sorted(str(k) for k in required_operation_keys))
    scope = ValidationScopeV1('WINDOW', keys, ())
    phenotype = WindowPhenotypeV1(tuple(dict(row) for row in assignments),
                                  tuple(dict(row) for row in boundary_facts),
                                  (), scope)
    accounting = validate_window_scope_accounting(phenotype)
    if accounting['scope_status'] != 'SCOPE_VALID':
        return {'validation_scope': 'TWO_WINDOW_CONTINUITY',
                'scope_status': accounting['scope_status'],
                'physical_status': 'NOT_RUN',
                'factory_complete_claimed': False,
                'scope': accounting, 'report': None,
                'violation_counts': {}}
    physical = validate_window_physical(state['problem'], state['operator_index'], phenotype, state=state)
    errors = list(physical.get('report', {}).get('errors', ()))
    errors.extend(physical.get('boundary_errors', ()))
    counts = _violation_counts(errors)
    return {'validation_scope': 'TWO_WINDOW_CONTINUITY',
            'scope_status': accounting['scope_status'],
            'physical_status': physical['physical_status'],
            'factory_complete_claimed': False,
            'scope': accounting, 'report': physical.get('report'),
            'boundary_errors': physical.get('boundary_errors', ()),
            'violation_counts': counts,
            'physical_errors': errors}


def validate_assembled_factory(state, assignments, *, required_operation_keys):
    """Run the same scoped replay/physical primitives at factory scope."""
    keys = tuple(sorted(str(k) for k in required_operation_keys))
    scope = ValidationScopeV1('ASSEMBLED_FACTORY', keys, ())
    phenotype = WindowPhenotypeV1(tuple(dict(row) for row in assignments), (), (), scope)
    accounting = validate_window_scope_accounting(phenotype)
    replay = replay_assembled_phenotype(state, phenotype) if accounting['scope_status'] == 'SCOPE_VALID' else {
        'replay_status': 'NOT_RUN', 'replay_mismatch_count': 0,
        'replay_mismatches': [], 'historical_future_assignments_backfilled': 0}
    physical = validate_window_physical(state['problem'], state['operator_index'], phenotype, state=state) if replay['replay_status'] == 'PASS' else {
        'physical_status': 'NOT_RUN', 'boundary_errors': [], 'report': None,
        'factory_complete_claimed': True}
    accepted = (accounting['scope_status'] == 'SCOPE_VALID'
                and replay['replay_status'] == 'PASS'
                and physical['physical_status'] == 'VALID')
    errors = list((physical.get('report') or {}).get('errors', ()))
    errors.extend(physical.get('boundary_errors', ()))
    return {'validation_scope': 'ASSEMBLED_FACTORY',
            'scope_status': accounting['scope_status'],
            'replay_status': replay['replay_status'],
            'physical_status': physical['physical_status'],
            'accepted': accepted,
            'factory_complete_claimed': bool(accepted),
            'scope': accounting, 'replay': replay, 'physical': physical,
            'violation_counts': _violation_counts(errors),
            'physical_errors': errors}
