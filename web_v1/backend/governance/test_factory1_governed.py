"""Actual application-approved Factory 1 TEST sequence, never legacy DB."""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from integrations.craft.factory1_test_state import approved_boundary, export_state
from integrations.craft.global_resource_state import GlobalStateError, validate_candidate
from governance.models import AuditEvent, CraftRun, FactoryAccess, ScheduleScope, ScheduleVersion
from governance.services import (claim_run, finish_run, fingerprint, promote, request_recipe,
                                 request_run, select_test_factory1_work)

ROOT = Path(__file__).resolve().parents[3]
FIRST = ['790_972', '790_973', '33_3578', '33_3579',
         '790_974', '790_975', '33_3580', '33_3581',
         '790_976', '790_977', '33_3582', '33_3583']
SECOND = ['790_978', '790_979', '790_980', '33_3584',
          '469_1744', '469_1745', '469_1746', '469_1747',
          '469_1748', '469_1749', '469_1750', '469_1751', '469_1752']


class Factory1GovernedIntegrationTests(TestCase):
    def test_real_worker_approved_boundary_second_solve_and_safe_promotion(self):
        self.assertEqual(len(FIRST + SECOND), 25)
        user = get_user_model().objects.create_user('factory1_test_manager', password='test-only')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        scope = ScheduleScope.objects.create(factory_external_id=1, environment='TEST_FACTORY1',
            source_hash='initial', resource_state={
                'classification': 'SIMULATED_TEST', 'production_admission_enabled': False,
                'selected_keys': FIRST})
        FactoryAccess.objects.create(user=user, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])
        worker = ROOT / 'venv/Scripts/python.exe'
        self.assertTrue(worker.exists())

        def execute(key):
            recipe = request_recipe(scope)
            run, created = request_run(user, scope.pk, key, recipe)
            self.assertTrue(created)
            with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(worker)}):
                call_command('craft_worker', verbosity=0)
            run.refresh_from_db()
            self.assertEqual(run.status, 'VALIDATED', run.result)
            candidate = ScheduleVersion.objects.get(run=run)
            self.assertEqual(candidate.evidence['multi_window']['independent_assembled']['status'], 'PASS')
            self.assertEqual(candidate.evidence['global_conflict_validation']['status'], 'PASS')
            self.assertTrue(all(w['replay'] == 'PASS' and w['physical'] == 'VALID'
                                and w['scope'] == 'SCOPE_VALID'
                                for w in candidate.evidence['multi_window']['windows']))
            return run, candidate

        _, first = execute('first-region')
        self.assertEqual(len(first.operations), 12)
        promote(user, first.pk, 'Approved source-derived Factory 1 TEST region')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, first.pk)
        scope = select_test_factory1_work(user, scope.pk, SECOND)
        state = export_state(scope, SECOND)
        self.assertEqual(len(state['approved_outside']), 12)
        self.assertEqual(len(state['fixed_commitments']), 35)
        self.assertEqual(approved_boundary(state)['active_version_id'], str(first.pk))

        second_run, second = execute('second-region')
        self.assertEqual(len(second.operations), 25)
        self.assertEqual(second.operations[:12], first.operations)
        self.assertEqual(second.evidence['multi_window']['operations_selected'], 13)
        self.assertEqual(second.evidence['global_conflict_validation']['outside_commitments'], 47)
        self.assertEqual(second_run.snapshot['global_resource_state']['active_version_id'], str(first.pk))
        # Same sealed snapshot, second request: competing approval must become stale.
        duplicate_run, _ = request_run(user, scope.pk, 'competing-region', request_recipe(scope))
        self.assertEqual(claim_run().pk, duplicate_run.pk)
        self.assertEqual(finish_run(duplicate_run.pk, deepcopy(second_run.result)), 'VALIDATED')
        duplicate = ScheduleVersion.objects.get(run=duplicate_run)

        # A local candidate crossing an outside approved machine reservation
        # fails the *independent* global check even if it has a local solution.
        clash = deepcopy(second_run.result['acceptance']['selected_schedule'])
        candidate = next(row for row in clash if row['operation_key'] == '469_1744')
        outside = next(row for row in state['approved_outside']
                       if row['machine_id'] in state['eligibility']['469_1744']['machines'])
        candidate['start_seconds'] = outside['start_seconds']
        candidate['end_seconds'] = outside['end_seconds']
        candidate['worker_segments'] = [[outside['start_seconds'] + 1,
                                         outside['start_seconds'] + 2]]
        candidate['resource_configuration']['machine_ids'] = [outside['machine_id']]
        candidate['resource_configuration']['line_id'] = outside['line_id']
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_MACHINE_CONFLICT'):
            validate_candidate(state, clash)

        promote(user, second.pk, 'Approved validated second Factory 1 TEST region')
        with self.assertRaises(ValidationError):
            promote(user, duplicate.pk, 'Must be stale')
        scope.refresh_from_db()
        self.assertEqual(scope.active_version_id, second.pk)
        self.assertEqual(ScheduleVersion.objects.filter(scope=scope, status='ACTIVE').count(), 1)
        self.assertEqual(ScheduleVersion.objects.get(pk=first.pk).status, 'ARCHIVED')
        self.assertEqual(AuditEvent.objects.filter(scope=scope, action='SCHEDULE_PROMOTED').count(), 2)
