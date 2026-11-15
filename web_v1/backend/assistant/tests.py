import io
import json
import urllib.error
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from . import router


def reply(arguments=None, name=router.TOOL_NAME):
    """A provider response carrying one tool call (or none when arguments is None)."""
    message = {'role': 'assistant', 'content': None}
    if arguments is not None:
        message['tool_calls'] = [{'id': 'c1', 'type': 'function',
                                  'function': {'name': name, 'arguments': json.dumps(arguments)}}]
    return io.BytesIO(json.dumps({'choices': [{'message': message}]}).encode())  # works as a context manager


@override_settings(ASK_AI_ENV_FILE='does-not-exist.env')
class AssistantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('asker', password='unused-test-password')
        self.client.force_login(self.user)

    def post(self, question):
        return self.client.post('/api/v1/assistant/route', {'question': question}, content_type='application/json')

    def test_needs_sign_in(self):
        self.client.logout()
        self.assertIn(self.post('which orders are late').status_code, (401, 403))

    def test_off_without_a_key(self):
        with mock.patch.dict('os.environ', {'GROQ_API_KEY': ''}):
            self.assertEqual(self.client.get('/api/v1/assistant/status').json(), {'ai': False})
            response = self.post('which orders are late')
        self.assertEqual((response.status_code, response.json()['error']), (503, 'AI_OFF'))

    def test_sends_only_the_question_and_keeps_allowed_values(self):
        with mock.patch.dict('os.environ', {'GROQ_API_KEY': 'test-key'}), \
                mock.patch('urllib.request.urlopen', return_value=reply({'topic': 'order', 'order_id': '#458', 'line': 0,
                                                                          'day': '2', 'material': 'thread; DROP'})) as call:
            response = self.post('  why is   order 458 late?  ')
        self.assertEqual(response.json(), {'route': {'topic': 'order', 'order_id': '458', 'day': 2, 'material': 'thread DROP'}})
        sent = json.loads(call.call_args.args[0].data)
        self.assertEqual(sent['messages'][1], {'role': 'user', 'content': 'why is order 458 late?'})
        self.assertEqual(len(sent['messages']), 2)  # system prompt and the question: no factory data
        self.assertEqual(sent['tools'][0]['function']['name'], router.TOOL_NAME)
        self.assertEqual(call.call_args.args[0].get_header('Authorization'), 'Bearer test-key')

    def test_unknown_topic_becomes_help(self):
        self.assertEqual(router.clean({'topic': 'write me a poem', 'order_id': 'abc', 'line': True}), {'topic': 'help'})

    def test_failures_fall_back(self):
        limit = urllib.error.HTTPError('u', 429, 'Too Many Requests', {}, None)
        cases = [(mock.Mock(side_effect=limit), 'AI_LIMIT'), (mock.Mock(side_effect=urllib.error.URLError('down')), 'AI_UNREACHABLE'),
                 (mock.Mock(return_value=reply(None)), 'AI_NO_TOOL_CALL'), (mock.Mock(return_value=reply({}, name='other')), 'AI_NO_TOOL_CALL')]
        for urlopen, code in cases:
            with mock.patch.dict('os.environ', {'GROQ_API_KEY': 'test-key'}), mock.patch('urllib.request.urlopen', urlopen):
                response = self.post('which orders are late')
            self.assertEqual((response.status_code, response.json()['error']), (503, code))

    def test_rejects_empty_questions_and_limits_rate(self):
        self.assertEqual(self.post('   ').status_code, 400)
        with mock.patch.dict('os.environ', {'GROQ_API_KEY': ''}):
            codes = [self.post('late?').status_code for _ in range(13)]
        self.assertEqual(codes[-1], 429)
