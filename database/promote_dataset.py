"""Offline MySQL 8/InnoDB promotion using one crash-atomic table-set rename.

Never call this with active application workers. See PROMOTION.md for the
maintenance contract, durable evidence, and recovery after an interrupted run.
"""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import mysql.connector
from data_generation.validation_records import validate_and_record, DEFAULT_OUTPUT, ROOT
from utils.derived_constants import calibration_for_clone

MARKER = '_dataset_activation'


class PromotionError(RuntimeError):
    pass


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,64}', value):
        raise PromotionError(f'Unsupported identifier: {value!r}')
    return '`' + value + '`'


def table(db, name):
    return ident(db) + '.' + ident(name)


def rows(conn, sql, args=()):
    cur = conn.cursor()
    try:
        cur.execute(sql, args)
        return cur.fetchall() if cur.with_rows else []
    finally:
        cur.close()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def checkpoint(phase):
    """Failure-injection seam; production deliberately does nothing."""


def inventory(conn, db):
    entries = rows(conn, 'SELECT TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s', (db,))
    if not entries or any(engine != 'InnoDB' or kind != 'BASE TABLE' for _, engine, kind in entries):
        raise PromotionError(f'{db}: only nonempty, all-InnoDB base-table schemas are supported')
    for name, _, _ in entries:
        ident(name)
    for catalog, column in [('TRIGGERS', 'TRIGGER_SCHEMA'), ('ROUTINES', 'ROUTINE_SCHEMA'), ('EVENTS', 'EVENT_SCHEMA')]:
        if rows(conn, f'SELECT COUNT(*) FROM information_schema.{catalog} WHERE {column}=%s', (db,))[0][0]:
            raise PromotionError(f'{db}: {catalog} require a separately reviewed migration')
    external = rows(conn, "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA IS NOT NULL AND ((TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA<>%s) OR (REFERENCED_TABLE_SCHEMA=%s AND TABLE_SCHEMA<>%s))", (db, db, db, db))[0][0]
    if external:
        raise PromotionError(f'{db}: cross-schema foreign keys prevent isolated promotion')
    unsupported = rows(conn, "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND (EXTRA LIKE '%VIRTUAL GENERATED%' OR EXTRA LIKE '%STORED GENERATED%' OR EXTRA LIKE '%INVISIBLE%')", (db,))[0][0]
    if unsupported:
        raise PromotionError(f'{db}: generated/invisible columns need explicit copy handling')
    return sorted(name for name, _, _ in entries)


def fingerprint(conn, db, names):
    """Order-independent, multiplicity-preserving content hash for every table."""
    result = {}
    for name in names:
        digests = sorted(hashlib.sha256(repr(row).encode('utf-8')).digest()
                         for row in rows(conn, f'SELECT * FROM {table(db, name)}'))
        result[name] = {'rows': len(digests), 'sha256': hashlib.sha256(b''.join(digests)).hexdigest()}
    return result


def create_schema_copy(conn, source, destination, names):
    charset, collation = rows(conn, 'SELECT DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s', (source,))[0]
    rows(conn, f'CREATE DATABASE {ident(destination)} CHARACTER SET {ident(charset)} COLLATE {ident(collation)}')
    rows(conn, f'USE {ident(destination)}')
    rows(conn, 'SET FOREIGN_KEY_CHECKS=0')
    try:
        for name in names:
            ddl = rows(conn, f'SHOW CREATE TABLE {table(source, name)}')[0][1]
            ddl = ddl.replace(ident(source) + '.', ident(destination) + '.')
            rows(conn, ddl)
    finally:
        rows(conn, 'SET FOREIGN_KEY_CHECKS=1')


def lock_sets(conn, sets):
    rows(conn, 'LOCK TABLES ' + ', '.join(f'{table(db, name)} {mode}' for db, names, mode in sets for name in names))


def copy_rows(conn, source, destination, names):
    rows(conn, 'SET FOREIGN_KEY_CHECKS=0')
    try:
        for name in names:
            rows(conn, f'INSERT INTO {table(destination, name)} SELECT * FROM {table(source, name)}')
    finally:
        rows(conn, 'SET FOREIGN_KEY_CHECKS=1')
    original = fingerprint(conn, source, names)
    if fingerprint(conn, destination, names) != original:
        raise PromotionError('Copy verification failed')
    return original


