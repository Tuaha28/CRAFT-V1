"""Actual pinned-source CP-SAT rejection, recovery and governed TEST approval."""
import json
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from governance.models import AuditEvent, FactoryAccess, ScheduleScope, ScheduleVersion
from governance.services import (claim_run, expire_abandoned_runs, finish_run, fingerprint,
                                  promote, request_recipe, request_run)
from governance.test_factory1_cycles import KEYS, ROOT
from crafter.production.cycles import new_checkpoint, record_window


class AdaptiveCheckpointAdmissionTests(TestCase):
    """Accounting/admission adversaries; no claims of physically solved rows."""
    def setUp(self):
        user = get_user_model().objects.create_user('checkpoint_manager', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(codename='request_run'))
        self.scope = ScheduleScope.objects.create(
            factory_external_id=1, environment='TEST_FACTORY1', source_hash='initial',
            resource_state={'classification': 'SIMULATED_TEST',
                            'production_admission_enabled': False,
                            'resource_policy': 'stage_b_adaptive', 'selected_keys': KEYS[:2],
                            'cycle_target': 2, 'window_size': 2, 'adaptive_retries': True})
        FactoryAccess.objects.create(user=user, scope=self.scope)
        self.scope.source_hash = fingerprint(request_recipe(self.scope))
        self.scope.save(update_fields=['source_hash'])
        self.run, _ = request_run(user, self.scope.pk, 'admission', request_recipe(self.scope))
        self.cp = new_checkpoint(factory_id=1, input_fingerprint='accounting-test-only',
                                  operation_keys=KEYS[:2],
                                  options=dict(cycle_target=2, window_size=2, adaptive_retries=True))

    def reject_next(self):
        self.cp = record_window(self.cp, dict(keys=self.cp['pending_windows'][0]['keys'],
                                status='REJECTED', selected_schedule=[],
                                failure_reason='COMPUTATIONAL_LIMIT'), independent_status='NOT_REACHED')
        claim_run()
        return dict(controller_status='COMPUTATIONAL_LIMIT' if self.cp['status'] == 'PARTIAL'
                    else 'CHECKPOINTED', checkpoint=self.cp)

    def test_terminal_deferrals_persist_without_candidate_or_approval(self):
        self.assertEqual(finish_run(self.run.pk, self.reject_next()), 'CHECKPOINTED')
        self.assertEqual(finish_run(self.run.pk, self.reject_next()), 'CHECKPOINTED')
        self.assertEqual(finish_run(self.run.pk, self.reject_next()), 'COMPUTATIONAL_LIMIT')
        self.run.refresh_from_db()
        self.assertEqual(self.run.result['checkpoint']['revision'], 3)
        self.assertEqual(set(self.run.result['checkpoint']['deferred']), set(KEYS[:2]))
        self.assertIsNone(claim_run())
        self.assertFalse(ScheduleVersion.objects.filter(scope=self.scope).exists())

    def test_stale_rejected_attempt_cannot_replace_checkpoint(self):
        result = self.reject_next()
        self.scope.resource_revision += 1
        self.scope.save(update_fields=['resource_revision'])
        with self.assertRaisesRegex(ValidationError, 'CYCLE_SNAPSHOT_STALE'):
            finish_run(self.run.pk, result)
        self.run.refresh_from_db()
        self.assertNotIn('checkpoint', self.run.result)

    def test_repeated_attempt_is_not_persisted_twice(self):
        result = self.reject_next()
        finish_run(self.run.pk, result)
        claim_run()
        with self.assertRaisesRegex(ValidationError, 'CYCLE_CHECKPOINT_INVALID_PROGRESS'):
            finish_run(self.run.pk, result)
        self.run.refresh_from_db()
        self.assertEqual(self.run.result['checkpoint']['revision'], 1)


