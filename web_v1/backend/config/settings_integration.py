"""Opt-in application-owned governance DB. Never registers a legacy DB alias."""
import os
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY is required for integration settings')
DEBUG = os.environ.get('DJANGO_DEBUG') == '1'
INSTALLED_APPS = [*INSTALLED_APPS, 'django.contrib.auth', 'django.contrib.sessions', 'governance']
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
ROOT_URLCONF = 'governance.urls'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:5173', 'http://localhost:5173']
CRAFT_PRODUCTION_ACTIVATION = False