def reidentify(conn, database, old_identity, new_identity, names):
    if 'dataset_calibration' not in {n.lower() for n in names}:
        return None  # A legacy rollback copy may have no calibration.
    payload = json.loads(rows(conn, f'SELECT payload FROM {table(database, "dataset_calibration")} WHERE singleton_id=1')[0][0])
    value = calibration_for_clone(payload, old_identity, new_identity)
    rows(conn, f'UPDATE {table(database, "dataset_calibration")} SET payload=%s WHERE singleton_id=1', (json.dumps(value),))
    return value


def require_latest(conn, database, output_root):
    try:
        pointer = json.loads((Path(output_root)/database/'latest.json').read_text())
        filename = pointer['record']
        if Path(filename).name != filename:
            raise ValueError('Unsafe record path')
        raw = (Path(output_root)/database/filename).read_bytes()
        record = json.loads(raw)
        version = json.loads(rows(conn, f'SELECT payload FROM {table(database, "dataset_calibration")} WHERE singleton_id=1')[0][0])['dataset_version']
        valid = (pointer['valid'] is True and record['result']['valid'] is True
                 and record['result']['errors'] == {}
                 and pointer['database'] == record['database'] == database
                 and pointer['dataset_version'] == record['dataset_version'] == version
                 and pointer['validation_id'] == record['validation_id']
                 and hashlib.sha256(raw).hexdigest() == pointer['record_sha256']
                 and record['validator_sha256'] == hashlib.sha256((ROOT/'data_generation/validate_seed.py').read_bytes()).hexdigest())
        if not valid:
            raise ValueError('Latest validation is failed, stale, mismatched, or corrupt')
        return record
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise PromotionError(f'{database}: valid latest validation is required: {exc}') from exc


def gate(config, db, output_root, expected_counts):
    connection = mysql.connector.connect(**dict(config, database=db, autocommit=False))
    try:
        connection.start_transaction(readonly=True)
        return validate_and_record(connection, output_root=output_root, expected_counts=expected_counts)
    finally:
        connection.rollback()
        connection.close()


def activation_id(conn, db):
    names = rows(conn, 'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s', (db,))
    if MARKER not in {n[0].lower() for n in names}:
        return None
    values = rows(conn, f'SELECT promotion_id FROM {table(db, MARKER)}')
    if len(values) != 1 or not values[0][0]:
        raise PromotionError(f'{db}: invalid activation marker')
    return values[0][0]


def require_original(conn, db, journal):
    """Prove the complete old dataset, not just a matching subset of rows."""
    required = {'old_tables', 'old_fingerprint', 'old_activation_id'}
    missing = required - journal.keys()
    if missing:
        raise PromotionError(f'Incomplete journal: missing original-state evidence {sorted(missing)}; cannot classify live safely')
    if inventory(conn, db) != journal['old_tables']:
        raise PromotionError('Original table inventory does not match')
    if activation_id(conn, db) != journal['old_activation_id']:
        raise PromotionError('Original activation identity does not match; a later activation may be present')
    if fingerprint(conn, db, journal['old_tables']) != journal['old_fingerprint']:
        raise PromotionError('Original dataset contents do not match')


def restore(conn, journal):
    live, retired, rejected = (journal[k] for k in ('live', 'retired', 'rejected'))
    if activation_id(conn, live) != journal['promotion_id']:
        raise PromotionError('Refusing rollback: live activation marker does not match this promotion')
    if inventory(conn, live) != journal['new_tables']:
        raise PromotionError('Refusing rollback: active table inventory changed')
    require_original(conn, retired, journal)
    pairs = [(live, n, rejected, n) for n in journal['new_tables']]
    pairs += [(retired, n, live, n) for n in journal['old_tables']]
    rows(conn, 'RENAME TABLE ' + ', '.join(f'{table(a,b)} TO {table(c,d)}' for a,b,c,d in pairs))
    require_original(conn, live, journal)


