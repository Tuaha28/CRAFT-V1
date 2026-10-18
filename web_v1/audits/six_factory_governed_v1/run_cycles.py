"""Whole-factory, cycle-by-cycle governed TEST scheduling for captured factories 1-6.

Each factory is scheduled in bounded cycles (prepared domain <= 200
operations, packed CP-SAT windows of --window-size). A fully validated cycle
is manager-approved in its TEST scope, then the domain is rebased past the
approved work. A partially accepted cycle is never promoted as-is: exactly
its accepted, precedence-closed subset is re-run and approved through the
ordinary validated path, and the deferred operations (plus route successors)
are excluded from later cycles with their recorded reasons.

All Django records live in a disposable SQLite file. Source files are the
pinned read-only capture; the legacy MySQL repository is not used.
Evidence is rewritten after every cycle so interrupted runs keep results.
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


def write_evidence(directory, summary):
    temporary = directory / 'cycles.json.partial'
    temporary.write_text(json.dumps(summary, sort_keys=True, indent=2, default=str), encoding='utf-8')
    temporary.replace(directory / 'cycles.json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, help='New, non-existing evidence directory')
    parser.add_argument('--factories', nargs='+', type=int, choices=range(1, 7), default=list(range(1, 7)))
    parser.add_argument('--window-size', type=int, default=15)
    parser.add_argument('--max-cycles', type=int, default=100)
    parser.add_argument('--max-worker-invocations', type=int, default=400)
    # The budget only stops new attempts; a running attempt is never cut. One
    # F6 attempt that escalated to large calendar-regime stages took 462 s, so
    # the timeout must sit far above budget + slowest attempt or the whole
    # invocation (and its checkpoint progress) is lost.
    parser.add_argument('--attempt-budget-seconds', type=int, default=60)
    parser.add_argument('--worker-timeout-seconds', type=int, default=1800)
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    if out.exists():
        raise SystemExit('OUTPUT_EXISTS_REFUSING_OVERWRITE')
    if not 1 <= args.window_size <= 25 or args.attempt_budget_seconds >= args.worker_timeout_seconds:
        raise SystemExit('INVALID_WINDOW_OR_BUDGET')
    out.mkdir(parents=True)

    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_integration'
    os.environ.setdefault('DJANGO_SECRET_KEY', 'isolated-whole-factory-test-only')
    os.environ['CRAFT_WORKER_PYTHON'] = str(SCHEDULER)
    os.environ['CRAFT_ATTEMPT_BUDGET_SECONDS'] = str(args.attempt_budget_seconds)
    os.environ['CRAFT_WORKER_TIMEOUT_SECONDS'] = str(args.worker_timeout_seconds)
    db_temp = tempfile.TemporaryDirectory(prefix='craft-whole-factory-db-')
    os.environ['CRAFT_WEB_DB_PATH'] = str(Path(db_temp.name) / 'governance.sqlite3')
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.core.exceptions import ValidationError
    from django.core.management import call_command
    from governance.models import FactoryAccess, ScheduleScope, ScheduleVersion
    from governance.services import (promote, rebase_test_capture_domain, request_recipe, request_run,
                                     select_test_capture_work)

    summary = {'contract': 'WHOLE_FACTORY_CYCLES_CAPTURE_TEST_V1',
               'classification': 'SIMULATED_TEST_NOT_OBSERVED_FACTORY_EXECUTION',
               'production_activation': 'DISABLED', 'legacy_database_access': 'NONE',
               'policy': {'window_size': args.window_size, 'cycle_domain_limit': 200,
                          'attempt_budget_seconds': args.attempt_budget_seconds,
                          'worker_timeout_seconds': args.worker_timeout_seconds,
                          'recovery': 'HALVING_WITHIN_WINDOW; ACCEPTED_SUBSET_RERUN; DEFERRED_EXCLUDED'},
               'factories': []}
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            call_command('migrate', interactive=False, verbosity=0)
            call_command('init_craft_capture_scopes', factory_id=args.factories, domain_limit=200,
                         initial_target=200, verbosity=0)
        manager = get_user_model().objects.create_user('whole_factory_test_manager', password='TEST_ONLY')
        manager.user_permissions.add(*Permission.objects.filter(codename__in=['request_run', 'promote_schedule']))
        for scope in ScheduleScope.objects.filter(environment='TEST_CAPTURE'):
            FactoryAccess.objects.create(user=manager, scope=scope)

        def run_cycle(scope, keys, label):
            begin = time.perf_counter()
            scope = select_test_capture_work(manager, scope.pk, keys, window_size=min(args.window_size, len(keys)))
            run, created = request_run(manager, scope.pk, label, request_recipe(scope))
            if not created:
                raise RuntimeError('IDEMPOTENCY_COLLISION_IN_DISPOSABLE_DATABASE')
            invocations, errors = 0, []
            while run.status in ('QUEUED', 'CHECKPOINTED') and invocations < args.max_worker_invocations:
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    try:
                        call_command('craft_worker', verbosity=0)
                    except Exception as exc:
                        errors.append(f'{type(exc).__name__}:{exc}')
                invocations += 1
                run.refresh_from_db()
            result = run.result or {}
            capture = result.get('capture_result', {})
            windows = capture.get('windows') or result.get('checkpoint', {}).get('payload', {}).get('windows', [])
            if run.status == 'VALIDATED':
                accepted = [row['operation_key'] for row in result['acceptance']['selected_schedule']]
            else:
                accepted = list(capture.get('scheduled_keys') or [])
            return scope, run, {
                'label': label, 'selected': len(keys), 'status': run.status,
                'reason': result.get('reason'), 'exception': result.get('exception'),
                'exit_code': result.get('exit_code'), 'worker_invocations': invocations,
                'worker_command_errors': errors[-5:],
                'wall_seconds': round(time.perf_counter() - begin, 3),
                'accepted': len(accepted), 'accepted_keys': accepted,
                'window_attempts': len(windows),
                'windows_accepted': sum(1 for w in windows if w.get('accepted')),
                'window_sizes': [len(w.get('operation_keys', ())) for w in windows],
                'cp_statuses': [w.get('cp_statuses') for w in windows],
                'deferred_operations': capture.get('deferred_operations') or {},
                'global_conflict': result.get('global_conflict_validation', {}).get('status'),
                'assembled': result.get('multi_window', {}).get('independent_assembled', {}).get('status'),
                'sampled_peak_rss_mb': result.get('sampled_peak_rss_mb')}

        for factory_id in args.factories:
            scope = ScheduleScope.objects.get(factory_external_id=factory_id, environment='TEST_CAPTURE')
            base = scope.resource_state['capture_state']
            admissible = sum(1 for reason in base['disposition_reasons'].values()
                             if reason == 'SOURCE_ADMISSIBLE_OUTSIDE_CURRENT_PREPARED_CYCLE') + len(base['selected_keys'])
            factory = {'factory_id': factory_id, 'ledger_operations': len(base['operation_ledger']),
                       'source_admissible_unstarted_route_operations': admissible,
                       'cycles': [], 'excluded_deferred': {}, 'stop_reason': None}
            summary['factories'].append(factory)
            factory_start = time.perf_counter()
            for number in range(1, args.max_cycles + 1):
                scope.refresh_from_db()
                domain = list(scope.resource_state['capture_state']['selected_keys'])
                cycle = {'cycle': number, 'prepared_domain': len(domain), 'attempts': []}
                factory['cycles'].append(cycle)
                try:
                    scope, run, attempt = run_cycle(scope, domain, f'f{factory_id}-cycle-{number}')
                    cycle['attempts'].append(attempt)
                    deferred = {}
                    if run.status != 'VALIDATED' and attempt['accepted']:
                        deferred = {k: v for k, v in attempt['deferred_operations'].items()}
                        deferred.update({k: {'reason': 'NOT_REACHED_OR_UNACCOUNTED_IN_PARTIAL_CYCLE'}
                                         for k in set(domain) - set(attempt['accepted_keys']) - set(deferred)})
                        scope, run, retry = run_cycle(scope, attempt['accepted_keys'],
                                                      f'f{factory_id}-cycle-{number}-accepted-subset')
                        cycle['attempts'].append(retry)
                    if run.status == 'VALIDATED':
                        candidate = ScheduleVersion.objects.get(run=run)
                        promote(manager, candidate.pk, f'Authorized TEST-only Factory {factory_id} cycle {number}')
                        scope.refresh_from_db()
                        cycle['approval'] = 'TEST_APPROVED'
                        cycle['approved_total'] = len(scope.active_version.operations)
                    else:
                        cycle['approval'] = 'NO_PROMOTION_ACTIVE_VERSION_PRESERVED'
                        factory['stop_reason'] = attempt.get('reason') or run.status
                        break
                    factory['excluded_deferred'].update(deferred)
                    try:
                        scope = rebase_test_capture_domain(manager, scope.pk, window_size=args.window_size,
                                                           exclude_keys=sorted(deferred))
                    except ValidationError as exc:
                        if 'CAPTURE_DOMAIN_EXHAUSTED' in exc.messages:
                            factory['stop_reason'] = 'ALL_SCHEDULABLE_WORK_PROCESSED'
                            break
                        raise
                except Exception as exc:  # Keep other factories independent.
                    cycle['error'] = f'{type(exc).__name__}:{exc}'
                    factory['stop_reason'] = 'INTEGRATION_EXCEPTION'
                    break
                finally:
                    factory['wall_seconds'] = round(time.perf_counter() - factory_start, 3)
                    scope.refresh_from_db()
                    factory['approved_operations'] = len(scope.active_version.operations) if scope.active_version else 0
                    summary['total_wall_seconds'] = round(time.perf_counter() - started, 3)
                    write_evidence(out, summary)
            else:
                factory['stop_reason'] = 'MAX_CYCLES_REACHED'
            scope.refresh_from_db()
            if scope.active_version:
                (out / f'factory{factory_id}_approved_schedule.json').write_text(
                    json.dumps(scope.active_version.operations, sort_keys=True, indent=1), encoding='utf-8')
            write_evidence(out, summary)
    finally:
        summary['total_wall_seconds'] = round(time.perf_counter() - started, 3)
        write_evidence(out, summary)
        from django.db import connections
        connections.close_all()
        db_temp.cleanup()
    print(json.dumps({'output': str(out), 'factories': [
        {k: f.get(k) for k in ('factory_id', 'approved_operations', 'source_admissible_unstarted_route_operations',
                               'stop_reason', 'wall_seconds')} for f in summary['factories']]}))


if __name__ == '__main__':
    main()
