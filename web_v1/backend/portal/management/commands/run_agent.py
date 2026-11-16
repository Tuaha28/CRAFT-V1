"""Start the Needle Craft customer assistant (web_v1/agent) for the portal.

    manage.py run_agent --init   add the two portal secrets to backend/.env if they are missing (never printed)
    manage.py run_agent          start the assistant on CRAFT_AGENT_URL's port

The assistant gets its keys as environment variables read from backend/.env: the Groq key (GROQ_API_KEY,
the same one Ask CRAFT uses), the token secret and the service key. No key is copied into another file.
Without a Groq key it runs in its offline mode (handbook answers without the AI model)."""
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

AGENT = Path(settings.BASE_DIR).parent / 'agent'
SECRETS = ('CRAFT_AGENT_JWT_SECRET', 'CRAFT_AGENT_SERVICE_KEY')
# 20b fits Groq's free tier. It answers handbook questions itself; for order questions it often skips the order
# lookup, and the assistant then answers from its own lookup (mode offline_fallback, still the live order record).
# gpt-oss-120b does the lookups but needs about 9,000 tokens a question against the free tier's 8,000 per minute;
# set CRAFT_AGENT_MODEL=openai/gpt-oss-120b in backend/.env on a paid tier.
DEFAULT_MODEL = 'openai/gpt-oss-20b'


def _env_file():
    return Path(settings.ASK_AI_ENV_FILE)


def _values():
    from dotenv import dotenv_values
    path = _env_file()
    return {**(dotenv_values(path) if path.exists() else {}),
            **{k: v for k, v in os.environ.items() if k in (*SECRETS, 'CRAFT_AGENT_MODEL')}}


class Command(BaseCommand):
    help = 'Start the customer assistant service for the portal (--init adds its secrets to backend/.env).'

    def add_arguments(self, parser):
        parser.add_argument('--init', action='store_true', help='add missing portal secrets to backend/.env')
        parser.add_argument('--backend', default='http://127.0.0.1:8002', help='where the portal API runs')

    def handle(self, **options):
        values = _values()
        if options['init']:
            missing = [k for k in SECRETS if len(values.get(k) or '') < 32]
            if missing:
                with open(_env_file(), 'a', encoding='utf-8') as env:
                    env.write('\n# Customer portal (manage.py run_agent --init)\n')
                    for key in missing:
                        env.write(f'{key}={secrets.token_urlsafe(48)}\n')
            self.stdout.write(f'Portal secrets ready in {_env_file()} ({len(missing)} added). '
                              'Restart the API so it reads them.')
            return
        if any(len(values.get(k) or '') < 32 for k in SECRETS):
            raise CommandError('Run "manage.py run_agent --init" first, then restart the API')
        python = AGENT / '.venv' / 'Scripts' / 'python.exe'
        if not python.exists():
            python = AGENT / '.venv' / 'bin' / 'python'
        if not python.exists():
            raise CommandError(f'Create the assistant environment first: see {AGENT / "README.md"}')
        port = str(urlparse(settings.CRAFT_AGENT_URL).port or 8003)
        groq = values.get('GROQ_API_KEY') or ''
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DJANGO_', 'CRAFT_', 'LEGACY_', 'GROQ_'))}
        env.update({
            'APP_ENV': 'local', 'AUTH_MODE': 'jwt', 'JWT_ALGORITHM': 'HS256',
            'JWT_VERIFICATION_KEY': values['CRAFT_AGENT_JWT_SECRET'],
            'JWT_ISSUER': 'needlecraft-portal', 'JWT_AUDIENCE': 'needlecraft-agent',
            'BACKEND_URL': options['backend'], 'BACKEND_SERVICE_KEY': values['CRAFT_AGENT_SERVICE_KEY'],
            'LLM_MODE': 'llm' if groq else 'offline', 'LLM_BASE_URL': 'https://api.groq.com/openai/v1',
            'LLM_API_KEY': groq, 'LLM_MODEL': values.get('CRAFT_AGENT_MODEL') or DEFAULT_MODEL,
            'REQUIRE_GROUNDED_OUTPUT': 'true', 'REQUIRE_SEMANTIC_INDEX': 'false',
            'CORS_ORIGINS': 'http://localhost:5173', 'TAVILY_API_KEY': '',
        })
        self.stdout.write(f'Customer assistant on port {port} ({"AI model" if groq else "offline answers"}), '
                          f'reading orders from {options["backend"]}')
        sys.exit(subprocess.call([str(python), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', port],
                                 cwd=AGENT, env=env))
