"""Who may read which pages (least privilege): each role sees what its job needs, and the server enforces it."""
from unittest import mock
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIClient
from erp import views as erp_views
from planning.models import Dataset
from .roles import ROLES, ensure_groups
from .services import _assign

D = 1  # dataset id used in the paths below (created in setUp)


def paths(d):
    return {
        'plans': '/api/v1/planning/runs',
        'activity': '/api/v1/planning/events',
        'overview': f'/api/v1/erp/{d}/6/summary',
        'orders': f'/api/v1/erp/{d}/6/orders',
        'styles': f'/api/v1/erp/{d}/6/styles',
        'workers': f'/api/v1/erp/{d}/6/workforce',
        'machines': f'/api/v1/erp/{d}/6/machines',
        'materials': f'/api/v1/erp/{d}/6/materials',
        'search': f'/api/v1/erp/{d}/6/search?q=a',
    }


# What each role may read (everything else answers 403)
ALLOWED = {
    'admin': {'plans', 'activity', 'overview', 'orders', 'styles', 'workers', 'machines', 'materials', 'search'},
    'manager': {'plans', 'activity', 'overview', 'orders', 'styles', 'workers', 'machines', 'materials', 'search'},
    'planner': {'plans', 'overview', 'orders', 'styles', 'workers', 'machines', 'materials', 'search'},
    'merchandiser': {'plans', 'overview', 'orders', 'styles', 'materials', 'search'},
    'qc': {'orders', 'styles', 'machines', 'materials', 'search'},
    'maintenance': {'machines', 'materials'},
    'supervisor': {'machines', 'materials'},
}


class AccessTests(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x', database='db',
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        groups = ensure_groups()
        self.users = {}
        for role in ROLES:
            user = User.objects.create_user(f'u_{role}')
            _assign(user, role, None, 44 if role == 'supervisor' else None, groups)
            self.users[role] = User.objects.get(pk=user.pk)
        fake = mock.patch.object(erp_views, '_run', side_effect=lambda *a, **k: Response(
            {'orders': [1], 'people': [1], 'machines': [1], 'styles': [1]}))
        fake.start()
        self.addCleanup(fake.stop)

    def client_for(self, role):
        client = APIClient()
        client.force_authenticate(self.users[role])
        return client

    def test_each_role_reads_only_what_its_job_needs(self):
        for role, allowed in ALLOWED.items():
            client = self.client_for(role)
            for name, path in paths(self.dataset.pk).items():
                status = client.get(path).status_code
                if name in allowed:
                    self.assertNotEqual(status, 403, f'{role} should read {name}')
                else:
                    self.assertEqual(status, 403, f'{role} must not read {name}')

    def test_search_hides_people_from_roles_without_the_worker_list(self):
        body = self.client_for('qc').get(paths(self.dataset.pk)['search']).json()
        self.assertNotIn('people', body)
        self.assertIn('orders', body)
        self.assertIn('people', self.client_for('planner').get(paths(self.dataset.pk)['search']).json())

    def test_floor_roles_keep_their_floor_pages(self):
        for role in ('supervisor', 'qc', 'maintenance', 'manager', 'planner', 'admin'):
            status = self.client_for(role).get(f'/api/v1/floor/{self.dataset.pk}/6/issues').status_code
            self.assertNotEqual(status, 403, role)
        self.assertEqual(self.client_for('merchandiser').get(f'/api/v1/floor/{self.dataset.pk}/6/issues').status_code, 403)
