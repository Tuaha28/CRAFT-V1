"""Application-only transactions. No scheduler imports or legacy connections."""
import hashlib
import json
import uuid
import sys
from pathlib import Path
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .models import AuditEvent, CraftRun, FactoryAccess, ScheduleScope, ScheduleVersion
from integrations.craft.global_resource_state import (
    GlobalStateError, assignment_from_row, seal, validate_candidate, verify,
)
from integrations.craft.factory1_test_state import admission_blocker, export_state as export_factory1_state, pinned_base
from integrations.craft import capture_test_state

# The opt-in governance process runs with web_v1/backend as sys.path[0].
# Resolve the repository-owned pure cycle contract explicitly for TEST cycles.
_CRAFT_ROOT = Path(__file__).resolve().parents[3]
if str(_CRAFT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAFT_ROOT))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def implementation_hash():
    root = Path(__file__).resolve().parents[3]
    files = ['web_v1/backend/integrations/craft/residual_execution.py',
             'web_v1/backend/integrations/craft/tests/test_residual_execution.py',
             'web_v1/audits/industrial_readiness_v1/run_fixture.py',
             'crafter/repair/calendar_regimes.py', 'crafter/repair/cp_repair.py',
             'crafter/repair/solve.py', 'crafter/repair/scoped_validation.py', 'crafter/contracts.py']
    files += ['crafter/lns_probe/joint_modes.py', 'crafter/authoritative_semantics.py',
              'utils/work_calendar.py', 'ga/worker_allocation.py']
    files += ['web_v1/audits/fyp_completion_20261010/run_partial_chain_smoke.py',
              'web_v1/audits/fyp_completion_20261010/validate_partial_chain.py']
    files += ['web_v1/backend/integrations/craft/priority_fixture.py',
              'web_v1/backend/integrations/craft/run_priority_fixture.py']
    files += ['web_v1/backend/integrations/craft/global_resource_state.py',
              'web_v1/backend/integrations/craft/run_global_state_fixture.py',
              'web_v1/backend/governance/resource_fixture.py']
    files += ['web_v1/backend/integrations/craft/factory1_test_state.py',
              'web_v1/backend/integrations/craft/run_factory1_governed.py',
              'web_v1/backend/integrations/craft/factory1_cycles.py',
              'crafter/production/cycles.py',
              'crafter/production/adaptive_cycles.py',
              'crafter/repair/resource_configs.py',
              'crafter/repair/planning.py', 'crafter/repair/resource_domain_v2.py',
              'web_v1/audits/factory1_realistic_neighborhood_v1/run_bounded.py',
              'web_v1/audits/factory1_realistic_neighborhood_v1/validate_bounded.py']
    files += ['web_v1/backend/integrations/craft/capture_test_state.py',
              'web_v1/backend/integrations/craft/run_capture_governed.py',
              'web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py',
              'crafter/production/capture_boundaries.py']
    return fingerprint({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files})


