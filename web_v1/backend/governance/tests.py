"""Governance tests use a throwaway application DB, never the legacy database."""
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
from datetime import timedelta
from unittest.mock import patch
from django.core.management import call_command
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from rest_framework.test import APIClient
from .models import AuditEvent, CraftRun, FactoryAccess, ScheduleScope, ScheduleVersion
from .services import (cancel_run, claim_run, finish_run, fingerprint, promote, request_run,
                       request_recipe, expire_abandoned_runs, reject, record_priority)

RECIPE = {'contract': 'RESIDUAL_VERTICAL_SLICE_V1', 'source': 'SYNTHETIC_FIXTURE'}


class GovernanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('manager', password='test-only-password')
        self.user.user_permissions.add(*Permission.objects.filter(codename__in=['request_run', 'promote_schedule']))
        self.scope = ScheduleScope.objects.create(factory_external_id=1, source_hash=fingerprint(RECIPE))
        FactoryAccess.objects.create(user=self.user, scope=self.scope)
        # Real captured canonical CP/replay/physical/scope evidence, not an invented success object.
        path = Path(__file__).resolve().parents[2] / 'audits/industrial_readiness_v1/fixture_result.json'
        self.result = json.loads(path.read_text(encoding='utf-8'))

    def request(self, key='one'):
        return request_run(self.user, self.scope.pk, key, RECIPE)[0]

    def candidate(self, key='one'):
        run = self.request(key)
        self.assertEqual(claim_run().pk, run.pk)
        self.assertEqual(finish_run(run.pk, deepcopy(self.result)), 'VALIDATED')
        return ScheduleVersion.objects.get(run=run)

    def test_request_idempotency_and_audit(self):
        a = self.request()
        self.assertEqual(a.pk, self.request().pk)
        self.assertEqual(CraftRun.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_unauthorized_and_cross_factory_request(self):
        other = ScheduleScope.objects.create(factory_external_id=2, source_hash=fingerprint(RECIPE))
        with self.assertRaises(PermissionDenied):
            request_run(self.user, other.pk, 'x', RECIPE)
        reader = get_user_model().objects.create_user('reader')
        with self.assertRaises(PermissionDenied):
            request_run(reader, self.scope.pk, 'x', RECIPE)

    def test_real_scope_cannot_be_admitted(self):
        with self.assertRaises(ValidationError):
            request_run(self.user, self.scope.pk, 'live', {'source': 'garment_fyp_db'})

    def test_claim_is_exclusive(self):
        self.request()
        self.assertIsNotNone(claim_run())
        self.assertIsNone(claim_run())

    def test_snapshot_tamper_rejected(self):
        run = self.request()
        CraftRun.objects.filter(pk=run.pk).update(snapshot={})
        self.assertIsNone(claim_run())
        run.refresh_from_db()
        self.assertEqual(run.status, 'FAILED')

    def test_manager_promotion_and_retry(self):
        c = self.candidate()
        self.scope.refresh_from_db()
        self.assertIsNone(self.scope.active_version_id)
        promote(self.user, c.pk, 'Reviewed actual fixture validators')
        promote(self.user, c.pk, 'Retry')
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, c.pk)
        self.assertEqual(AuditEvent.objects.filter(action='SCHEDULE_PROMOTED').count(), 1)

    def test_stale_candidate_preserves_active(self):
        a = self.candidate('a')
        b = self.candidate('b')
        promote(self.user, a.pk, 'Approved')
        with self.assertRaises(ValidationError):
            promote(self.user, b.pk, 'Stale')
        self.assertEqual(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id, a.pk)

    def test_source_change_blocks_promotion(self):
        c = self.candidate()
        ScheduleScope.objects.filter(pk=self.scope.pk).update(source_hash='changed')
        with self.assertRaises(ValidationError):
            promote(self.user, c.pk, 'Changed source')

    def test_missing_validation_blocks_candidate(self):
        self.request()
        run = claim_run()
        del self.result['acceptance']['replay']
        self.assertEqual(finish_run(run.pk, self.result), 'FAILED')
        self.assertFalse(ScheduleVersion.objects.exists())

    def test_limit_not_infeasibility(self):
        self.request()
        run = claim_run()
        self.assertEqual(finish_run(run.pk, {'controller_status': 'COMPUTATIONAL_LIMIT',
            'physical_infeasibility_proven': False}), 'COMPUTATIONAL_LIMIT')
        self.assertFalse(ScheduleVersion.objects.exists())

    def test_cancelled_result_cannot_commit(self):
        run = self.request()
        claim_run()
        cancel_run(self.user, run.pk, 'Operator stopped proof')
        with self.assertRaises(ValidationError):
            finish_run(run.pk, self.result)
        self.assertFalse(ScheduleVersion.objects.exists())

    def test_candidate_tamper_blocks_promotion(self):
        c = self.candidate()
        ScheduleVersion.objects.filter(pk=c.pk).update(operations=[])
        with self.assertRaises(ValidationError):
            promote(self.user, c.pk, 'Tampered')

    def test_unauthorized_promotion(self):
        c = self.candidate()
        reader = get_user_model().objects.create_user('reader')
        with self.assertRaises(PermissionDenied):
            promote(reader, c.pk, 'Forbidden')

    def test_http_request_returns_202_without_solver(self):
        client = APIClient()
        self.assertEqual(client.get('/api/v1/governance/runs').status_code, 403)
        client.force_authenticate(self.user)
        response = client.post('/api/v1/governance/runs', {'scope': self.scope.pk,
            'idempotency_key': 'api', 'snapshot': RECIPE}, format='json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['status'], 'QUEUED')
        self.assertIsNone(response.data['started_at'])

    def test_csrf_login_enforced(self):
        client = APIClient(enforce_csrf_checks=True)
        response = client.post('/api/v1/governance/login', {'username': 'manager', 'password': 'test-only-password'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_worker_crash_expiry_preserves_active(self):
        run = self.request()
        claim_run()
        CraftRun.objects.filter(pk=run.pk).update(started_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(expire_abandoned_runs(), 1)
        with self.assertRaises(ValidationError):
            finish_run(run.pk, self.result)
        self.assertFalse(ScheduleVersion.objects.exists())

    def test_worker_timeout_is_computational_limit_and_preserves_active(self):
        run = self.request('timeout')
        python = Path(__file__).resolve().parents[3] / 'venv/Scripts/python.exe'
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(python)}), \
             patch('governance.management.commands.craft_worker.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='fixture', timeout=180)):
            call_command('craft_worker')
        run.refresh_from_db()
        self.assertEqual(run.status, 'COMPUTATIONAL_LIMIT')
        self.assertEqual(run.cp_status, 'NOT_REACHED')
        self.assertFalse(ScheduleVersion.objects.exists())
        self.assertIsNone(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id)

    def test_actual_background_process_to_manager_promotion(self):
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get('/api/v1/governance/session').data['csrf_token']
        login = client.post('/api/v1/governance/login', {'username': 'manager', 'password': 'test-only-password'},
                            format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(login.status_code, 200)
        csrf = login.json()['csrf_token']
        response = client.post('/api/v1/governance/runs', {'scope': self.scope.pk, 'idempotency_key': 'subprocess',
            'snapshot': RECIPE}, format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 202)
        run = CraftRun.objects.get(pk=response.data['id'])
        python = Path(__file__).resolve().parents[3] / 'venv/Scripts/python.exe'
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(python)}):
            call_command('craft_worker')
        run.refresh_from_db()
        self.assertEqual(run.status, 'VALIDATED', run.result)
        self.assertIn(run.cp_status, ['FEASIBLE', 'OPTIMAL'])
        c = ScheduleVersion.objects.get(run=run)
        listed = client.get('/api/v1/governance/candidates')
        self.assertEqual(str(listed.data['results'][0]['id']), str(c.pk))
        comparison = client.get(f'/api/v1/governance/candidates/{c.pk}/compare')
        self.assertEqual(comparison.data['changed_operation_count'], 2)
        approved = client.post(f'/api/v1/governance/candidates/{c.pk}/promote',
            {'reason': 'Reviewed real fixture replay and independent validators'}, format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(approved.status_code, 200)
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, c.pk)

    def test_rejection_preserves_active_version(self):
        first = self.candidate('first')
        promote(self.user, first.pk, 'Approve first')
        second = self.candidate('second')
        reject(self.user, second.pk, 'Keep previous schedule')
        with self.assertRaises(ValidationError):
            promote(self.user, second.pk, 'Cannot promote rejected')
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, first.pk)

    def test_comparison_exposes_actual_validation_gates(self):
        candidate = self.candidate()
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f'/api/v1/governance/candidates/{candidate.pk}/compare')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['environment'], 'TEST')
        evidence = response.data['validation']
        self.assertEqual(evidence['replay_status'], 'PASS')
        self.assertEqual(evidence['physical_status'], 'VALID')
        self.assertEqual(evidence['scope_status'], 'SCOPE_VALID')
        self.assertEqual(evidence['quantity_status'], 'PASS')
        self.assertEqual(evidence['materialization_mismatch_count'], 0)
        self.assertTrue(evidence['candidate_validated'])

    def test_fixture_factory_mismatch_rejected_even_with_access(self):
        other = ScheduleScope.objects.create(factory_external_id=2, source_hash=fingerprint(RECIPE))
        FactoryAccess.objects.create(user=self.user, scope=other)
        with self.assertRaises(ValidationError):
            request_run(self.user, other.pk, 'wrong-factory', RECIPE)

    def test_replay_failure_and_computational_failure_are_distinct(self):
        run = self.request()
        claim_run()
        result = deepcopy(self.result)
        result['acceptance']['status'] = 'REJECTED'
        result['acceptance']['replay']['status'] = 'FAIL'
        self.assertEqual(finish_run(run.pk, result), 'REJECTED')

    def test_only_application_database_is_configured(self):
        from django.conf import settings
        self.assertEqual(set(settings.DATABASES), {'default'})
        self.assertEqual(settings.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')


class SimulatedFactory1GovernanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('sim_manager', password='test-only-password')
        self.user.user_permissions.add(*Permission.objects.filter(codename__in=['request_run', 'promote_schedule']))
        recipe = request_recipe(ScheduleScope(factory_external_id=1, environment='TEST_SIM'))
        self.recipe = recipe
        self.scope = ScheduleScope.objects.create(factory_external_id=1, environment='TEST_SIM',
                                                  source_hash=fingerprint(recipe))
        FactoryAccess.objects.create(user=self.user, scope=self.scope)

    def test_actual_simulated_factory1_worker_candidate_and_test_promotion(self):
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get('/api/v1/governance/session').data['csrf_token']
        login = client.post('/api/v1/governance/login',
                            {'username': 'sim_manager', 'password': 'test-only-password'},
                            format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(login.status_code, 200)
        csrf = login.json()['csrf_token']
        scopes = client.get('/api/v1/governance/scopes')
        self.assertEqual(scopes.data['results'][0]['request_snapshot'], self.recipe)
        queued = client.post('/api/v1/governance/runs', {'scope': self.scope.pk,
            'idempotency_key': 'factory1-partial-chain', 'snapshot': self.recipe},
            format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(queued.status_code, 202)
        run = CraftRun.objects.get(pk=queued.data['id'])
        python = Path(__file__).resolve().parents[3] / 'venv/Scripts/python.exe'
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(python)}):
            call_command('craft_worker')
        run.refresh_from_db()
        self.assertEqual(run.status, 'VALIDATED', run.result)
        self.assertEqual(run.result['source'], 'SIMULATED_FACTORY1_PARTIAL_CHAIN')
        self.assertEqual(run.result['multi_window']['assembled_independent_validation']['status'], 'PASS')
        candidate = ScheduleVersion.objects.get(run=run)
        self.assertEqual({r['operation_key'] for r in candidate.operations},
                         {'665_2677', '665_2678', '665_2679'})
        self.assertEqual(client.get(f'/api/v1/governance/candidates/{candidate.pk}/compare').data[
            'validation']['candidate_validated'], True)
        self.assertIsNone(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id)
        response = client.post(f'/api/v1/governance/candidates/{candidate.pk}/promote',
            {'reason': 'Approved controlled simulated route only'}, format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 200)
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, candidate.pk)
        self.assertEqual(AuditEvent.objects.filter(scope=self.scope, action='SCHEDULE_PROMOTED').count(), 1)

    def test_missing_multi_window_validation_cannot_create_candidate(self):
        path = Path(__file__).resolve().parents[2] / 'audits/fyp_completion_20261010/worker_result_sim_test.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        result.pop('multi_window')
        run, _ = request_run(self.user, self.scope.pk, 'missing-window', self.recipe)
        claim_run()
        self.assertEqual(finish_run(run.pk, result), 'FAILED')
        self.assertFalse(ScheduleVersion.objects.exists())


class PriorityGovernanceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('priority_manager', password='test-only-password')
        self.user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule', 'change_priority']))
        self.scope = ScheduleScope.objects.create(factory_external_id=1, environment='TEST_PRIORITY',
            source_hash=fingerprint(request_recipe(ScheduleScope(factory_external_id=1,
                environment='TEST_PRIORITY', priority_state={}))), priority_state={})
        FactoryAccess.objects.create(user=self.user, scope=self.scope)
        self.python = Path(__file__).resolve().parents[3] / 'venv/Scripts/python.exe'

    def _worker(self):
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(self.python)}):
            call_command('craft_worker')

    def _request(self, key):
        self.scope.refresh_from_db()
        run, _ = request_run(self.user, self.scope.pk, key, request_recipe(self.scope))
        self._worker()
        run.refresh_from_db()
        self.assertEqual(run.status, 'VALIDATED', run.result)
        return ScheduleVersion.objects.get(run=run)

    def test_baseline_priority_override_worker_comparison_and_test_promotion(self):
        baseline = self._request('baseline')
        self.assertEqual(baseline.evidence['fixture']['order_completion_seconds'], {'A': 1020, 'B': 3120})
        promote(self.user, baseline.pk, 'Approved baseline TEST fixture')
        self.scope.refresh_from_db()
        client = APIClient(enforce_csrf_checks=True)
        csrf = client.get('/api/v1/governance/session').data['csrf_token']
        login = client.post('/api/v1/governance/login',
            {'username': 'priority_manager', 'password': 'test-only-password'},
            format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(login.status_code, 200)
        csrf = login.json()['csrf_token']
        response = client.post(f'/api/v1/governance/scopes/{self.scope.pk}/priority', {
            'order_id': 'B', 'tier': 'URGENT', 'target_due_seconds': 900,
            'reason': 'Controlled urgent delivery test', 'expected_source_hash': self.scope.source_hash,
        }, format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['priority_state']['override']['entered_by'], 'priority_manager')
        urgent = self._request('urgent-after-override')
        self.assertEqual(urgent.evidence['fixture']['order_completion_seconds'], {'A': 3120, 'B': 900})
        self.assertEqual(urgent.evidence['acceptance']['replay']['status'], 'PASS')
        comparison = client.get(f'/api/v1/governance/candidates/{urgent.pk}/compare')
        self.assertEqual(comparison.status_code, 200)
        self.assertGreater(comparison.data['changed_operation_count'], 0)
        self.assertTrue(comparison.data['validation']['candidate_validated'])
        self.assertEqual(comparison.data['priority_impact']['active']['completion_seconds'],
                         {'A': 1020, 'B': 3120})
        self.assertEqual(comparison.data['priority_impact']['candidate']['completion_seconds'],
                         {'A': 3120, 'B': 900})
        self.assertEqual(comparison.data['priority_impact']['candidate']['makespan_seconds'], 3120)
        approved = client.post(f'/api/v1/governance/candidates/{urgent.pk}/promote',
            {'reason': 'Reviewed urgency impact and valid physical schedule'},
            format='json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(approved.status_code, 200, approved.data)
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, urgent.pk)
        self.assertEqual(ScheduleVersion.objects.get(pk=baseline.pk).status, 'ARCHIVED')
        self.assertEqual(AuditEvent.objects.filter(scope=self.scope, action='ORDER_PRIORITY_CHANGED').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(scope=self.scope, action='SCHEDULE_PROMOTED').count(), 2)

    def test_priority_change_stales_existing_candidate_and_rejects_unauthorized_user(self):
        candidate = self._request('before-change')
        reader = get_user_model().objects.create_user('priority_reader', password='test-only-password')
        FactoryAccess.objects.create(user=reader, scope=self.scope)
        with self.assertRaises(PermissionDenied):
            record_priority(reader, self.scope.pk, order_id='B', tier='URGENT',
                            target_due_seconds=900, reason='Not allowed',
                            expected_source_hash=self.scope.source_hash)
        record_priority(self.user, self.scope.pk, order_id='B', tier='URGENT',
                        target_due_seconds=900, reason='Controlled urgency request',
                        expected_source_hash=self.scope.source_hash)
        with self.assertRaises(ValidationError):
            promote(self.user, candidate.pk, 'Candidate is stale')
        self.assertIsNone(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id)
