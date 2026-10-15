"""Real worker checkpoint and two-cycle TEST promotion, using pinned source."""
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from governance.models import AuditEvent, FactoryAccess, ScheduleScope, ScheduleVersion
from governance.services import expire_abandoned_runs, fingerprint, finish_run, promote, request_recipe, request_run
from crafter.production.cycles import new_checkpoint, record_window
from integrations.craft.factory1_test_state import pinned_base


ROOT = Path(__file__).resolve().parents[3]
KEYS = ['790_972', '790_973', '33_3578', '33_3579',
        '790_974', '790_975', '33_3580', '33_3581']


class Factory1CycleWorkerTests(TestCase):
    def test_full_ledger_identity_without_source_domain_is_not_admitted(self):
        base = pinned_base()
        admitted = set(base['selected_keys'])
        outside = next(key for key, status in base['execution_states'].items()
                       if status == 'UNSTARTED' and key not in admitted)
        scope = ScheduleScope(factory_external_id=1, environment='TEST_FACTORY1',
                              resource_state={'classification': 'SIMULATED_TEST',
                                              'production_admission_enabled': False,
                                              'resource_policy': 'stage_b_adaptive',
                                              'selected_keys': [outside],
                                              'cycle_target': 200, 'window_size': 25})
        with self.assertRaisesRegex(ValidationError,
                                    'SOURCE_EXECUTION_DOMAIN_NOT_INDEPENDENTLY_VALIDATED'):
            request_recipe(scope)

    def test_checkpoint_rejects_changed_resource_state_without_approval(self):
        user = get_user_model().objects.create_user('stale_cycle_manager', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(codename='request_run'))
        keys = KEYS[:2]
        scope = ScheduleScope.objects.create(
            factory_external_id=1, environment='TEST_FACTORY1', source_hash='initial',
            resource_state={'classification': 'SIMULATED_TEST',
                            'production_admission_enabled': False,
                            'resource_policy': 'stage_b_adaptive',
                            'selected_keys': keys, 'cycle_target': 2, 'window_size': 1})
        FactoryAccess.objects.create(user=user, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])
        run, _ = request_run(user, scope.pk, 'stale-cycle', request_recipe(scope))
        run.status = 'PREPARING'
        run.save(update_fields=['status'])
        cp = new_checkpoint(factory_id=1, input_fingerprint='test-input',
                            operation_keys=keys, options={'cycle_target': 2, 'window_size': 1})
        cp = record_window(cp, {'keys': keys[:1], 'status': 'ACCEPTED',
                                'replay': 'PASS', 'physical': 'VALID',
                                'scope': 'SCOPE_VALID', 'materialization_mismatch_count': 0,
                                'selected_schedule': [{'operation_key': keys[0]}]},
                           independent_status='PASS')
        scope.resource_revision += 1
        scope.save(update_fields=['resource_revision'])
        with self.assertRaisesRegex(ValidationError, 'CYCLE_SNAPSHOT_STALE'):
            finish_run(run.pk, {'controller_status': 'CHECKPOINTED', 'checkpoint': cp})
        run.refresh_from_db()
        scope.refresh_from_db()
        self.assertEqual(run.status, 'PREPARING')
        self.assertIsNone(scope.active_version_id)
        self.assertEqual(ScheduleVersion.objects.filter(scope=scope).count(), 0)

    def test_generalized_eight_operations_two_cycles_and_approval(self):
        user = get_user_model().objects.create_user('cycle_manager', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        scope = ScheduleScope.objects.create(
            factory_external_id=1, environment='TEST_FACTORY1', source_hash='initial',
            resource_state={'classification': 'SIMULATED_TEST',
                            'production_admission_enabled': False,
                            'resource_policy': 'stage_b_adaptive',
                            'selected_keys': KEYS,
                            'cycle_target': 4, 'window_size': 2})
        FactoryAccess.objects.create(user=user, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])
        run, created = request_run(user, scope.pk, 'two-cycle-idempotent', request_recipe(scope))
        assert created
        same, created = request_run(user, scope.pk, 'two-cycle-idempotent', request_recipe(scope))
        assert not created and same.pk == run.pk
        scheduler = ROOT / 'venv/Scripts/python.exe'
        self.assertTrue(scheduler.exists())
        revisions = []
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(scheduler)}):
            for step in range(4):
                call_command('craft_worker', verbosity=0)
                run.refresh_from_db()
                revisions.append(run.result['checkpoint']['revision'])
                if step > 0:
                    self.assertEqual(run.result['restored_prefix_validation']['status'], 'PASS')
                self.assertEqual(run.result['checkpoint']['approval_status'],
                                 'TENTATIVE_NOT_APPROVED')
                if step < 3:
                    self.assertEqual(run.status, 'CHECKPOINTED', run.result)
                    self.assertEqual(ScheduleVersion.objects.filter(scope=scope).count(), 0)
                if step == 0:
                    # Simulate a crash after the durable checkpoint and prove
                    # the next worker resumes the same run and assignment.
                    run.status = 'PREPARING'
                    run.started_at = timezone.now() - timedelta(minutes=11)
                    run.save(update_fields=['status', 'started_at'])
                    self.assertEqual(expire_abandoned_runs(), 1)
                    run.refresh_from_db()
                    self.assertEqual(run.status, 'CHECKPOINTED')
        self.assertEqual(revisions, [1, 2, 3, 4])
        self.assertEqual(run.status, 'VALIDATED', run.result)
        self.assertEqual(run.result['multi_window']['independent_assembled']['status'], 'PASS')
        self.assertEqual(run.result['global_conflict_validation']['status'], 'PASS')
        self.assertEqual([w['cycle_number'] for w in run.result['checkpoint']['windows']],
                         [1, 1, 2, 2])
        candidate = ScheduleVersion.objects.get(run=run)
        self.assertEqual(len(candidate.operations), 8)
        scope.refresh_from_db()
        self.assertIsNone(scope.active_version_id)
        promote(user, candidate.pk, 'Controlled two-cycle TEST approval')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, candidate.pk)
        self.assertEqual(AuditEvent.objects.filter(scope=scope, action='RUN_CHECKPOINTED').count(), 3)
        self.assertEqual(AuditEvent.objects.filter(scope=scope, action='SCHEDULE_PROMOTED').count(), 1)