def request_recipe(scope):
    if scope.environment == 'TEST_CAPTURE':
        try:
            return capture_test_state.recipe(scope)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
    if scope.factory_external_id != 1:
        raise ValidationError('UNSUPPORTED_FACTORY_SCOPE')
    if scope.environment == 'TEST':
        return {'contract': 'RESIDUAL_VERTICAL_SLICE_V1', 'source': 'SYNTHETIC_FIXTURE'}
    if scope.environment == 'TEST_SIM':
        path = Path(__file__).resolve().parents[3] / 'web_v1/audits/factory1_simulated_continuation_v1/scenario.json'
        scenario = json.loads(path.read_text(encoding='utf-8'))
        if not scenario.get('simulation_only') or scenario.get('production_admission_enabled'):
            raise ValidationError('SIMULATION_ISOLATION_INVALID')
        if fingerprint({key: value for key, value in scenario.items() if key != 'scenario_hash'}) != scenario.get('scenario_hash'):
            raise ValidationError('SIMULATION_SCENARIO_HASH_MISMATCH')
        return {'contract': 'FACTORY1_PARTIAL_CHAIN_V1', 'source': 'SIMULATED_CONTINUATION',
                'scenario_hash': scenario['scenario_hash'], 'source_fingerprint': scenario['source_fingerprint']}
    if scope.environment == 'TEST_PRIORITY':
        return {'contract': 'SYNTHETIC_PRIORITY_TWO_ORDER_V1', 'source': 'SYNTHETIC_PRIORITY_FIXTURE',
                'fixture_version': 1, 'priority_state': scope.priority_state}
    if scope.environment == 'TEST_RESOURCE':
        selected = scope.resource_state.get('selected_keys', ())
        if list(selected) not in (['A_residual'], ['B_residual']):
            raise ValidationError('INVALID_TEST_RESOURCE_SELECTION')
        return {'contract': 'SYNTHETIC_GLOBAL_RESOURCE_V1', 'source': 'SYNTHETIC_RESOURCE_TEST',
                'selected_keys': list(selected), 'fixture_version': 1}
    if scope.environment == 'TEST_FACTORY1':
        base = pinned_base()
        selected = scope.resource_state.get('selected_keys', ())
        resource_policy = scope.resource_state.get('resource_policy', 'incumbent_prefix')
        outside = set(selected) - set(base['selected_keys'])
        if outside:
            key = sorted(outside)[0]
            raise ValidationError(f'FACTORY1_ADMISSION_BLOCKED:{key}:{admission_blocker(base, key)}')
        if (scope.resource_state.get('classification') != 'SIMULATED_TEST' or
                scope.resource_state.get('production_admission_enabled') or
                resource_policy not in ('incumbent_prefix', 'stage_b_diverse', 'stage_b_adaptive') or
                not selected or len(selected) != len(set(selected))):
            raise ValidationError('INVALID_FACTORY1_TEST_SELECTION')
        cycle_target = scope.resource_state.get('cycle_target')
        window_size = scope.resource_state.get('window_size')
        adaptive_retries = scope.resource_state.get('adaptive_retries', False)
        if type(adaptive_retries) is not bool or (adaptive_retries and cycle_target is None):
            raise ValidationError('INVALID_ADAPTIVE_CYCLE_POLICY')
        if (cycle_target is None) != (window_size is None):
            raise ValidationError('INCOMPLETE_CYCLE_POLICY')
        if cycle_target is not None:
            from crafter.production.cycles import policy as cycle_policy
            try:
                cycle_policy({'cycle_target': cycle_target, 'window_size': window_size})
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if resource_policy != 'stage_b_adaptive':
                raise ValidationError('CYCLE_REQUIRES_SOURCE_ADAPTIVE_DOMAIN')
        recipe = {'contract': 'FACTORY1_GOVERNED_BOUNDED_V1',
                'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                'source_fingerprint': base['source_hash'],
                'scenario_hash': base['scenario_hash'],
                'resource_policy': resource_policy,
                'selected_keys': list(selected)}
        if cycle_target is not None:
            recipe.update(cycle_target=cycle_target, window_size=window_size)
            if adaptive_retries:
                recipe['adaptive_retries'] = True
        return recipe
    raise ValidationError('LIVE_ADMISSION_NOT_PROVEN')


def global_state_for(scope, selected_keys):
    """Export application-owned ACTIVE TEST assignments; never source planned lots."""
    if scope.environment == 'TEST_CAPTURE':
        try:
            return capture_test_state.export_state(scope, selected_keys)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
    if scope.environment == 'TEST_FACTORY1':
        try:
            return export_factory1_state(scope, selected_keys)
        except (ValueError, GlobalStateError) as exc:
            raise ValidationError(str(exc)) from exc
    if scope.environment != 'TEST_RESOURCE':
        raise ValidationError('GLOBAL_STATE_SCOPE_UNSUPPORTED')
    state = scope.resource_state
    if state.get('classification') != 'SIMULATED_TEST' or state.get('production_admission_enabled'):
        raise ValidationError('GLOBAL_STATE_SIMULATION_ISOLATION')
    selected = set(selected_keys)
    active = scope.active_version
    if active and active.status != 'ACTIVE':
        raise ValidationError('GLOBAL_STATE_ACTIVE_VERSION_INVALID')
    if active and active.payload_hash != fingerprint({'operations': active.operations,
                                                       'evidence': active.evidence}):
        raise ValidationError('GLOBAL_STATE_ACTIVE_PAYLOAD_CHANGED')
    outside_rows = [row for row in (active.operations if active else ())
                    if row['operation_key'] not in selected]
    approved = [{**assignment_from_row(row, scope.factory_external_id),
                 'approved_version_id': str(active.pk)} for row in outside_rows]
    for row in state.get('fixed_commitments', ()):
        if row.get('factory_id') != scope.factory_external_id:
            raise ValidationError('GLOBAL_STATE_CROSS_FACTORY')
    packet = {'contract': 'GLOBAL_RESOURCE_STATE_V1', 'classification': 'SIMULATED_TEST',
              'factory_id': scope.factory_external_id, 'source_hash': scope.source_hash,
              'source_identity': state.get('source_identity'),
              'reference_time': state.get('reference_time'), 'timezone': state.get('timezone'),
              'resource_revision': scope.resource_revision,
              'calendar_revision': state.get('calendar_revision'),
              'eligibility_revision': state.get('eligibility_revision'),
              'machine_lines': state.get('machine_lines', {}),
              'worker_ids': state.get('worker_ids', []),
              'active_version_id': str(active.pk) if active else None,
              'selected_keys': sorted(selected), 'approved_outside': approved,
              'fixed_commitments': state.get('fixed_commitments', []),
              'eligibility': state.get('eligibility', {}),
              'calendar_windows': state.get('calendar_windows', {}),
              'route_releases': state.get('route_releases', {}),
              'route_edges': state.get('route_edges', {}),
              'completed_keys': state.get('completed_keys', []),
              'operation_ledger': state.get('operation_ledger', {}),
              'execution_states': state.get('execution_states', {}),
              'order_identities': state.get('order_identities', {}),
              'schedule_disposition': {
                  key: ('SELECTED' if key in selected else
                        'APPROVED' if any(row['operation_key'] == key for row in outside_rows) else 'UNSCHEDULED')
                  for key in state.get('operation_ledger', {})},
              'priority_revision': int(scope.priority_state.get('revision', 0))}
    packet = seal(packet)
    try:
        verify(packet)
    except GlobalStateError as exc:
        raise ValidationError(str(exc)) from exc
    return packet


