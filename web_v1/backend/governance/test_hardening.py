"""Sign-in protection and security settings a real deployment expects: a lock after repeated wrong passwords,
password rules, shorter sessions and a content security policy on every API answer."""
import json
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

GOOD = 'Factory-Line-44-ok'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])  # fast, for many sign-ins
class SignInLockTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('demo_manager', password=GOOD)

    def login(self, password, username='demo_manager'):
        return self.client.post('/api/v1/governance/login', json.dumps({'username': username, 'password': password}),
                                content_type='application/json')

    def test_five_wrong_passwords_lock_the_account_for_a_while(self):
        for _ in range(5):
            self.assertEqual(self.login('wrong').status_code, 401)
        locked = self.login(GOOD)  # even the right password waits
        self.assertEqual(locked.status_code, 429)
        self.assertIn('minutes', locked.json()['message'])
        cache.clear()  # the lock time has passed
        self.assertEqual(self.login(GOOD).status_code, 200)

    def test_a_good_sign_in_resets_the_count(self):
        for _ in range(3):
            self.login('wrong')
        self.assertEqual(self.login(GOOD).status_code, 200)
        for _ in range(4):
            self.assertEqual(self.login('wrong').status_code, 401)
        self.assertEqual(self.login(GOOD).status_code, 200)

    def test_the_lock_is_per_account_and_ignores_case(self):
        User.objects.create_user('demo_qc', password=GOOD)
        for _ in range(5):
            self.login('wrong', username='DEMO_MANAGER')
        self.assertEqual(self.login(GOOD).status_code, 429)
        self.assertEqual(self.login(GOOD, username='demo_qc').status_code, 200)


    def test_junk_sign_ins_cannot_push_a_lock_out(self):
        for _ in range(5):
            self.login('wrong')
        for i in range(320):  # unknown names are not counted, so they cannot crowd the lock out
            self.login('wrong', username=f'nobody{i}')
        self.assertEqual(self.login(GOOD).status_code, 429)

    def test_the_lock_is_shared_by_every_server_process(self):
        self.assertTrue(settings.CACHES['default']['BACKEND'].endswith('DatabaseCache'))


class SecuritySettingsTests(TestCase):
    def test_weak_passwords_are_refused(self):
        for weak in ('short', 'password1234', '12345678901234', 'demo_manager1'):
            with self.assertRaises(ValidationError, msg=weak):
                validate_password(weak, user=User(username='demo_manager'))
        validate_password(GOOD, user=User(username='demo_manager'))

    def test_sessions_end_after_12_hours(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 12 * 3600)

    def test_api_answers_carry_a_content_security_policy(self):
        csp = self.client.get('/api/v1/planning/session')['Content-Security-Policy']
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
