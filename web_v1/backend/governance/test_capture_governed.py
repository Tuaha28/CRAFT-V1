"""Pinned six-factory TEST admission and source-bound governance checks.

This suite reads offline captured files only and creates records solely in
Django's disposable test database. The one-worker/one-machine policy remains
explicitly labeled TEST_ASSUMPTION in each generated execution contract.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from governance.models import FactoryAccess, ScheduleScope
from governance.services import (fingerprint, global_state_for, request_recipe,
                                 request_run, select_test_capture_work)
from integrations.craft.capture_test_state import build_base
from integrations.craft.global_resource_state import verify


class CapturedFactoryGovernanceAdmissionTests(TestCase):
    def test_capture_scope_initializer_is_idempotent_and_test_only(self):
        call_command('init_craft_capture_scopes', factory_id=[4], domain_limit=200,
                    initial_target=25, verbosity=0)
        scope = ScheduleScope.objects.get(factory_external_id=4, environment='TEST_CAPTURE')
        first = scope.resource_state['capture_state']['fingerprint']
        self.assertEqual(len(scope.resource_state['selected_keys']), 25)
        self.assertEqual(len(scope.resource_state['capture_state']['selected_keys']), 63)
        self.assertFalse(scope.resource_state['production_admission_enabled'])
        call_command('init_craft_capture_scopes', factory_id=[4], domain_limit=200,
                    initial_target=25, verbosity=0)
        scope.refresh_from_db()
        self.assertEqual(scope.resource_state['capture_state']['fingerprint'], first)

    def test_all_six_factory_snapshots_and_governed_cycle_requests(self):
        manager = get_user_model().objects.create_user('capture_manager', password='test-only')
        manager.user_permissions.add(*Permission.objects.filter(codename='request_run'))
        summaries = []
        for factory_id in range(1, 7):
            base = build_base(factory_id, domain_limit=200)
            domain = base['selected_keys']
            self.assertEqual(base['factory_id'], factory_id)
            self.assertLessEqual(len(domain), 200)
            self.assertEqual(set(base['execution_contracts']), set(domain))
            self.assertTrue(all(contract['evidence_class'] == 'TEST_ASSUMPTION'
                                for contract in base['execution_contracts'].values()))
            self.assertFalse(base.get('production_admission_enabled', False))
            selected = domain[:min(25, len(domain))]
            state = {'classification': 'SIMULATED_TEST', 'production_admission_enabled': False,
                     'capture_state': base, 'selected_keys': selected,
                     'cycle_target': len(selected), 'window_size': min(5, len(selected)),
                     'adaptive_retries': True}
            scope = ScheduleScope.objects.create(factory_external_id=factory_id,
                environment='TEST_CAPTURE', source_hash='pending', resource_state=state)
            FactoryAccess.objects.create(user=manager, scope=scope)
            scope.source_hash = fingerprint(request_recipe(scope))
            scope.save(update_fields=['source_hash'])

            # Selection is versioned/audited, and its predecessor boundary is
            # checked before a run is admitted.
            scope = select_test_capture_work(manager, scope.pk, selected,
                                               window_size=min(5, len(selected)))
            recipe = request_recipe(scope)
            self.assertEqual(recipe['factory_id'], factory_id)
            self.assertEqual(recipe['cycle_target'], len(selected))
            self.assertEqual(scope.source_hash, fingerprint(recipe))
            snapshot = global_state_for(scope, selected)
            self.assertTrue(verify(snapshot))
            self.assertEqual(snapshot['factory_id'], factory_id)
            self.assertEqual(set(snapshot['selected_keys']), set(selected))
            run, created = request_run(manager, scope.pk, f'f{factory_id}-25', recipe)
            self.assertTrue(created)
            self.assertEqual(run.snapshot['global_resource_state']['fingerprint'],
                             snapshot['fingerprint'])
            summaries.append({'factory': factory_id, 'operations': len(selected),
                              'domain': len(domain), 'ledger': len(base['operation_ledger']),
                              'fixed_holds': len(snapshot['fixed_commitments']),
                              'status': 'SNAPSHOT_ADMITTED_TEST_ONLY'})

        self.assertEqual([row['factory'] for row in summaries], list(range(1, 7)))
        print({'capture_governance': summaries})

    def test_selection_rejects_missing_source_predecessor_and_foreign_factory_id(self):
        manager = get_user_model().objects.create_user('capture_boundary_manager', password='test-only')
        manager.user_permissions.add(*Permission.objects.filter(codename='request_run'))
        bases = {factory: build_base(factory, domain_limit=200) for factory in (4, 6)}
        base = bases[4]
        selected = base['selected_keys'][:min(5, len(base['selected_keys']))]
        state = {'classification': 'SIMULATED_TEST', 'production_admission_enabled': False,
                 'capture_state': base, 'selected_keys': selected,
                 'cycle_target': len(selected), 'window_size': min(5, len(selected)),
                 'adaptive_retries': True}
        scope = ScheduleScope.objects.create(factory_external_id=4, environment='TEST_CAPTURE',
                                             source_hash='pending', resource_state=state)
        FactoryAccess.objects.create(user=manager, scope=scope)
        scope.source_hash = fingerprint(request_recipe(scope))
        scope.save(update_fields=['source_hash'])

        later = next((key for key in base['selected_keys']
                      if base['route_edges'][key].get('predecessor') is not None), None)
        self.assertIsNotNone(later)
        with self.assertRaisesRegex(ValidationError, 'CAPTURE_PREDECESSOR_NOT_APPROVED'):
            select_test_capture_work(manager, scope.pk, [later], window_size=1)
        foreign = next((key for key in bases[6]['selected_keys'] if key not in set(base['selected_keys'])), None)
        self.assertIsNotNone(foreign)
        with self.assertRaisesRegex(ValidationError, 'CAPTURE_SELECTION_INVALID_OR_APPROVED'):
            select_test_capture_work(manager, scope.pk, [foreign], window_size=1)