class AdaptiveCycleIntegrationTests(TestCase):
    def test_real_unknown_then_smaller_windows_restart_and_manager_approval(self):
        user = get_user_model().objects.create_user('adaptive_manager', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        scope = ScheduleScope.objects.create(
            factory_external_id=1, environment='TEST_FACTORY1', source_hash='initial',
            resource_state={'classification': 'SIMULATED_TEST',
                            'production_admission_enabled': False,
                            'resource_policy': 'stage_b_adaptive', 'selected_keys': KEYS,
                            'cycle_target': 4, 'window_size': 4, 'adaptive_retries': True})
        FactoryAccess.objects.create(user=user, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])
        run, _ = request_run(user, scope.pk, 'adaptive-recovery', request_recipe(scope))
        same, created = request_run(user, scope.pk, 'adaptive-recovery', request_recipe(scope))
        self.assertFalse(created)
        self.assertEqual(same.pk, run.pk)
        self.assertEqual(claim_run().pk, run.pk)
        scheduler = ROOT / 'venv/Scripts/python.exe'
        with tempfile.TemporaryDirectory(prefix='craft-budget-test-') as directory:
            snapshot_path, result_path = Path(directory) / 'snapshot.json', Path(directory) / 'result.json'
            snapshot_path.write_text(json.dumps(run.snapshot), encoding='utf-8')
            child = subprocess.run([str(scheduler), str(ROOT / 'web_v1/audits/governed_adaptive_recovery_v1/budget_probe.py'),
                                    str(snapshot_path), str(result_path)], cwd=ROOT,
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(child.returncode, 0, child.stderr[-6000:])
            failed_attempt = json.loads(result_path.read_text(encoding='utf-8'))
        self.assertEqual(finish_run(run.pk, failed_attempt), 'CHECKPOINTED')
        run.refresh_from_db()
        self.assertEqual(run.cp_status, 'REJECTED_BOUNDED_ATTEMPT')
        self.assertEqual(run.result['checkpoint']['accepted_keys'], [])
        self.assertFalse(ScheduleVersion.objects.filter(scope=scope).exists())
        # Abrupt worker loss after durable checkpoint: no reconstructed/mock schedule.
        run.status = 'PREPARING'
        run.started_at = timezone.now() - timedelta(minutes=11)
        run.save(update_fields=['status', 'started_at'])
        self.assertEqual(expire_abandoned_runs(), 1)
        timings = [failed_attempt['runtime_seconds']]
        memory = [failed_attempt['sampled_peak_rss_mb']]
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(scheduler)}):
            for _ in range(12):
                call_command('craft_worker', verbosity=0)
                run.refresh_from_db()
                timings.append(run.result.get('runtime_seconds', 0))
                memory.append(run.result.get('sampled_peak_rss_mb', 0))
                if run.status != 'CHECKPOINTED':
                    break
                self.assertFalse(ScheduleVersion.objects.filter(scope=scope).exists())
        self.assertEqual(run.status, 'VALIDATED', run.result)
        self.assertEqual(run.result['restored_prefix_validation']['status'], 'PASS')
        self.assertEqual(run.result['multi_window']['independent_assembled']['status'], 'PASS')
        self.assertEqual(run.result['global_conflict_validation']['status'], 'PASS')
        cp = run.result['checkpoint']
        self.assertEqual(cp['accepted_keys'], sorted(KEYS))
        self.assertEqual(cp['pending_windows'], [])
        self.assertEqual(cp['deferred'], {})
        self.assertEqual(cp['windows'][0]['status'], 'REJECTED')
        self.assertEqual(cp['windows'][1]['keys'], KEYS[:2])
        self.assertEqual({w['cycle_number'] for w in cp['windows']}, {1, 2})
        candidate = ScheduleVersion.objects.get(run=run)
        self.assertEqual(len(candidate.operations), 8)
        scope.refresh_from_db()
        self.assertIsNone(scope.active_version_id)
        promote(user, candidate.pk, 'Manager-authorized adaptive TEST recovery')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, candidate.pk)
        self.assertEqual(AuditEvent.objects.filter(scope=scope, action='SCHEDULE_PROMOTED').count(), 1)
        summary = {'operations': 8, 'attempts': cp['revision'],
                   'accepted_windows': run.result['multi_window']['windows_accepted'],
                   'cycles': 2, 'runtime_seconds_sum': sum(timings),
                   'max_sampled_rss_mb': max(memory), 'approval': 'TEST_ONLY',
                   'fault_injection': failed_attempt['fault_injection'], 'result': run.result}
        evidence_path = os.environ.get('CRAFT_ADAPTIVE_EVIDENCE_PATH')
        if evidence_path:
            with Path(evidence_path).open('x', encoding='utf-8') as stream:
                json.dump(summary, stream, sort_keys=True, default=str)
        print(json.dumps({k: v for k, v in summary.items() if k != 'result'}))
