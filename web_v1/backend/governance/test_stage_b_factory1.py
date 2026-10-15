"""TEST-only Stage B resource selection through real governed CRAFT workers."""
import os
import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from integrations.craft.factory1_test_state import export_state
from governance.models import AuditEvent, FactoryAccess, ScheduleScope, ScheduleVersion
from governance.services import fingerprint, promote, request_recipe, request_run, select_test_factory1_work

ROOT = Path(__file__).resolve().parents[3]
FIRST = ['790_972', '790_973', '33_3578', '33_3579',
         '790_974', '790_975', '33_3580', '33_3581',
         '790_976', '790_977', '33_3582', '33_3583']
SECOND = ['790_978', '790_979', '790_980', '33_3584',
          '469_1744', '469_1745', '469_1746', '469_1747',
          '469_1748', '469_1749', '469_1750', '469_1751', '469_1752']


class StageBFactory1GovernedTests(TestCase):
    def test_unknown_resource_policy_is_rejected_before_run_creation(self):
        scope = ScheduleScope(factory_external_id=1, environment='TEST_FACTORY1',
                              resource_state={'classification': 'SIMULATED_TEST',
                                              'production_admission_enabled': False,
                                              'resource_policy': 'unverified',
                                              'selected_keys': FIRST})
        with self.assertRaisesRegex(ValidationError, 'INVALID_FACTORY1_TEST_SELECTION'):
            request_recipe(scope)

    def test_diverse_sample_preserves_manager_approved_outside_work(self):
        self._exercise_governed_schedule('stage_b_diverse')

    def test_adaptive_source_domain_preserves_manager_approved_outside_work(self):
        self._exercise_governed_schedule('stage_b_adaptive')

    def test_checkpointed_adaptive_windows_preserve_approved_outside_work(self):
        self._exercise_governed_schedule('stage_b_adaptive', adaptive_cycles=True)

    def _exercise_governed_schedule(self, policy, adaptive_cycles=False):
        user = get_user_model().objects.create_user('stage_b_factory1', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        scope = ScheduleScope.objects.create(
            factory_external_id=1, environment='TEST_FACTORY1', source_hash='initial',
            resource_state={'classification': 'SIMULATED_TEST',
                            'production_admission_enabled': False,
                            'resource_policy': policy,
                            'selected_keys': FIRST,
                            **({'cycle_target': 8, 'window_size': 4, 'adaptive_retries': True}
                               if adaptive_cycles else {})})
        FactoryAccess.objects.create(user=user, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])
        worker = ROOT / 'venv/Scripts/python.exe'
        self.assertTrue(worker.exists())

        def execute(request_key, count):
            recipe = request_recipe(scope)
            self.assertEqual(recipe['resource_policy'], policy)
            run, created = request_run(user, scope.pk, request_key, recipe)
            self.assertTrue(created)
            started = time.perf_counter()
            with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(worker)}):
                for _ in range(24 if adaptive_cycles else 1):
                    call_command('craft_worker', verbosity=0)
                    run.refresh_from_db()
                    if adaptive_cycles:
                        cp = run.result.get('checkpoint', {})
                        print(json.dumps({'request': request_key, 'status': run.status,
                                          'revision': cp.get('revision'),
                                          'accepted': len(cp.get('accepted_keys', [])),
                                          'elapsed_seconds': time.perf_counter() - started}), flush=True)
                        evidence_dir = os.environ.get('CRAFT_CYCLE_TEST_EVIDENCE_DIR')
                        if evidence_dir:
                            directory = Path(evidence_dir)
                            directory.mkdir(parents=True, exist_ok=True)
                            target = directory / f'{run.pk}_{cp.get("revision", 0)}_{run.status}.json'
                            with target.open('x', encoding='utf-8') as stream:
                                json.dump({'classification': 'DISPOSABLE_APPLICATION_TEST_NOT_LIVE',
                                           'request': request_key, 'status': run.status,
                                           'elapsed_seconds': time.perf_counter() - started,
                                           'result': run.result}, stream, sort_keys=True, default=str)
                    if run.status != 'CHECKPOINTED':
                        break
                    self.assertFalse(ScheduleVersion.objects.filter(run=run).exists())
            run.refresh_from_db()
            self.assertEqual(run.status, 'VALIDATED', run.result)
            candidate = ScheduleVersion.objects.get(run=run)
            evidence = candidate.evidence
            self.assertEqual(evidence['domain']['resource_selection_policy'], policy)
            self.assertEqual(evidence['multi_window']['independent_assembled']['status'], 'PASS')
            self.assertEqual(evidence['global_conflict_validation']['status'], 'PASS')
            self.assertEqual(evidence['multi_window']['operations_selected'], count)
            if adaptive_cycles:
                self.assertEqual(evidence['checkpoint']['status'], 'COMPLETE')
                self.assertEqual(evidence['checkpoint']['contract'], 'CRAFT_CYCLE_CHECKPOINT_V2')
                self.assertEqual(len(evidence['checkpoint']['accepted_keys']), count)
                self.assertEqual(evidence['restored_prefix_validation']['status'], 'PASS')
            if policy == 'stage_b_adaptive':
                self.assertTrue(all(window['resource_domain_v2'] is not None
                                    for window in evidence['multi_window']['windows']))
                self.assertTrue(all(window['resource_domain_v2']['rounds']
                                    for window in evidence['multi_window']['windows']))
            self.assertTrue(all(row['status'] == 'ACCEPTED' and row['replay'] == 'PASS'
                                and row['physical'] == 'VALID' and row['scope'] == 'SCOPE_VALID'
                                for row in evidence['multi_window']['windows']))
            return candidate

        first = execute('stage-b-first', 12)
        self.assertEqual(len(first.operations), 12)
        promote(user, first.pk, 'TEST-only Stage B first region')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, first.pk)

        scope = select_test_factory1_work(user, scope.pk, SECOND)
        state = export_state(scope, SECOND)
        self.assertEqual(len(state['approved_outside']), 12)
        self.assertEqual(len(state['fixed_commitments']), 35)
        second = execute('stage-b-second', 13)
        self.assertEqual(len(second.operations), 25)
        self.assertEqual(second.operations[:12], first.operations)
        self.assertEqual(second.evidence['global_conflict_validation']['outside_commitments'], 47)
        promote(user, second.pk, 'TEST-only Stage B second region')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, second.pk)
        self.assertEqual(ScheduleVersion.objects.filter(scope=scope, status='ACTIVE').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(scope=scope, action='SCHEDULE_PROMOTED').count(), 2)
        evidence_dir = os.environ.get('CRAFT_CYCLE_TEST_EVIDENCE_DIR')
        if adaptive_cycles and evidence_dir:
            # Persist only after BOTH real promotions and all assertions pass.
            # Per-attempt VALIDATED evidence alone does not prove approval.
            summary = {
                'classification': 'DISPOSABLE_APPLICATION_TEST_NOT_LIVE',
                'status': 'PASS', 'first_run': str(first.run_id),
                'second_run': str(second.run_id),
                'first_approved_operations': len(first.operations),
                'consolidated_approved_operations': len(second.operations),
                'outside_assignments_preserved': second.operations[:12] == first.operations,
                'active_versions': ScheduleVersion.objects.filter(scope=scope, status='ACTIVE').count(),
                'promotion_audit_events': AuditEvent.objects.filter(scope=scope, action='SCHEDULE_PROMOTED').count(),
                'global_conflict_validation': second.evidence['global_conflict_validation'],
                'production_activation': 'DISABLED',
            }
            with (Path(evidence_dir) / f'{second.run_id}_approval_PASS.json').open('x', encoding='utf-8') as stream:
                json.dump(summary, stream, sort_keys=True, default=str)
