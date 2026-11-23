"""Opt-in application-owned governance DB. Never registers a legacy DB alias."""
import os
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY is required for integration settings')
DEBUG = os.environ.get('DJANGO_DEBUG') == '1'
INSTALLED_APPS = [*INSTALLED_APPS, 'django.contrib.auth', 'django.contrib.sessions', 'governance', 'planning', 'erp', 'floor', 'portal', 'sim']
MIDDLEWARE = [*MIDDLEWARE, 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware',
              'portal.middleware.CustomerGuard', 'governance.middleware.ApiSecurityHeaders',
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
# Extra page addresses allowed to sign in (comma-separated), e.g. a second copy on another port for tests.
CSRF_TRUSTED_ORIGINS += [o.strip() for o in os.environ.get('CRAFT_EXTRA_ORIGINS', '').split(',') if o.strip()]
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


def _local(name, default=''):
    """A setting from the environment, else from web_v1/backend/.env (gitignored), else the default."""
    if os.environ.get(name) is not None:
        return os.environ[name]
    try:
        from dotenv import dotenv_values
        return dotenv_values(ASK_AI_ENV_FILE).get(name) or default
    except (ImportError, OSError):
        return default


# Demo copies: <database>_demo, created by the user (web_v1/docs/demo_copies.md). Off until switched on.
CRAFT_DEMO_SUFFIX = '_demo'
CRAFT_USE_DEMO_COPIES = _local('CRAFT_USE_DEMO_COPIES') == '1'
ASK_AI_URL = os.environ.get('CRAFT_ASK_URL') or 'https://api.groq.com/openai/v1/chat/completions'
ASK_AI_MODEL = os.environ.get('CRAFT_ASK_MODEL') or 'openai/gpt-oss-20b'
ASK_AI_TIMEOUT = 20

# Email to managers when an automatic CRAFT plan is ready (planning/notify.py). Real SMTP once the
# user puts CRAFT_EMAIL_* in .env; until then messages are saved as files in planning_runs/outbox.
APP_BASE_URL = _local('APP_BASE_URL', 'http://localhost:5173')
DEFAULT_FROM_EMAIL = _local('CRAFT_EMAIL_FROM', 'CRAFT Planner <craft@localhost>')
if _local('CRAFT_EMAIL_HOST'):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = _local('CRAFT_EMAIL_HOST')
    EMAIL_PORT = int(_local('CRAFT_EMAIL_PORT', '587'))
    EMAIL_HOST_USER = _local('CRAFT_EMAIL_USER')
    EMAIL_HOST_PASSWORD = _local('CRAFT_EMAIL_PASSWORD')
    EMAIL_USE_TLS = _local('CRAFT_EMAIL_TLS', '1') == '1'
    EMAIL_TIMEOUT = 20
else:
    EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
    EMAIL_FILE_PATH = str(Path(PLANNING_RUNS_DIR) / 'outbox')

# Auto-planning (planning/autoplan.py): wait this long after the newest trigger, at most this long after the oldest.
AUTO_PLAN_QUIET_MINUTES = int(_local('AUTO_PLAN_QUIET_MINUTES', '15'))
AUTO_PLAN_MAX_WAIT_MINUTES = int(_local('AUTO_PLAN_MAX_WAIT_MINUTES', '60'))
AUTO_PLAN_NEW_ORDER_CHECK_MINUTES = int(_local('AUTO_PLAN_NEW_ORDER_CHECK_MINUTES', '15'))
AUTO_CAPTURE_HOUR = int(_local('AUTO_CAPTURE_HOUR', '2'))
# The factory's clock: the nightly capture hour and the times in e-mails use it (the database stays UTC).
FACTORY_TIME_ZONE = _local('FACTORY_TIME_ZONE', 'Asia/Karachi')

# Write-back (erp/writer.py): approved plans and machine status go into the <database>_demo copies only,
# through the craft_writer account the user creates (web_v1/docs/demo_copies.md). Off until switched on.
CRAFT_WRITEBACK = _local('CRAFT_WRITEBACK') == '1'

# Customer portal: the customer assistant service (web_v1/agent, started with manage.py run_agent).
# Secrets live in backend/.env only: CRAFT_AGENT_JWT_SECRET (32+ characters) signs 10-minute customer tokens,
# CRAFT_AGENT_SERVICE_KEY is what the assistant sends when it reads a customer's orders from /customer/*.
CRAFT_AGENT_URL = _local('CRAFT_AGENT_URL', 'http://127.0.0.1:8003')
CRAFT_AGENT_JWT_SECRET = _local('CRAFT_AGENT_JWT_SECRET')
CRAFT_AGENT_SERVICE_KEY = _local('CRAFT_AGENT_SERVICE_KEY')

# Factory simulator (sim app): realistic defaults; raise them in backend/.env for a livelier demo.
SIM_BREAKDOWN_PER_HOUR = float(_local('SIM_BREAKDOWN_PER_HOUR', '0.0015'))  # per busy machine per working hour
SIM_ABSENCE_PER_DAY = float(_local('SIM_ABSENCE_PER_DAY', '0.02'))  # per planned worker per day

# Auto-planning plans ahead: a first plan of the next AUTO_PLAN_ORDERS orders for a factory without one, and the
# next plan once the approved plan is AUTO_PLAN_NEXT_AT done (share of its pieces recorded).
AUTO_PLAN_ORDERS = int(_local('AUTO_PLAN_ORDERS', '5'))
AUTO_PLAN_NEXT_AT = float(_local('AUTO_PLAN_NEXT_AT', '0.8'))

# Accounts: strong passwords (checked when a password is set or changed) and 12-hour sessions for shared PCs.
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
SESSION_COOKIE_AGE = 12 * 3600
# One cache for every server process, kept in the app database (sign-in locks survive restarts and are shared).
# The table is created by `manage.py createcachetable` (the start script runs it).
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache', 'LOCATION': 'craft_cache',
                      'TIMEOUT': 15 * 60, 'OPTIONS': {'MAX_ENTRIES': 100000}}}