@transaction.atomic
def select_test_resource_work(user, scope_id, operation_key):
    """TEST-only change of next bounded operation; existing ACTIVE work stays put."""
    scope = ScheduleScope.objects.select_for_update().get(pk=scope_id)
    authorize(user, scope, 'request_run')
    if scope.environment != 'TEST_RESOURCE' or operation_key not in ('A_residual', 'B_residual'):
        raise ValidationError('UNSUPPORTED_TEST_RESOURCE_SELECTION')
    if scope.resource_state.get('classification') != 'SIMULATED_TEST':
        raise ValidationError('GLOBAL_STATE_SIMULATION_ISOLATION')
    scope.resource_state = {**scope.resource_state, 'selected_keys': [operation_key]}
    scope.resource_revision += 1
    scope.source_hash = fingerprint(request_recipe(scope))
    scope.save(update_fields=['resource_state', 'resource_revision', 'source_hash'])
    AuditEvent.objects.create(scope=scope, actor=user, action='TEST_RESOURCE_SELECTION',
                              entity_id=operation_key, reason='Controlled next bounded TEST operation',
                              details={'resource_revision': scope.resource_revision,
                                       'source_hash': scope.source_hash})
    return scope


@transaction.atomic
def select_test_factory1_work(user, scope_id, selected_keys):
    """Select another pinned bounded region; ACTIVE assignments remain immutable."""
    scope = ScheduleScope.objects.select_for_update().get(pk=scope_id)
    authorize(user, scope, 'request_run')
    if scope.environment != 'TEST_FACTORY1':
        raise ValidationError('FACTORY1_TEST_ONLY')
    selected = list(selected_keys)
    base = pinned_base()
    if (not selected or len(selected) != len(set(selected)) or
            not set(selected) <= set(base['selected_keys']) or
            any(row['operation_key'] in selected for row in
                (scope.active_version.operations if scope.active_version else ()))):
        raise ValidationError('FACTORY1_SELECTION_INVALID_OR_APPROVED')
    scope.resource_state = {**scope.resource_state, 'selected_keys': selected}
    scope.resource_revision += 1
    scope.source_hash = fingerprint(request_recipe(scope))
    scope.save(update_fields=['resource_state', 'resource_revision', 'source_hash'])
    AuditEvent.objects.create(scope=scope, actor=user, action='FACTORY1_TEST_SELECTION',
                              entity_id=str(scope.pk), reason='Controlled pinned simulation region',
                              details={'selected_keys': selected,
                                       'resource_revision': scope.resource_revision})
    return scope


def authorize(user, scope, permission):
    if not user.is_authenticated or not user.has_perm('governance.' + permission):
        raise PermissionDenied('Scheduling permission required')
    if not user.is_superuser and not FactoryAccess.objects.filter(user=user, scope=scope).exists():
        raise PermissionDenied('Factory scope access required')


