"""Read-only access to the factory database for the ERP pages.

Every connection goes through LegacyExecutionRepository (the dedicated
craft_reader account, never root), checks that its grants are SELECT-only
before the first query, and runs in a READ ONLY transaction. The database is
chosen per capture and must match the validated source pattern. Credentials
come from the repository .env and never leave this process.
"""
import re
import threading
import time
from contextlib import contextmanager
from django.conf import settings
from integrations.craft.legacy_repository import (LegacyExecutionRepository, SOURCE_DATABASE_PATTERN,
                                                  verify_select_only_grants)


class ErpUnavailable(RuntimeError):
    """The factory database cannot be reached or is not verified read-only."""


def _config(database):
    from dotenv import dotenv_values
    config = dict(dotenv_values(settings.ERP_ENV_FILE))
    config['LEGACY_DB_NAME'] = database
    return config


def live_database(dataset):
    """The database the app reads for a capture: its demo copy when demo copies are switched on."""
    if settings.CRAFT_USE_DEMO_COPIES:
        return dataset.database + settings.CRAFT_DEMO_SUFFIX
    return dataset.database


@contextmanager
def read_only(database):
    if not re.fullmatch(SOURCE_DATABASE_PATTERN, database or ''):
        raise ErpUnavailable('This capture has no matching factory database')
    try:
        repo = LegacyExecutionRepository.from_config(_config(database))
        connection = repo.connection_factory()
    except (PermissionError, ValueError) as exc:
        raise ErpUnavailable(str(exc)) from exc
    except Exception as exc:  # pymysql.err.OperationalError and friends
        raise ErpUnavailable('The factory database is not reachable. Check that MySQL is running.') from exc
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW GRANTS')
            verify_select_only_grants(cursor.fetchall())
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
            cursor.execute('START TRANSACTION READ ONLY')
            yield cursor
            cursor.execute('ROLLBACK')
    except PermissionError as exc:
        raise ErpUnavailable(str(exc)) from exc
    finally:
        connection.close()


_cache, _lock = {}, threading.Lock()


def cached(key, seconds, compute):
    """Small in-process cache; the captured databases do not change while the app runs."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = compute()
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (now + seconds, value)
    return value