def promote(config, staging, live, *, maintenance=False, output_root=DEFAULT_OUTPUT,
            journal_root=ROOT/'outputs/promotions', expected_counts=None):
    """Promote offline. expected_counts is restricted to disposable test schemas."""
    if not maintenance:
        raise PromotionError('Stop application/ingestion/training processes and acknowledge maintenance mode')
    for db in (staging, live): ident(db)
    if staging.lower() == live.lower(): raise PromotionError('Staging and live must differ')
    if expected_counts is not None and not all(db.startswith('promotion_test_') for db in (staging, live)):
        raise PromotionError('Population overrides are restricted to disposable test schemas')
    connection = mysql.connector.connect(**dict(config, autocommit=True))
    token = uuid4().hex[:20]
    journal = {'version': 1, 'promotion_id': token, 'staging': staging, 'live': live,
               'prepared': 'promotion_prepared_'+token, 'backup': 'promotion_backup_'+token,
               'retired': 'promotion_retired_'+token, 'rejected': 'promotion_rejected_'+token,
               'state': 'preflight', 'created_at': datetime.now(timezone.utc).isoformat(),
               'validation_root': str(Path(output_root).resolve()), 'expected_counts': expected_counts}
    path = Path(journal_root)/(token+'.json')
    lock_name = 'dataset_promotion_' + hashlib.sha256(live.lower().encode()).hexdigest()[:40]
    locked = False
    try:
        version = rows(connection, 'SELECT VERSION()')[0][0]
        match = re.match(r'8\.(0|4)\.(\d+)', version)
        if not match or 'mariadb' in version.lower() or (match[1] == '0' and int(match[2]) < 13):
            raise PromotionError(f'Unsupported atomic-DDL server: {version}')
        if rows(connection, 'SELECT GET_LOCK(%s,0)', (lock_name,))[0][0] != 1:
            raise PromotionError('Another promotion/recovery owns this live database')
        locked = True
        journal['server_uuid'] = rows(connection, 'SELECT @@server_uuid')[0][0]
        old = inventory(connection, live)
        lock_sets(connection, [(live, old, 'READ')])
        try:
            journal['old_tables'] = old
            journal['old_activation_id'] = activation_id(connection, live)
            journal['old_fingerprint'] = fingerprint(connection, live, old)
        finally:
            rows(connection, 'UNLOCK TABLES')
        atomic_json(path, journal)
        checkpoint('original_recorded')
        source = inventory(connection, staging)
        record = require_latest(connection, staging, output_root)
        journal['source_validation_id'] = record['validation_id']
        journal['old_tables'] = old
        journal['new_tables'] = sorted(set(source) | {MARKER})
        atomic_json(path, journal)
        create_schema_copy(connection, staging, journal['prepared'], source)
        lock_sets(connection, [(staging, source, 'READ'), (journal['prepared'], source, 'WRITE')])
        try:
            copy_rows(connection, staging, journal['prepared'], source)
            prepared_calibration = reidentify(connection, journal['prepared'], staging, journal['prepared'], source)
        finally:
            rows(connection, 'UNLOCK TABLES')
        if prepared_calibration is None: raise PromotionError('Staging calibration is required')
        prepared = journal['prepared']
        rows(connection, f'CREATE TABLE IF NOT EXISTS {table(prepared, MARKER)} (promotion_id VARCHAR(32) PRIMARY KEY) ENGINE=InnoDB')
        rows(connection, f'DELETE FROM {table(prepared, MARKER)}')
        rows(connection, f'INSERT INTO {table(prepared, MARKER)} VALUES (%s)', (token,))
        validated_fingerprint = fingerprint(connection, prepared, journal['new_tables'])
        checked = gate(config, prepared, output_root, expected_counts)
        if not checked['result']['valid']: raise PromotionError(f'Prepared dataset failed gate: {checked["result"]["errors"]}')
        journal['prepared_validation_id'] = checked['validation_id']
        checkpoint('prepared_validated')
        create_schema_copy(connection, live, journal['backup'], old)
        for key in ('retired', 'rejected'):
            rows(connection, f'CREATE DATABASE {ident(journal[key])}')
        lock_sets(connection, [(live, old, 'WRITE'), (journal['backup'], old, 'WRITE'), (prepared, journal['new_tables'], 'WRITE'), (staging, source, 'READ')])
        try:
            if inventory(connection, live) != old or inventory(connection, prepared) != journal['new_tables']:
                raise PromotionError('Table inventory changed during preparation')
            if fingerprint(connection, prepared, journal['new_tables']) != validated_fingerprint:
                raise PromotionError('Prepared dataset changed after validation')
            # The latest staging verdict must still be a pass at cutover.
            require_latest(connection, staging, output_root)
            require_original(connection, live, journal)
            copy_rows(connection, live, journal['backup'], old)
            reidentify(connection, journal['backup'], live, journal['backup'], old)
            journal['backup_fingerprint'] = fingerprint(connection, journal['backup'], old)
            final_cal = reidentify(connection, prepared, prepared, live, journal['new_tables'])
            journal['new_dataset_version'] = final_cal['dataset_version']
            journal['state'] = 'cutover_intent'
            atomic_json(path, journal)
            checkpoint('before_cutover')
            pairs = [(live, n, journal['retired'], n) for n in old]
            pairs += [(prepared, n, live, n) for n in journal['new_tables']]
            rows(connection, 'RENAME TABLE ' + ', '.join(f'{table(a,b)} TO {table(c,d)}' for a,b,c,d in pairs))
            checkpoint('after_cutover')
        finally:
            rows(connection, 'UNLOCK TABLES')
        journal['state'] = 'activated_pending_validation'
        atomic_json(path, journal)
        verified = gate(config, live, output_root, expected_counts)
        journal['live_validation_id'] = verified['validation_id']
        if not verified['result']['valid']:
            raise PromotionError(f'Live validation failed: {verified["result"]["errors"]}')
        journal['state'] = 'validated'
        atomic_json(path, journal)
        return dict(journal, journal_path=str(path))
    except Exception as exc:
        # A lost RENAME acknowledgement is ambiguous. The activation marker,
        # never a Python success flag, determines reality. If the connection
        # is lost, retain recovery_required for the explicit recovery command.
        if journal['state'] in ('cutover_intent', 'activated_pending_validation', 'validated'):
            try:
                rows(connection, 'UNLOCK TABLES')
                if activation_id(connection, live) == token:
                    restore(connection, journal)
                    journal['state'] = 'rolled_back'
                else:
                    journal['state'] = 'failed_before_activation'
            except Exception as recovery_error:
                journal['state'] = 'recovery_required'
                journal['recovery_error'] = str(recovery_error)
        else:
            journal['state'] = 'failed_before_activation'
        journal['error'] = f'{type(exc).__name__}: {exc}'
        atomic_json(path, journal)
        exc.add_note(f'Promotion journal: {path}; state={journal["state"]}')
        raise
    finally:
        if locked and connection.is_connected():
            rows(connection, 'SELECT RELEASE_LOCK(%s)', (lock_name,))
        connection.close()