@transaction.atomic
def record_priority(user, scope_id, *, order_id, tier, target_due_seconds, reason, expected_source_hash):
    scope = ScheduleScope.objects.select_for_update().get(pk=scope_id)
    authorize(user, scope, 'change_priority')
    if scope.environment != 'TEST_PRIORITY' or scope.factory_external_id != 1:
        raise ValidationError('PRIORITY_LIVE_ADMISSION_DISABLED')
    if scope.source_hash != expected_source_hash:
        raise ValidationError('STALE_PRIORITY_STATE')
    if order_id not in ('A', 'B') or tier not in ('NORMAL', 'HIGH', 'URGENT'):
        raise ValidationError('UNSUPPORTED_PRIORITY_REQUEST')
    if not isinstance(target_due_seconds, int) or not 1 <= target_due_seconds <= 3600:
        raise ValidationError('INVALID_MANAGER_TARGET')
    if not reason.strip():
        raise ValidationError('Priority reason required')
    revision = int(scope.priority_state.get('revision', 0)) + 1
    override = (None if tier == 'NORMAL' else {
        'override_id': str(uuid.uuid4()), 'order_id': order_id, 'tier': tier,
        'effective_at': 0, 'target_due_at': target_due_seconds,
        'reason_code': 'MANAGER_DELIVERY_RISK', 'entered_by': user.get_username(),
        'entered_at': 0, 'revision': revision,
    })
    scope.priority_state = {'revision': revision, 'override': override,
                            'selected_order_id': order_id, 'tier': tier,
                            'target_due_seconds': target_due_seconds,
                            'recorded_at': timezone.now().isoformat(), 'reason': reason}
    scope.source_hash = fingerprint(request_recipe(scope))
    scope.save(update_fields=['priority_state', 'source_hash'])
    AuditEvent.objects.create(scope=scope, actor=user, action='ORDER_PRIORITY_CHANGED',
                              entity_id=order_id, reason=reason,
                              details={'tier': tier, 'target_due_seconds': target_due_seconds,
                                       'revision': revision, 'source_hash': scope.source_hash,
                                       'override_id': override['override_id'] if override else None})
    return scope


def priority_result_matches(snapshot, result):
    if snapshot.get('contract') != 'SYNTHETIC_PRIORITY_TWO_ORDER_V1':
        return True
    if result.get('source') != 'SYNTHETIC_PRIORITY_FIXTURE':
        return False
    expected = snapshot.get('priority_state', {}).get('override')
    context = result.get('acceptance', {}).get('objective', {}).get('objective', {}).get('context', {})
    active = context.get('active_manager_overrides', {})
    if result.get('acceptance', {}).get('objective', {}).get('status') != 'OBJECTIVE_OPTIMIZATION_COMPLETE':
        return False
    if expected is None:
        return not active and result.get('manager_overrides') == []
    actual = active.get(expected.get('order_id'))
    fields = ('override_id', 'order_id', 'tier', 'effective_at', 'target_due_at',
              'reason_code', 'entered_by', 'entered_at', 'revision')
    return (len(active) == 1 and isinstance(actual, dict)
            and all(actual.get(name) == expected.get(name) for name in fields)
            and result.get('manager_overrides') == [expected])


@transaction.atomic
def request_run(user, scope_id, key, snapshot):
    scope = ScheduleScope.objects.select_for_update().get(pk=scope_id)
    authorize(user, scope, 'request_run')
    # Only proven synthetic integration scope admitted. No implicit live fallback.
    if snapshot != request_recipe(scope):
        raise ValidationError('LIVE_ADMISSION_NOT_PROVEN')
    if not key or len(key) > 128:
        raise ValidationError('Invalid idempotency key')
    if scope.source_hash != fingerprint(snapshot):
        raise ValidationError('SOURCE_STATE_CHANGED')
    if scope.environment in ('TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE'):
        snapshot = {**snapshot, 'global_resource_state': global_state_for(scope, snapshot['selected_keys'])}
    snapshot = {**snapshot, 'implementation_hash': implementation_hash()}
    input_hash = fingerprint(snapshot)
    previous = CraftRun.objects.filter(scope=scope, idempotency_key=key).first()
    if previous:
        if previous.input_hash != input_hash or previous.requested_by_id != user.pk:
            raise ValidationError('IDEMPOTENCY_CONFLICT')
        return previous, False
    run = CraftRun.objects.create(scope=scope, requested_by=user, idempotency_key=key,
                                 snapshot=snapshot, input_hash=input_hash, source_hash=scope.source_hash,
                                 base_version=scope.active_version)
    AuditEvent.objects.create(scope=scope, actor=user, action='RUN_REQUESTED', entity_id=str(run.pk),
                              reason='Controlled scheduling request',
                              details={'input_hash': input_hash, 'priority_state': snapshot.get('priority_state')})
    return run, True


