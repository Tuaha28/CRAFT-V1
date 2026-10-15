"""Shared recorded gate for staging and standalone use; never mutates the DB.

Records are append-only. latest.json is an atomic pointer, not a rewritten
historical verdict. The pure validate_seed function remains available to tests.
Run: python -m data_generation.validation_records DATABASE
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

from data_generation.validate_seed import validate_seed

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'outputs' / 'dataset_validations'


def _write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def validate_and_record(connection, *, output_root=DEFAULT_OUTPUT, expected_counts=None):
    """Record identity and complete verdict, including unexpected gate failures.

    The caller must provide a stable database/read-only transaction. Records
    with exceptions replace the latest pointer too, so an old pass cannot mask
    a failed attempt. Historical seed manifests are deliberately untouched.
    """
    started = datetime.now(timezone.utc)
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute('SELECT DATABASE() database_name')
        database = cursor.fetchone()['database_name']
    finally:
        cursor.close()
    if not database or not all(c.isalnum() or c == '_' for c in database):
        raise ValueError('Recording requires a named database with a safe identifier')
    folder = Path(output_root) / database
    folder.mkdir(parents=True, exist_ok=True)
    record_id = started.strftime('%Y%m%dT%H%M%S.%fZ') + '_' + uuid4().hex
    record = {'record_version': 1, 'validation_id': record_id,
              'database': database, 'dataset_version': None,
              'started_at': started.isoformat(),
              'validator_sha256': hashlib.sha256((ROOT/'data_generation/validate_seed.py').read_bytes()).hexdigest(),
              'recorder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    caught = None
    try:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("SELECT TABLE_NAME name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            names = {r['name'].lower() for r in cursor.fetchall()}
            if 'dataset_calibration' in names:
                cursor.execute('SELECT payload FROM Dataset_Calibration WHERE singleton_id=1')
                row = cursor.fetchone()
                if row:
                    record['dataset_version'] = json.loads(row['payload']).get('dataset_version')
        finally:
            cursor.close()
        record['result'] = validate_seed(connection, expected_counts=expected_counts)
    except Exception as exc:
        caught = exc
        record['result'] = {'valid': False, 'checks': {},
                            'errors': {'validation_exception': 1},
                            'details': {'exception': f'{type(exc).__name__}: {exc}'}}
    record['finished_at'] = datetime.now(timezone.utc).isoformat()
    destination = folder / (record_id + '.json')
    _write_new(destination, record)
    pointer = {'validation_id': record_id, 'database': database,
               'dataset_version': record['dataset_version'],
               'record': destination.name,
               'record_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
               'valid': record['result']['valid'], 'finished_at': record['finished_at']}
    pending = folder / (record_id + '.latest.tmp')
    _write_new(pending, pointer)
    os.replace(pending, folder / 'latest.json')
    if caught is not None:
        caught.add_note(f'Failed validation recorded at {destination}')
        raise caught
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    args = parser.parse_args()
    import mysql.connector
    from data_generation.seed_database_fixed import db_config
    config = dict(db_config, database=args.database)
    connection = mysql.connector.connect(**config)
    try:
        connection.start_transaction(readonly=True)
        record = validate_and_record(connection)
    finally:
        connection.rollback()
        connection.close()
    print(json.dumps(record, indent=2))
    raise SystemExit(0 if record['result']['valid'] else 1)


if __name__ == '__main__':
    main()
