"""Read-only per-factory snapshots; never imports the admin or Django engine."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'web_v1/backend'))
from dotenv import dotenv_values
from integrations.craft.contracts import json_value
from integrations.craft.legacy_repository import LegacyExecutionRepository, verify_select_only_grants
from integrations.craft.source_readiness import audit_source


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    config = {**dotenv_values(ROOT / '.env'), **os.environ}
    if config.get('LEGACY_DB_USER') != 'craft_reader':
        raise RuntimeError('Dedicated craft_reader configuration required')
    repo = LegacyExecutionRepository.from_config(config)
    output = Path(__file__).parent / ('capture_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(exist_ok=False)
    manifest = {}

    def save(name, payload):
        path = output / name
        with path.open('x', encoding='utf-8') as stream:
            json.dump(json_value(payload), stream, sort_keys=True, indent=2)
            stream.write('\n')
        manifest[name] = sha(path)

    prior = json.loads((Path(__file__).parent / 'offline_readiness_report.json').read_text())
    protected_before = {path: sha(ROOT / path) for path in prior['protected_file_hashes_before']}
    started = time.perf_counter()
    connection = repo.connection_factory()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW GRANTS')
            grants = list(cursor.fetchall())
            verify_select_only_grants(grants)
            cursor.execute('SELECT CURRENT_USER() AS account, CURRENT_ROLE() AS active_roles')
            identity = cursor.fetchone()
            if identity != {'account': 'craft_reader@localhost', 'active_roles': 'NONE'}:
                raise RuntimeError('Reader identity or active role mismatch')
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
            cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            cursor.execute('SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, ORDINAL_POSITION', ('garment_fyp_db',))
            schema = list(cursor.fetchall())
            cursor.execute('SELECT factory_id FROM factories ORDER BY factory_id')
            factories = [row['factory_id'] for row in cursor.fetchall()]
            if len(factories) != 6:
                raise RuntimeError('Expected exactly six factories')
            cursor.execute('SELECT o.factory_id, COUNT(*) AS operations FROM production_orders p JOIN order_factory_ownership o ON o.production_order_id=p.production_order_id JOIN routing_operations r ON r.product_id=p.product_id GROUP BY o.factory_id ORDER BY o.factory_id')
            expected_counts = {row['factory_id']: row['operations'] for row in cursor.fetchall()}
            save('reader_and_schema.json', {'identity': identity, 'grants': grants,
                 'columns': schema, 'operation_counts': expected_counts,
                 'snapshot_scope': 'Metadata transaction; each factory has its own consistent read-only transaction'})
    finally:
        connection.rollback()
        connection.close()

    summaries = []
    dataset = None
    for factory_id in factories:
        tick = time.perf_counter()
        source = repo.read_factory(factory_id)
        extraction_seconds = time.perf_counter() - tick
        if dataset is None:
            dataset = source['dataset']
        if source['dataset'] != dataset:
            raise RuntimeError('Dataset revision changed between factory snapshots')
        if len(source['operations']) != expected_counts[factory_id]:
            raise RuntimeError('Operation counts changed since metadata snapshot')
        name = f'factory{factory_id}_source.json'
        save(name, source)
        tick = time.perf_counter()
        audit = audit_source(source, raw_sha256=manifest[name])
        validation_seconds = time.perf_counter() - tick
        save(f'factory{factory_id}_readiness.json', audit)
        summary = {'factory_id': factory_id, 'operations': audit['operation_count'],
                   'orders': len(source['orders']), 'counts': audit['counts'],
                   'resource_counts': {key: len(value) for key, value in source['resources'].items()},
                   'boundary_error_codes': audit['boundary_error_codes'],
                   'extraction_seconds': extraction_seconds,
                   'validation_seconds': validation_seconds,
                   'source_sha256': manifest[name]}
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
    protected_after = {path: sha(ROOT / path) for path in protected_before}
    if protected_before != protected_after:
        raise RuntimeError('Protected historical evidence changed')
    save('report.json', {'status': 'EXTRACTED_AND_ADMISSION_AUDITED',
         'factory_summaries': summaries, 'dataset': dataset,
         'factory_data_writes': 0, 'django_database_access': False,
         'snapshot_consistency': 'PER_FACTORY_REPEATABLE_READ_INNODB; NOT_ONE_GLOBAL_TRANSACTION',
         'cross_factory_checks': 'Matching dataset revision and operation counts; not global atomicity proof',
         'protected_before': protected_before, 'protected_after': protected_after,
         'total_seconds': time.perf_counter() - started,
         'crew_columns': [row for row in schema if any(token in row['COLUMN_NAME'].lower() for token in ('crew', 'required_worker', 'workers_required'))],
         'production_activation': 'DISABLED'})
    save('manifest.json', dict(manifest))
    print(json.dumps({'output_directory': str(output.relative_to(ROOT)),
                      'total_operations': sum(row['operations'] for row in summaries)}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never serialize driver messages that could contain connection details.
        print(json.dumps({'status': 'BLOCKED', 'error_type': type(exc).__name__,
                          'numeric_error_code': exc.args[0] if exc.args and isinstance(exc.args[0], int) else None}), flush=True)
        sys.exit(1)