@transaction.atomic
def claim_run():
    # Compare-and-swap works on SQLite too; no SELECT FOR UPDATE safety assumption there.
    run = CraftRun.objects.filter(status__in=['QUEUED', 'CHECKPOINTED']).order_by('created_at').first()
    if not run:
        return None
    if CraftRun.objects.filter(pk=run.pk, status=run.status).update(status='PREPARING', started_at=timezone.now()) != 1:
        return None
    run.refresh_from_db()
    try:
        source_current = fingerprint(request_recipe(run.scope)) == run.source_hash
    except (ValidationError, OSError, ValueError, KeyError):
        source_current = False
    if (fingerprint(run.snapshot) != run.input_hash or run.snapshot.get('implementation_hash') != implementation_hash()
            or not source_current):
        run.status = 'FAILED'
        run.result = {'reason': 'SNAPSHOT_HASH_MISMATCH'}
        run.finished_at = timezone.now()
        run.save()
        return None
    if run.scope.environment in ('TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE'):
        try:
            current = global_state_for(run.scope, run.snapshot['selected_keys'])
            if current['fingerprint'] != run.snapshot['global_resource_state']['fingerprint']:
                raise ValidationError('GLOBAL_STATE_STALE')
        except (ValidationError, GlobalStateError, KeyError):
            run.status = 'STALE'
            run.result = {'reason': 'GLOBAL_STATE_STALE'}
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'result', 'finished_at'])
            return None
    return run


@transaction.atomic
def expire_abandoned_runs():
    """Fail closed after a crashed worker; never retry automatically or promote."""
    cutoff = timezone.now() - timedelta(minutes=10)
    runs = list(CraftRun.objects.filter(status='PREPARING', started_at__lt=cutoff))
    for run in runs:
        checkpoint = run.result.get('checkpoint') if run.snapshot.get('cycle_target') else None
        recovered = bool(checkpoint and run.result.get('controller_status') == 'CHECKPOINTED')
        changed = CraftRun.objects.filter(pk=run.pk, status='PREPARING', started_at__lt=cutoff).update(
            status='CHECKPOINTED' if recovered else 'FAILED',
            finished_at=None if recovered else timezone.now(),
            result={**run.result, 'reason': 'WORKER_LEASE_EXPIRED'} if recovered else
                   {'reason': 'WORKER_LEASE_EXPIRED'})
        if changed:
            AuditEvent.objects.create(scope=run.scope, action='WORKER_LEASE_EXPIRED', entity_id=str(run.pk),
                                      reason='Worker exceeded maximum runtime; manual new request required')
    return len(runs)


def evidence_valid(result):
    if result.get('source') in ('PINNED_FACTORY1_SIMULATION_NOT_OBSERVED', capture_test_state.SOURCE):
        multi = result.get('multi_window', {})
        windows = multi.get('windows', ())
        rows = result.get('acceptance', {}).get('selected_schedule', ())
        return (bool(windows) and len(windows) == multi.get('windows_accepted')
                and all(w.get('status') == 'ACCEPTED' and w.get('replay') == 'PASS'
                        and w.get('physical') == 'VALID' and w.get('scope') == 'SCOPE_VALID'
                        and w.get('materialization_mismatch_count') == 0 for w in windows)
                and multi.get('independent_assembled', {}).get('status') == 'PASS'
                and result.get('global_conflict_validation', {}).get('status') == 'PASS'
                and result.get('acceptance', {}).get('status') == 'ACCEPTED'
                and len(rows) == len({r['operation_key'] for r in rows}) == multi.get('operations_selected')
                and result.get('quantity', {}).get('status') == 'PASS')
    acceptance = result.get('acceptance', {})
    scope = acceptance.get('validator', {}).get('scope_acceptance', {})
    multi = result.get('multi_window')
    multi_valid = ((multi is None and result.get('source') != 'SIMULATED_FACTORY1_PARTIAL_CHAIN') or (
        result.get('source') == 'SIMULATED_FACTORY1_PARTIAL_CHAIN' and multi is not None
        and multi.get('windows_attempted') == 2
        and all(multi.get(f'window_{i}', {}).get('status') == 'ACCEPTED'
                and multi.get(f'window_{i}', {}).get('replay') == 'PASS'
                and multi.get(f'window_{i}', {}).get('physical') == 'VALID'
                and multi.get(f'window_{i}', {}).get('scope') == 'SCOPE_VALID' for i in (1, 2))
        and multi.get('assembled_independent_validation', {}).get('status') == 'PASS'
        and multi.get('assembled_source_quantity_conserved') is True))
    return (multi_valid and acceptance.get('status') == 'ACCEPTED'
            and acceptance.get('winning_stage', {}).get('status') in ('FEASIBLE', 'OPTIMAL')
            and acceptance.get('materialization', {}).get('mismatch_count') == 0
            and acceptance.get('replay', {}).get('status') == 'PASS'
            and scope.get('scope_status') == 'SCOPE_VALID'
            and scope.get('physical_status') == 'VALID'
            and result.get('quantity', {}).get('status') == 'PASS'
            and bool(acceptance.get('selected_schedule')))


