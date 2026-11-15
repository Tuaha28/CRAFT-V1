"""Ask CRAFT: an AI model reads a question and picks which built-in answer fits.

Only the question text is sent to the model provider. No plan, order or factory
data leaves the server, and the model never writes an answer or a number: it
returns one tool call, which is checked against a fixed list here. The browser
then works out the answer from the app's own data (frontend/src/lib/ask.ts).
"""
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings

TOPICS = ['order', 'crew', 'late', 'risk', 'line', 'busiest', 'built', 'plan', 'stock', 'sales', 'quality',
          'machines', 'people', 'help']
TOOL_NAME = 'answer_from_factory_data'
MAX_QUESTION = 300

SYSTEM = (
    'You route questions for CRAFT, a production planning app for a garment factory. '
    f'Always call the {TOOL_NAME} tool exactly once and write nothing else. Pick the topic: '
    'order = status, finish, due date or lateness of one order (needs order_id); '
    'crew = workers or machines on one order (needs order_id); '
    'late = which orders are late or overdue; risk = orders on time with little spare time; '
    'line = what one production line does (needs line; day if a day is named); '
    'busiest = which line has the most work; built = how the plan was made; plan = overall plan summary; '
    'stock = raw material inventory, stock or shortages (material = the material named, if any); '
    'sales = orders shipped, deliveries, sales or clients; quality = defects and inspections; '
    'machines = machines down or under maintenance; people = staff present, on leave, roles; '
    'help = anything else, including questions unrelated to the factory. '
    'If the user asks about "the worst order" or "the latest order", use topic order with no order_id. '
    'day is the plan day: today = 1, tomorrow = 2, "day 3" = 3.'
)

TOOL = {'type': 'function', 'function': {
    'name': TOOL_NAME,
    'description': 'Show the answer to the question, worked out by the app from the factory data.',
    'parameters': {'type': 'object', 'properties': {
        'topic': {'type': 'string', 'enum': TOPICS},
        'order_id': {'type': 'string', 'description': 'Order number, digits only'},
        'line': {'type': 'integer', 'description': 'Production line number as people say it, e.g. 2 for "line 2"'},
        'day': {'type': 'integer', 'description': 'Plan day: today = 1, tomorrow = 2'},
        'material': {'type': 'string', 'description': 'Material named in the question, e.g. "thread"'},
    }, 'required': ['topic']},
}}


class AssistantUnavailable(Exception):
    """The model could not be reached or gave no usable answer; the app falls back to its own matching."""


def api_key():
    """The provider key: GROQ_API_KEY from the environment, else from web_v1/backend/.env (never committed)."""
    key = os.environ.get('GROQ_API_KEY', '').strip()
    if key:
        return key
    path = Path(settings.ASK_AI_ENV_FILE)
    if path.exists():
        from dotenv import dotenv_values
        return (dotenv_values(path).get('GROQ_API_KEY') or '').strip()
    return ''


def clean(args):
    """Keep only values the tool allows; anything else is dropped."""
    out = {'topic': args.get('topic') if args.get('topic') in TOPICS else 'help'}
    order = str(args.get('order_id') or '').strip().lstrip('#')
    if re.fullmatch(r'\d{1,12}', order):
        out['order_id'] = order
    for name, high in (('line', 999), ('day', 366)):
        value = args.get(name)
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= high:
            out[name] = value
    material = re.sub(r'[^\w\s\-/.]', '', str(args.get('material') or ''))[:60].strip()
    if material:
        out['material'] = material
    return out


def route(question):
    """The model's reading of the question as {'topic', and maybe 'order_id', 'line', 'day', 'material'}."""
    key = api_key()
    if not key:
        raise AssistantUnavailable('AI_OFF')
    body = json.dumps({
        'model': settings.ASK_AI_MODEL, 'temperature': 0, 'max_tokens': 1024,
        'messages': [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': question}],
        'tools': [TOOL], 'tool_choice': 'auto',
    }).encode('utf-8')
    request = urllib.request.Request(settings.ASK_AI_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': 'craft-planner/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=settings.ASK_AI_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise AssistantUnavailable('AI_LIMIT' if exc.code == 429 else f'AI_HTTP_{exc.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise AssistantUnavailable('AI_UNREACHABLE') from None
    try:
        calls = data['choices'][0]['message'].get('tool_calls') or []
        call = next(c for c in calls if c.get('function', {}).get('name') == TOOL_NAME)
        args = json.loads(call['function'].get('arguments') or '{}')
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        raise AssistantUnavailable('AI_NO_TOOL_CALL') from None
    if not isinstance(args, dict):
        raise AssistantUnavailable('AI_NO_TOOL_CALL')
    return clean(args)
