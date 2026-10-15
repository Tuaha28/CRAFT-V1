"""Application TEST state only; actual CRAFT worker, no legacy database."""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from integrations.craft.global_resource_state import (
    GlobalStateError, assignment_from_row, seal, validate_candidate,
)
from .models import AuditEvent, FactoryAccess, ScheduleScope, ScheduleVersion
from .resource_fixture import initial_state
from .services import (claim_run, finish_run, fingerprint, global_state_for, promote, request_recipe,
                       request_run, select_test_resource_work)


class GlobalResourceStateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('resource_manager', password='test-only')
        self.user.user_permissions.add(*Permission.objects.filter(
            codename__in=['request_run', 'promote_schedule']))
        state = initial_state()
        probe = ScheduleScope(factory_external_id=1, environment='TEST_RESOURCE', resource_state=state)
        self.scope = ScheduleScope.objects.create(factory_external_id=1, environment='TEST_RESOURCE',
            resource_state=state, source_hash=fingerprint(request_recipe(probe)))
        FactoryAccess.objects.create(user=self.user, scope=self.scope)

    def worker(self, key):
        self.scope.refresh_from_db()
        run, created = request_run(self.user, self.scope.pk, key, request_recipe(self.scope))
        self.assertTrue(created)
        python = Path(__file__).resolve().parents[3] / 'venv/Scripts/python.exe'
        with patch.dict(os.environ, {'CRAFT_WORKER_PYTHON': str(python)}):
            call_command('craft_worker')
        run.refresh_from_db()
        self.assertEqual(run.status, 'VALIDATED', run.result)
        return ScheduleVersion.objects.get(run=run)

    def test_real_worker_approved_a_becomes_boundary_for_b(self):
        a = self.worker('a')
        self.assertEqual(a.evidence['global_conflict_validation']['status'], 'PASS')
        promote(self.user, a.pk, 'Approve first TEST operation')
        self.scope.refresh_from_db()
        select_test_resource_work(self.user, self.scope.pk, 'B_residual')
        b = self.worker('b')
        self.assertEqual(len(b.operations), 2)
        old = next(row for row in b.operations if row['operation_key'] == 'A_residual')
        new = next(row for row in b.operations if row['operation_key'] == 'B_residual')
        self.assertEqual(old, a.operations[0])
        self.assertGreaterEqual(new['start_seconds'], old['end_seconds'])
        self.assertEqual(b.evidence['acceptance']['replay']['status'], 'PASS')
        self.assertEqual(b.evidence['acceptance']['validator']['scope_acceptance']['physical_status'], 'VALID')
        self.assertEqual(b.evidence['acceptance']['validator']['scope_acceptance']['scope_status'], 'SCOPE_VALID')
        promote(self.user, b.pk, 'Approve second TEST operation')
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, b.pk)
        self.assertEqual(ScheduleVersion.objects.get(pk=a.pk).status, 'ARCHIVED')
        self.assertEqual(AuditEvent.objects.filter(action='SCHEDULE_PROMOTED').count(), 2)

    def test_machine_and_worker_overlap_have_precise_diagnostics(self):
        snapshot = global_state_for(self.scope, ['A_residual'])
        row = {'operation_key': 'A_residual', 'start_seconds': 300, 'end_seconds': 900,
               'resource_configuration': {'factory_id': 1, 'machine_ids': [1],
                                          'worker_ids': ['1'], 'line_id': 1},
               'worker_segments': [[300, 900]]}
        machine = deepcopy(snapshot)
        machine['approved_outside'] = [{**assignment_from_row(row, 1),
                                        'operation_key': 'B_residual', 'worker_segments': [],
                                        'approved_version_id': 'test-active'}]
        machine['active_version_id'] = 'test-active'
        machine['schedule_disposition']['B_residual'] = 'APPROVED'
        machine = seal({k: v for k, v in machine.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_MACHINE_CONFLICT'):
            validate_candidate(machine, [row])
        worker = deepcopy(snapshot)
        worker['approved_outside'] = [{**assignment_from_row(row, 1),
                                       'operation_key': 'B_residual', 'machine_id': 2,
                                       'approved_version_id': 'test-active'}]
        worker['active_version_id'] = 'test-active'
        worker['schedule_disposition']['B_residual'] = 'APPROVED'
        worker['eligibility']['B_residual']['machines'].append(2)
        worker['machine_lines']['2'] = 1
        worker = seal({k: v for k, v in worker.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_WORKER_CONFLICT'):
            validate_candidate(worker, [row])

    def test_missing_route_and_fixed_hold_rejected(self):
        a = self.worker('a')
        row = a.evidence['acceptance']['selected_schedule'][0]
        snapshot = global_state_for(self.scope, ['A_residual'])
        missing = deepcopy(snapshot)
        del missing['route_releases']['A_residual']
        missing = seal({k: v for k, v in missing.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_ROUTE_BOUNDARY_MISSING'):
            validate_candidate(missing, [row])
        outside_predecessor = deepcopy(snapshot)
        outside_predecessor['route_edges']['A_residual']['predecessor'] = 'outside_unresolved'
        outside_predecessor = seal({k: v for k, v in outside_predecessor.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_EXTERNAL_PREDECESSOR_UNRESOLVED'):
            validate_candidate(outside_predecessor, [row])
        hold = deepcopy(snapshot)
        hold['fixed_commitments'][0]['end_seconds'] = 700
        hold['fixed_commitments'][0]['worker_segments'] = [['1', 0, 700]]
        hold = seal({k: v for k, v in hold.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_MACHINE_CONFLICT'):
            validate_candidate(hold, [row])

    def test_state_revision_stales_candidate_and_preserves_active(self):
        a = self.worker('a')
        promote(self.user, a.pk, 'Approve first TEST operation')
        self.scope.refresh_from_db()
        select_test_resource_work(self.user, self.scope.pk, 'B_residual')
        b = self.worker('b')
        self.scope.refresh_from_db()
        self.scope.resource_revision += 1
        self.scope.save(update_fields=['resource_revision'])
        with self.assertRaisesRegex(ValidationError, 'GLOBAL_STATE_STALE'):
            promote(self.user, b.pk, 'Must reject stale state')
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, a.pk)

    def test_factory_isolation_and_quantity_tamper(self):
        snapshot = global_state_for(self.scope, ['A_residual'])
        other = deepcopy(snapshot)
        other['fixed_commitments'][0]['factory_id'] = 2
        other = seal({k: v for k, v in other.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_CROSS_FACTORY'):
            validate_candidate(other, [])
        quantity = deepcopy(snapshot)
        quantity['operation_ledger']['A_residual']['remaining'] = 61
        quantity = seal({k: v for k, v in quantity.items() if k != 'fingerprint'})
        with self.assertRaisesRegex(GlobalStateError, 'GLOBAL_STATE_QUANTITY_LEDGER'):
            validate_candidate(quantity, [])

    def test_worker_failure_preserves_approved_version(self):
        a = self.worker('a')
        promote(self.user, a.pk, 'Approve first TEST operation')
        self.scope.refresh_from_db()
        select_test_resource_work(self.user, self.scope.pk, 'B_residual')
        self.scope.refresh_from_db()
        run, _ = request_run(self.user, self.scope.pk, 'worker-failure', request_recipe(self.scope))
        self.assertEqual(claim_run().pk, run.pk)
        self.assertEqual(finish_run(run.pk, {'reason': 'WORKER_PROCESS_FAILED'}), 'FAILED')
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.active_version_id, a.pk)
        self.assertEqual(ScheduleVersion.objects.filter(status='ACTIVE').count(), 1)

    def test_missing_snapshot_evidence_rejected_before_approval(self):
        a = self.worker('a')
        ScheduleVersion.objects.filter(pk=a.pk).update(evidence={**a.evidence, 'global_conflict_validation': {}})
        a.refresh_from_db()
        a.payload_hash = fingerprint({'operations': a.operations, 'evidence': a.evidence})
        a.save(update_fields=['payload_hash'])
        with self.assertRaisesRegex(ValidationError, 'GLOBAL_STATE_STALE'):
            promote(self.user, a.pk, 'Cannot approve missing global evidence')
        self.assertIsNone(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id)

    def test_worker_result_without_global_evidence_is_not_persisted(self):
        candidate = self.worker('valid-worker-output')
        run, _ = request_run(self.user, self.scope.pk, 'missing-global', request_recipe(self.scope))
        self.assertEqual(claim_run().pk, run.pk)
        result = deepcopy(candidate.evidence)
        del result['global_conflict_validation']
        self.assertEqual(finish_run(run.pk, result), 'FAILED')
        run.refresh_from_db()
        self.assertEqual(run.result['reason'], 'GLOBAL_STATE_WORKER_EVIDENCE_MISMATCH')
        self.assertEqual(ScheduleVersion.objects.count(), 1)
        self.assertIsNone(ScheduleScope.objects.get(pk=self.scope.pk).active_version_id)