@transaction.atomic
def finish_run(run_id, result):
    run = CraftRun.objects.select_for_update().get(pk=run_id)
    if run.status != 'PREPARING':
        raise ValidationError('RUN_NOT_OWNED_OR_CANCELLED')
    scope = ScheduleScope.objects.select_for_update().get(pk=run.scope_id)
    capture_request = run.snapshot.get('contract') == capture_test_state.CONTRACT
    if capture_request and result.get('checkpoint'):
        try:
            capture_test_state.verify_progress(result['checkpoint'], run.result.get('checkpoint'), run.snapshot)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
        if (fingerprint(run.snapshot) != run.input_hash or
                run.snapshot.get('implementation_hash') != implementation_hash() or
                scope.active_version_id != run.base_version_id or scope.source_hash != run.source_hash or
                global_state_for(scope, run.snapshot['selected_keys'])['fingerprint'] !=
                run.snapshot['global_resource_state']['fingerprint']):
            raise ValidationError('CAPTURE_SNAPSHOT_STALE')
        if result.get('controller_status') in ('CHECKPOINTED', 'COMPUTATIONAL_LIMIT'):
            status = result['controller_status']
            if CraftRun.objects.filter(pk=run.pk, status='PREPARING').update(
                    status=status, result=result, started_at=None,
                    finished_at=None if status == 'CHECKPOINTED' else timezone.now()) != 1:
                raise ValidationError('RUN_STATE_CHANGED')
            AuditEvent.objects.create(scope=scope, action='RUN_' + status, entity_id=str(run.pk),
                                      reason='Tentative captured TEST progress, not manager approval')
            return status
    terminal_adaptive = (run.snapshot.get('adaptive_retries') is True and
                         result.get('controller_status') == 'COMPUTATIONAL_LIMIT' and
                         result.get('checkpoint', {}).get('status') == 'PARTIAL')
    if not capture_request and run.snapshot.get('cycle_target') and (result.get('controller_status') == 'CHECKPOINTED'
                                            or terminal_adaptive):
        from crafter.production.cycles import verify_checkpoint
        new = result.get('checkpoint')
        old = run.result.get('checkpoint')
        options = {'cycle_target': run.snapshot['cycle_target'],
                   'window_size': run.snapshot['window_size']}
        if run.snapshot.get('adaptive_retries'):
            options['adaptive_retries'] = True
        if (not isinstance(new, dict) or not new.get('input_fingerprint') or
                new.get('status') != ('PARTIAL' if terminal_adaptive else 'PENDING') or
                (old and (new['revision'] != old['revision'] + 1 or
                          new['input_fingerprint'] != old['input_fingerprint'] or
                          new['windows'][:old['revision']] != old['windows'])) or
                (not old and new['revision'] != 1)):
            raise ValidationError('CYCLE_CHECKPOINT_INVALID_PROGRESS')
        try:
            verify_checkpoint(new, factory_id=scope.factory_external_id,
                              input_fingerprint=new['input_fingerprint'],
                              operation_keys=run.snapshot['selected_keys'], options=options)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if (fingerprint(run.snapshot) != run.input_hash or
                run.snapshot.get('implementation_hash') != implementation_hash() or
                scope.source_hash != run.source_hash or
                scope.active_version_id != run.base_version_id or
                global_state_for(scope, run.snapshot['selected_keys'])['fingerprint'] !=
                run.snapshot['global_resource_state']['fingerprint']):
            raise ValidationError('CYCLE_SNAPSHOT_STALE')
        accepted_attempt = new['windows'][-1]['status'] == 'ACCEPTED'
        persisted_status = 'COMPUTATIONAL_LIMIT' if terminal_adaptive else 'CHECKPOINTED'
        if CraftRun.objects.filter(pk=run.pk, status='PREPARING').update(
                status=persisted_status, result=result, started_at=None,
                finished_at=timezone.now() if terminal_adaptive else None,
                cp_status=('VALIDATED_WINDOW' if accepted_attempt else 'REJECTED_BOUNDED_ATTEMPT')) != 1:
            raise ValidationError('RUN_STATE_CHANGED')
        AuditEvent.objects.create(scope=scope, action='RUN_CHECKPOINTED', entity_id=str(run.pk),
                                  reason=('Validated tentative window; active schedule unchanged'
                                          if accepted_attempt else
                                          'Rejected bounded attempt recorded; active schedule unchanged'),
                                  details={'revision': new['revision'],
                                           'accepted': len(new['accepted_keys']),
                                           'attempt_status': new['windows'][-1]['status'],
                                           'deferred': new['deferred'],
                                           'run_status': persisted_status})
        return persisted_status
    status = 'VALIDATED' if evidence_valid(result) else 'FAILED'
    expected_result_source = {'FACTORY1_PARTIAL_CHAIN_V1': 'SIMULATED_FACTORY1_PARTIAL_CHAIN',
                              capture_test_state.CONTRACT: capture_test_state.SOURCE,
                              'FACTORY1_GOVERNED_BOUNDED_V1': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                              'SYNTHETIC_PRIORITY_TWO_ORDER_V1': 'SYNTHETIC_PRIORITY_FIXTURE',
                              'SYNTHETIC_GLOBAL_RESOURCE_V1': 'SYNTHETIC_RESOURCE_TEST'}.get(
                                  run.snapshot.get('contract'), 'SYNTHETIC_FIXTURE')
    if status == 'VALIDATED' and result.get('source') != expected_result_source:
        status = 'FAILED'
        result = {'reason': 'RESULT_SOURCE_CONTRACT_MISMATCH'}
    if status == 'VALIDATED' and not priority_result_matches(run.snapshot, result):
        status = 'FAILED'
        result = {'reason': 'PRIORITY_OBJECTIVE_EVIDENCE_MISMATCH'}
    if fingerprint(run.snapshot) != run.input_hash or run.snapshot.get('implementation_hash') != implementation_hash():
        status = 'FAILED'
        result = {'reason': 'SNAPSHOT_OR_IMPLEMENTATION_CHANGED'}
    if result.get('acceptance', {}).get('status') == 'REJECTED':
        status = 'REJECTED'
    if status == 'VALIDATED' and any(
        row.get('resource_configuration', {}).get('factory_id') != scope.factory_external_id
        for row in result['acceptance']['selected_schedule']):
        status = 'FAILED'
        result = {'reason': 'CROSS_FACTORY_CANDIDATE'}
    if status == 'VALIDATED' and scope.environment in ('TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE'):
        try:
            global_state = run.snapshot['global_resource_state']
            report = validate_candidate(global_state, result['acceptance']['selected_schedule'])
            if result.get('global_conflict_validation') != report:
                raise GlobalStateError('GLOBAL_STATE_WORKER_EVIDENCE_MISMATCH')
            if global_state_for(scope, run.snapshot['selected_keys'])['fingerprint'] != global_state['fingerprint']:
                raise GlobalStateError('GLOBAL_STATE_STALE')
            result = {**result, 'global_conflict_validation': report}
        except (GlobalStateError, ValidationError, KeyError) as exc:
            status = 'FAILED'
            result = {'reason': str(exc)}
    if result.get('controller_status') == 'COMPUTATIONAL_LIMIT' and not evidence_valid(result):
        status = 'COMPUTATIONAL_LIMIT'
        if run.result.get('checkpoint'):
            result = {**result, 'checkpoint': run.result['checkpoint']}
    if scope.active_version_id != run.base_version_id or scope.source_hash != run.source_hash:
        status = 'STALE'
    # Compare-and-swap also prevents a late subprocess from undoing cancellation.
    if CraftRun.objects.filter(pk=run.pk, status='PREPARING').update(
        status=status, result=result, finished_at=timezone.now(),
        cp_status=result.get('acceptance', {}).get('winning_stage', {}).get('status', 'NOT_REACHED')) != 1:
        raise ValidationError('RUN_STATE_CHANGED')
    if status == 'VALIDATED':
        operations = result['acceptance']['selected_schedule']
        if scope.environment in ('TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE') and scope.active_version:
            selected = set(run.snapshot['selected_keys'])
            operations = [row for row in scope.active_version.operations
                          if row['operation_key'] not in selected] + operations
        ScheduleVersion.objects.create(scope=scope, run=run, operations=operations, evidence=result,
                                       payload_hash=fingerprint({'operations': operations, 'evidence': result}))
    AuditEvent.objects.create(scope=scope, action='RUN_' + status, entity_id=str(run.pk),
                              reason='Isolated worker completed; active schedule unchanged')
    return status


