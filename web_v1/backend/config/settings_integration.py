"""Opt-in application-owned governance DB. Never registers a legacy DB alias."""
import os
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY is required for integration settings')
DEBUG = os.environ.get('DJANGO_DEBUG') == '1'
INSTALLED_APPS = [*INSTALLED_APPS, 'django.contrib.auth', 'django.contrib.sessions', 'governance', 'planning', 'erp', 'floor']
MIDDLEWARE = [*MIDDLEWARE, 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware']
X_FRAME_OPTIONS = 'DENY'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                         'NAME': os.environ.get('CRAFT_WEB_DB_PATH') or BASE_DIR / 'craft_web_db.sqlite3'}}
# SQLite is a development application DB only, not a production deployment claim.
REST_FRAMEWORK = {**REST_FRAMEWORK, 'DEFAULT_AUTHENTICATION_CLASSES': [
    'rest_framework.authentication.SessionAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'UNAUTHENTICATED_USER': 'django.contrib.auth.models.AnonymousUser'}
ROOT_URLCONF = 'config.urls_app'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:5173', 'http://localhost:5173']
CRAFT_PRODUCTION_ACTIVATION = False

# CRAFT V3 planning (web app). The scheduler runs in its own virtualenv as a
# subprocess; captures are read-only snapshot folders (see planning/datasets.py).
_REPO = BASE_DIR.parents[1]
PLANNING_SCHEDULER_PYTHON = os.environ.get('CRAFT_WORKER_PYTHON') or str(_REPO / 'venv' / 'Scripts' / 'python.exe')
PLANNING_RUNS_DIR = os.environ.get('CRAFT_PLANNING_RUNS_DIR') or str(BASE_DIR / 'planning_runs')
PLANNING_CAPTURE_ROOTS = [_REPO / 'web_v1' / 'audits' / 'factory_source_integration_v1']
PLANNING_EXPERIMENTS_DIR = os.environ.get('CRAFT_EXPERIMENTS_DIR') or str(_REPO / 'deliverables' / 'paper' / 'results')
# Read-only ERP pages: craft_reader settings from the repository .env (never root, SELECT-only verified).
ERP_ENV_FILE = os.environ.get('CRAFT_ERP_ENV_FILE') or str(_REPO / '.env')
# Ask CRAFT: an AI model only reads the question and picks a built-in answer (assistant/router.py).
# Off unless GROQ_API_KEY is set in the environment or in web_v1/backend/.env (gitignored).
ASK_AI_ENV_FILE = os.environ.get('CRAFT_ASK_ENV_FILE') or str(BASE_DIR / '.env')
ASK_AI_URL = os.environ.get('CRAFT_ASK_URL') or 'https://api.groq.com/openai/v1/chat/completions'
ASK_AI_MODEL = os.environ.get('CRAFT_ASK_MODEL') or 'openai/gpt-oss-20b'
ASK_AI_TIMEOUT = 20
