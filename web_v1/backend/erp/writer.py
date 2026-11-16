"""Write-back into the demo copies of the factory databases. Never an original.

Tables and columns written (checked on the 30% load database, 2026-11-16):
  production_orders   scheduled_start_date, scheduled_end_date (date)          WHERE production_order_id
  job_shop_schedules  line_id, asset_id, employee_id, planned_start_time, planned_end_time (datetime),
                      planned_work_segments (longtext)                         WHERE production_order_id,
                                                                               operation_id, execution_status
                                                                               in Scheduled/Delayed
  machinery_assets    current_status ('Active' | 'Breakdown')                  WHERE asset_id
A CRAFT step key is "<order>_<operation_id>", the same key the capture groups lots by.

Safety: the database must end with CRAFT_DEMO_SUFFIX, and before writing, the account's grants are
checked: USAGE on *.* and rights on *_demo databases only, nothing on an original. Every write is one
transaction, recorded as a WriteBack row (and a PlanEvent for plans); it never raises.
"""
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from django.conf import settings
from planning.models import WriteBack
from planning.services import event
from .db import live_database

MACHINE_STATES = ('Active', 'Breakdown')


def _secret(name):
    from dotenv import dotenv_values
    return os.environ.get(name) or dotenv_values(settings.ASK_AI_ENV_FILE).get(name) or ''


def enabled():
    return bool(settings.CRAFT_WRITEBACK and _secret('CRAFT_WRITER_USER') and _secret('CRAFT_WRITER_PASSWORD'))


def _connect(database):
    """A connection as the writer account (host and port from the repository .env, account from backend/.env)."""
    from dotenv import dotenv_values
    from integrations.craft.legacy_repository import LegacyExecutionRepository
    config = dict(dotenv_values(settings.ERP_ENV_FILE))
    config.update(LEGACY_DB_NAME=database, LEGACY_DB_USER=_secret('CRAFT_WRITER_USER'),
                  LEGACY_DB_PASSWORD=_secret('CRAFT_WRITER_PASSWORD'))
    return LegacyExecutionRepository.from_config(config).connection_factory()


def verify_writer_grants(grants):
    """USAGE on *.* is fine; every other grant must be on a *_demo database. Anything else: refuse."""
    suffix = re.escape(settings.CRAFT_DEMO_SUFFIX)
    statements = [str(v) for row in grants for v in row.values()]
    if not statements:
        raise PermissionError('No grants returned for the writer account')
    for s in statements:
        if re.fullmatch(r'GRANT USAGE ON \*\.\* TO .+', s, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r'GRANT .+ ON `?[A-Za-z0-9_]+' + suffix + r'`?\.\* TO .+', s, flags=re.IGNORECASE) \
                and 'WITH GRANT OPTION' not in s.upper():
            continue
        raise PermissionError('The writer account has rights on an original database; refusing to write')


def _target(dataset):
    database = live_database(dataset)
    if not settings.CRAFT_USE_DEMO_COPIES or not database.endswith(settings.CRAFT_DEMO_SUFFIX):
        raise PermissionError('Demo copies are not switched on (CRAFT_USE_DEMO_COPIES=1)')
    return database


def _write(dataset, kind, run, statements):
    """Run (sql, params) statements in one transaction on the demo copy; record the outcome."""
    database, rows, connection = '', 0, None
    try:
        database = _target(dataset)
        connection = _connect(database)
        with connection.cursor() as cursor:
            cursor.execute('SHOW GRANTS')
            verify_writer_grants(cursor.fetchall())
            cursor.execute('START TRANSACTION')
            try:
                for sql, params in statements:
                    cursor.execute(sql, params)
                    rows += max(0, cursor.rowcount or 0)
                cursor.execute('COMMIT')
            except Exception:
                cursor.execute('ROLLBACK')
                raise
        result = WriteBack.objects.create(run=run, database=database, kind=kind, rows=rows, ok=True)
    except Exception as exc:  # recorded, never raised: an approval must not fail because MySQL did
        result = WriteBack.objects.create(run=run, database=database, kind=kind, rows=0, ok=False,
                                          error=f'{type(exc).__name__}: {exc}'[:1000])
    finally:
        if connection is not None:
            connection.close()
    return result


def _stamp(reference, seconds):
    return (reference + timedelta(seconds=int(seconds))).strftime('%Y-%m-%d %H:%M:%S')


def write_plan(run):
    """Write an approved plan's schedule into the demo copy. None when write-back is off."""
    if not settings.CRAFT_WRITEBACK:
        return None
    try:
        digest = json.loads((Path(run.out_dir) / 'digest.json').read_text(encoding='utf-8'))
        reference = datetime.fromisoformat(digest['reference_time'])
        statements = _plan_statements(reference, digest['gantt']['CRAFT_V3'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        result = WriteBack.objects.create(run=run, database='', kind='PLAN', ok=False, error=f'No plan to write: {exc}')
        event(run, None, 'WRITEBACK_FAILED', 'Not written to the demo data: the plan has no readable saved schedule')
        return result
    result = _write(run.dataset, 'PLAN', run, statements)
    if result.ok:
        event(run, None, 'WRITEBACK_DONE', f'Plan written to the demo factory data ({result.rows} rows)')
    else:
        event(run, None, 'WRITEBACK_FAILED', f'Not written to the demo data: {result.error[:200]}')
    return result


def _plan_statements(reference, steps):
    """UPDATE statements for each planned step (its lot rows) and each order's start and end dates."""
    statements, spans = [], {}
    for s in steps:
        order, operation = (int(x) for x in s['key'].split('_', 1))
        start, end = _stamp(reference, s['start']), _stamp(reference, s['end'])
        statements.append((
            'UPDATE job_shop_schedules SET line_id = %s, asset_id = %s, employee_id = %s, planned_start_time = %s, '
            "planned_end_time = %s, planned_work_segments = %s WHERE execution_status IN ('Scheduled', 'Delayed') "
            'AND production_order_id = %s AND operation_id = %s',
            (int(s['line']), int(s['machines'][0]) if s.get('machines') else None,
             int(s['workers'][0]) if s.get('workers') else None, start, end, json.dumps([[start, end]]),
             order, operation)))
        first, last = spans.get(order, (start, end))
        spans[order] = (min(first, start), max(last, end))
    for order, (start, end) in sorted(spans.items()):
        statements.append(('UPDATE production_orders SET scheduled_start_date = %s, scheduled_end_date = %s '
                           'WHERE production_order_id = %s', (start[:10], end[:10], order)))
    return statements


def set_machine_status(dataset, asset_id, status):
    """Machine down or repaired, in the demo copy. True when written; False when off or failed."""
    if status not in MACHINE_STATES:
        raise ValueError(f'Machine status must be one of {MACHINE_STATES}')
    if not settings.CRAFT_WRITEBACK:
        return False
    result = _write(dataset, 'MACHINE', None, [('UPDATE machinery_assets SET current_status = %s WHERE asset_id = %s',
                                                (status, int(asset_id)))])
    return result.ok
