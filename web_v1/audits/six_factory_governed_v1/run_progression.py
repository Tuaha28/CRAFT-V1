"""Progressive, application-governed TEST scheduling for sealed factories 1-6.

All Django records are placed in a disposable SQLite file. Source files are
read-only captures; the legacy MySQL repository is not configured here.
Each successful cumulative target is promoted only to its factory's TEST scope.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / 'web_v1/backend'
SCHEDULER = ROOT / 'venv/Scripts/python.exe'
sys.path.insert(0, str(BACKEND))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, help='New, non-existing JSON evidence path')
    parser.add_argument('--factories', nargs='+', type=int, choices=range(1, 7),
                        default=list(range(1, 7)))
    parser.add_argument('--targets', nargs='+', type=int, choices=(25, 50, 100, 200),
                        default=[25, 50, 100, 200])
    parser.add_argument('--max-worker-invocations', type=int, default=80)
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit('OUTPUT_EXISTS_REFUSING_OVERWRITE')
    if not SCHEDULER.is_file():
        raise SystemExit('SCHEDULER_VIRTUALENV_MISSING')

    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_integration'
    os.environ.setdefault('DJANGO_SECRET_KEY', 'isolated-six-factory-test-only')
    os.environ['CRAFT_WORKER_PYTHON'] = str(SCHEDULER)
    db_temp = tempfile.TemporaryDirectory(prefix='craft-six-factory-db-')
    os.environ['CRAFT_WEB_DB_PATH'] = str(Path(db_temp.name) / 'governance.sqlite3')
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.core.management import call_command
    from governance.models import AuditEvent, FactoryAccess, ScheduleScope, ScheduleVersion
    from governance.services import promote, request_recipe, request_run, select_test_capture_work

    summary = {'contract': 'SIX_FACTORY_GOVERNED_CAPTURE_TEST_V1',
               'classification': 'SIMULATED_TEST_NOT_OBSERVED_FACTORY_EXECUTION',
               'production_activation': 'DISABLED', 'legacy_database_access': 'NONE',
               'targets': args.targets, 'factories': []}
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            call_command('migrate', interactive=False, verbosity=0)
            call_command('init_craft_capture_scopes', factory_id=args.factories, domain_limit=200,
                         initial_target=min(args.targets), verbosity=0)
        manager = get_user_model().objects.create_user('six_factory_test_manager', password='TEST_ONLY')
        manager.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        for scope in ScheduleScope.objects.filter(environment='TEST_CAPTURE'):
            FactoryAccess.objects.create(user=manager, scope=scope)

        for factory_id in args.factories:
            scope = ScheduleScope.objects.get(factory_external_id=factory_id, environment='TEST_CAPTURE')
            domain = list(scope.resource_state['capture_state']['selected_keys'])
            factory = {'factory_id': factory_id, 'source_hash': scope.resource_state['capture_state']['source_hash'],
                       'scenario_hash': scope.resource_state['capture_state']['scenario_hash'],
                       'prepared_domain': len(domain), 'ledger_operations': len(
                           scope.resource_state['capture_state']['operation_ledger']), 'cycles': []}
            previous_target = 0
            stop_reason = None
            for target in args.targets:
                if target > len(domain):
                    factory['cycles'].append({'target': target, 'status': 'NOT_SOURCE_ADMISSIBLE',
                                              'available_prepared_domain': len(domain)})
                    continue
                if target <= previous_target:
                    continue
                selected = domain[previous_target:target]
                cycle_start = time.perf_counter()
                try:
                    scope = select_test_capture_work(manager, scope.pk, selected,
                                                     window_size=min(25, len(selected)))
                    recipe = request_recipe(scope)
                    run, created = request_run(manager, scope.pk,
                        f'factory-{factory_id}-cumulative-{target}', recipe)
                    if not created:
                        raise RuntimeError('IDEMPOTENCY_COLLISION_IN_DISPOSABLE_DATABASE')
                    invocations = 0
                    worker_log = []
                    while run.status in ('QUEUED', 'CHECKPOINTED') and invocations < args.max_worker_invocations:
                        out, err = io.StringIO(), io.StringIO()
                        started_call = time.perf_counter()
                        command_error = None
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            try:
                                call_command('craft_worker', verbosity=0)
                            except Exception as exc:  # Recorded, then the run state decides.
                                command_error = f'{type(exc).__name__}:{exc}'
                        invocations += 1
                        run.refresh_from_db()
                        worker_log.append({'invocation': invocations, 'status_after': run.status,
                                           'seconds': round(time.perf_counter() - started_call, 3),
                                           'command_error': command_error,
                                           'stdout_tail': out.getvalue()[-500:],
                                           'stderr_tail': err.getvalue()[-2000:]})
                    capture_result = run.result.get('capture_result', {})
                    checkpoint_payload = run.result.get('checkpoint', {}).get('payload', {})
                    attempt_rows = checkpoint_payload.get('windows', capture_result.get('windows', []))
                    cycle = {'target': target, 'delta_selected': len(selected),
                             'status': run.status, 'worker_invocations': invocations,
                             'wall_seconds': round(time.perf_counter() - cycle_start, 3),
                             'controller_status': run.result.get('controller_status'),
                             'reason': run.result.get('reason'),
                             'exit_code': run.result.get('exit_code'),
                             'exception': run.result.get('exception'),
                             'worker_stderr_tail': run.result.get('stderr_tail'),
                             'worker_invocation_log': worker_log,
                             'accepted_total': (len(run.result.get('acceptance', {}).get(
                                 'selected_schedule', [])) + previous_target if run.status == 'VALIDATED' else
                                 len(capture_result.get('selected_schedule', []))),
                             'windows_accepted': run.result.get('multi_window', {}).get(
                                 'windows_accepted', capture_result.get('accepted_windows')),
                             'windows_attempted': len(attempt_rows),
                             'attempts': [{'window': row.get('window', row.get('number')),
                                           'keys': row.get('operation_keys', row.get('operation_keys', row.get('keys'))),
                                           'status': row.get('status'), 'accepted': row.get('accepted'),
                                           'cp_statuses': row.get('cp_statuses'),
                                           'resource_domain': row.get('resource_domain', {}).get('status'),
                                           'domain_complete': row.get('resource_domain', {}).get(
                                               'resource_domain_complete'),
                                           'calendar_regimes': row.get('calendar_regimes'),
                                           'recovery': row.get('recovery'),
                                           'replay': row.get('replay'),
                                           'physical': row.get('physical'),
                                           'scope': row.get('scope'),
                                           'resource_domain_detail': row.get('resource_domain'),
                                           'stages': row.get('stages'),
                                           'complete_domain_diagnostic': row.get('complete_domain_diagnostic')}
                                          for row in attempt_rows],
                             'scheduled_operation_keys': capture_result.get('scheduled_keys'),
                             'deferred_operations': capture_result.get('deferred_operations'),
                             'pending_windows': capture_result.get('pending_windows'),
                             'replay': 'PASS' if run.status == 'VALIDATED' and
                                 all(window.get('replay') == 'PASS' for window in
                                     run.result.get('multi_window', {}).get('windows', [])) else 'NOT_PASS',
                             'physical': 'VALID' if run.status == 'VALIDATED' and
                                 all(window.get('physical') == 'VALID' for window in
                                     run.result.get('multi_window', {}).get('windows', [])) else 'NOT_VALID',
                             'scope': 'SCOPE_VALID' if run.status == 'VALIDATED' and
                                 all(window.get('scope') == 'SCOPE_VALID' for window in
                                     run.result.get('multi_window', {}).get('windows', [])) else 'NOT_VALID',
                             'global_conflict': run.result.get('global_conflict_validation', {}).get('status'),
                             'assembled': run.result.get('multi_window', {}).get(
                                 'independent_assembled', {}).get('status'),
                             'sampled_peak_rss_mb': run.result.get('sampled_peak_rss_mb')}
                    if run.status == 'VALIDATED':
                        candidate = ScheduleVersion.objects.get(run=run)
                        promote(manager, candidate.pk,
                                f'Authorized TEST-only Factory {factory_id} target {target}')
                        scope.refresh_from_db()
                        cycle['approval'] = 'TEST_APPROVED'
                        cycle['active_version'] = str(scope.active_version_id)
                        previous_target = target
                    else:
                        cycle['approval'] = 'NO_PROMOTION_ACTIVE_VERSION_PRESERVED'
                        stop_reason = cycle.get('reason') or run.status
                    factory['cycles'].append(cycle)
                except Exception as exc:  # Keep other factory scopes independent.
                    factory['cycles'].append({'target': target, 'status': 'BLOCKED',
                                              'reason': f'{type(exc).__name__}:{exc}',
                                              'approval': 'NO_PROMOTION'})
                    stop_reason = 'INTEGRATION_EXCEPTION'
                if stop_reason:
                    break
            factory['cumulative_approved_operations'] = previous_target
            factory['active_version'] = str(scope.active_version_id) if scope.active_version_id else None
            factory['audit_promotions'] = AuditEvent.objects.filter(
                scope=scope, action='SCHEDULE_PROMOTED').count()
            factory['stop_reason'] = stop_reason
            summary['factories'].append(factory)
    finally:
        summary['total_wall_seconds'] = round(time.perf_counter() - started, 3)
        with destination.open('x', encoding='utf-8') as stream:
            json.dump(summary, stream, sort_keys=True, indent=2)
        from django.db import connections
        connections.close_all()
        db_temp.cleanup()
    print(json.dumps({'output': str(destination), 'factories': len(summary['factories']),
                      'status': ['PASS' if row['cumulative_approved_operations'] else
                                 'PARTIAL' for row in summary['factories']],
                      'total_wall_seconds': summary['total_wall_seconds']}))


if __name__ == '__main__':
    main()
