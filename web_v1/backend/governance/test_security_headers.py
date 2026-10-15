"""Read-only HTTP safety checks; not a deployment security certification."""
from django.conf import settings
from django.test import SimpleTestCase


class IntegrationHeadersTests(SimpleTestCase):
    def test_session_cannot_be_framed_and_never_admits_live_work(self):
        response = self.client.get('/api/v1/governance/session')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertFalse(response.json()['live_admission'])
        self.assertFalse(settings.CRAFT_PRODUCTION_ACTIVATION)

    def test_unauthorized_run_read_is_denied_with_security_header(self):
        response = self.client.get('/api/v1/governance/runs')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['X-Frame-Options'], 'DENY')