@transaction.atomic
def cancel_run(user, run_id, reason):
    run = CraftRun.objects.get(pk=run_id)
    authorize(user, run.scope, 'request_run')
    if not reason.strip():
        raise ValidationError('Reason required')
    changed = CraftRun.objects.filter(pk=run.pk, status__in=['QUEUED', 'PREPARING', 'CHECKPOINTED']).update(
        status='CANCELLED', finished_at=timezone.now())
    if changed:
        AuditEvent.objects.create(scope=run.scope, actor=user, action='RUN_CANCELLED', entity_id=str(run.pk), reason=reason)
    return bool(changed)


@transaction.atomic
def promote(user, candidate_id, reason):
    candidate = ScheduleVersion.objects.select_for_update().select_related('run').get(pk=candidate_id)
    scope = ScheduleScope.objects.select_for_update().get(pk=candidate.scope_id)
    authorize(user, scope, 'promote_schedule')
    if scope.environment not in ('TEST', 'TEST_SIM', 'TEST_PRIORITY', 'TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE'):
        raise ValidationError('PRODUCTION_ACTIVATION_DISABLED')
    if not reason.strip():
        raise ValidationError('Manager approval reason required')
    if scope.active_version_id == candidate.pk and candidate.status == 'ACTIVE':
        return candidate  # retry, no duplicate audit or second active schedule
    if candidate.status != 'VALIDATED_CANDIDATE' or not evidence_valid(candidate.evidence):
        raise ValidationError('CANDIDATE_NOT_VALIDATED')
    if candidate.payload_hash != fingerprint({'operations': candidate.operations, 'evidence': candidate.evidence}):
        raise ValidationError('CANDIDATE_HASH_MISMATCH')
    run = candidate.run
    if run.scope_id != scope.pk:
        raise ValidationError('CROSS_FACTORY_CANDIDATE')
    expected_result_source = {'FACTORY1_PARTIAL_CHAIN_V1': 'SIMULATED_FACTORY1_PARTIAL_CHAIN',
                              capture_test_state.CONTRACT: capture_test_state.SOURCE,
                              'FACTORY1_GOVERNED_BOUNDED_V1': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                              'SYNTHETIC_PRIORITY_TWO_ORDER_V1': 'SYNTHETIC_PRIORITY_FIXTURE',
                              'SYNTHETIC_GLOBAL_RESOURCE_V1': 'SYNTHETIC_RESOURCE_TEST'}.get(
                                  run.snapshot.get('contract'), 'SYNTHETIC_FIXTURE')
    if candidate.evidence.get('source') != expected_result_source:
        raise ValidationError('RESULT_SOURCE_CONTRACT_MISMATCH')
    if not priority_result_matches(run.snapshot, candidate.evidence):
        raise ValidationError('PRIORITY_OBJECTIVE_EVIDENCE_MISMATCH')
    if scope.source_hash != run.source_hash or scope.active_version_id != run.base_version_id:
        raise ValidationError('STALE_CANDIDATE')
    if fingerprint(request_recipe(scope)) != scope.source_hash:
        raise ValidationError('SOURCE_STATE_CHANGED')
    if scope.environment in ('TEST_RESOURCE', 'TEST_FACTORY1', 'TEST_CAPTURE'):
        try:
            global_state = run.snapshot['global_resource_state']
            report = validate_candidate(global_state, candidate.evidence['acceptance']['selected_schedule'])
            selected = set(run.snapshot['selected_keys'])
            expected_operations = ([row for row in (scope.active_version.operations if scope.active_version else ())
                                    if row['operation_key'] not in selected] +
                                   candidate.evidence['acceptance']['selected_schedule'])
            if candidate.operations != expected_operations:
                raise GlobalStateError('GLOBAL_STATE_ASSEMBLED_OPERATIONS_CHANGED')
            if (global_state_for(scope, run.snapshot['selected_keys'])['fingerprint'] != global_state['fingerprint'] or
                    candidate.evidence.get('global_conflict_validation') != report):
                raise GlobalStateError('GLOBAL_STATE_STALE')
        except (GlobalStateError, KeyError) as exc:
            raise ValidationError(str(exc)) from exc
    previous = scope.active_version_id
    # Atomic compare-and-swap is authoritative, including on dev SQLite.
    changed = ScheduleScope.objects.filter(pk=scope.pk, source_hash=run.source_hash,
                                           active_version_id=run.base_version_id).update(active_version=candidate)
    if changed != 1:
        raise ValidationError('STALE_CANDIDATE')
    if previous:
        ScheduleVersion.objects.filter(pk=previous).update(status='ARCHIVED')
    candidate.status = 'ACTIVE'
    candidate.save(update_fields=['status'])
    ScheduleVersion.objects.filter(scope=scope, status='VALIDATED_CANDIDATE').exclude(pk=candidate.pk).update(status='STALE')
    AuditEvent.objects.create(scope=scope, actor=user, action='SCHEDULE_PROMOTED', entity_id=str(candidate.pk),
                              reason=reason, details={'previous': str(previous) if previous else None, 'environment': 'TEST'})
    return candidate


@transaction.atomic
def reject(user, candidate_id, reason):
    candidate = ScheduleVersion.objects.select_for_update().get(pk=candidate_id)
    authorize(user, candidate.scope, 'promote_schedule')
    if not reason.strip():
        raise ValidationError('Rejection reason required')
    if candidate.status not in ('VALIDATED_CANDIDATE', 'STALE'):
        raise ValidationError('CANDIDATE_NOT_REJECTABLE')
    candidate.status = 'REJECTED'
    candidate.save(update_fields=['status'])
    AuditEvent.objects.create(scope=candidate.scope, actor=user, action='CANDIDATE_REJECTED', entity_id=str(candidate.pk), reason=reason)
    return candidate