def recover(config, journal_path, *, maintenance=False, rollback=False):
    if not maintenance: raise PromotionError('Recovery requires maintenance mode')
    path = Path(journal_path)
    journal = json.loads(path.read_text())
    required = {'server_uuid', 'live', 'promotion_id', 'state', 'old_tables',
                'old_fingerprint', 'old_activation_id'}
    missing = required - journal.keys()
    if missing:
        raise PromotionError(f'Incomplete journal: missing original-state evidence {sorted(missing)}; cannot classify live safely')
    connection = mysql.connector.connect(**dict(config, autocommit=True))
    live = journal['live']
    lock_name = 'dataset_promotion_' + hashlib.sha256(live.lower().encode()).hexdigest()[:40]
    try:
        if rows(connection, 'SELECT @@server_uuid')[0][0] != journal['server_uuid']:
            raise PromotionError('Journal belongs to another MySQL server')
        if rows(connection, 'SELECT GET_LOCK(%s,0)', (lock_name,))[0][0] != 1:
            raise PromotionError('Another promotion/recovery is active')
        if activation_id(connection, live) == journal['promotion_id']:
            required = {'new_tables', 'retired', 'rejected', 'validation_root', 'expected_counts'}
            missing = required - journal.keys()
            if missing:
                raise PromotionError(f'Incomplete activation journal: {sorted(missing)}')
            if inventory(connection, live) != journal['new_tables']:
                raise PromotionError('Active table inventory does not match this promotion')
            if rollback:
                restore(connection, journal)
                journal['state'] = 'rolled_back'
            else:
                result = gate(config, live, journal['validation_root'], journal['expected_counts'])
                if not result['result']['valid']:
                    restore(connection, journal)
                    journal['state'] = 'rolled_back'
                else:
                    journal['state'] = 'validated'
                journal['recovery_validation_id'] = result['validation_id']
        else:
            require_original(connection, live, journal)
            # Includes early failures, repeated recovery, and repeated rollback.
            journal['state'] = 'old_dataset_active'
        atomic_json(path, journal)
        return journal
    finally:
        rows(connection, 'SELECT RELEASE_LOCK(%s)', (lock_name,))
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('promote')
    start.add_argument('--staging', required=True)
    start.add_argument('--live', required=True)
    start.add_argument('--maintenance', action='store_true', required=True)
    repair = commands.add_parser('recover')
    repair.add_argument('journal')
    repair.add_argument('--maintenance', action='store_true', required=True)
    repair.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    from data_generation.seed_database_fixed import db_config
    if args.command == 'promote':
        result = promote(db_config, args.staging, args.live, maintenance=args.maintenance)
    else:
        result = recover(db_config, args.journal, maintenance=args.maintenance, rollback=args.rollback)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
