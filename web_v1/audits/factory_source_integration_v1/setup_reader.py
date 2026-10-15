"""Explicitly authorized account provisioning. No factory-data queries as admin.

Run only with --provision-reader. Secrets stay in memory and the ignored .env.
Existing accounts are never modified. Error output deliberately omits SQL.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'web_v1/backend'))
from dotenv import dotenv_values, set_key
import pymysql
from integrations.craft.legacy_repository import verify_select_only_grants


def provision():
    env_path = ROOT / '.env'
    if subprocess.run(['git', 'check-ignore', '--quiet', '.env'], cwd=ROOT).returncode:
        raise RuntimeError('ENV_NOT_IGNORED')
    tracked = subprocess.run(['git', 'ls-files', '--', '.env'], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    if tracked:
        raise RuntimeError('ENV_TRACKED')
    config = {**dotenv_values(env_path), **os.environ}
    host = config.get('DB_HOST', 'localhost')
    port = int(config.get('DB_PORT', 3306))
    if host not in ('localhost', '127.0.0.1'):
        raise RuntimeError('LOCAL_ADMIN_HOST_REQUIRED')
    if not config.get('DB_USER') or not config.get('DB_PASS'):
        raise RuntimeError('ADMIN_CREDENTIALS_MISSING')
    evidence = {'started_utc': datetime.now(timezone.utc).isoformat(),
                'factory_data_queries_as_admin': 0, 'factory_data_writes': 0,
                'existing_accounts_modified': False, 'admin_statement_types': []}
    output = Path(__file__).parent / ('reader_setup_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    admin = None
    try:
        admin = pymysql.connect(host=host, port=port, user=config['DB_USER'],
                                password=config['DB_PASS'], autocommit=True,
                                connect_timeout=5, read_timeout=20,
                                cursorclass=pymysql.cursors.DictCursor)
        with admin.cursor() as cursor:
            cursor.execute('SELECT User, Host FROM mysql.user WHERE User=%s', ('craft_reader',))
            rows = list(cursor.fetchall())
            evidence['admin_statement_types'].append('SELECT_READER_ACCOUNT_METADATA')
            cursor.execute('SELECT @@GLOBAL.mandatory_roles AS mandatory_roles')
            mandatory = cursor.fetchone()['mandatory_roles']
            evidence['admin_statement_types'].append('SELECT_MANDATORY_ROLES')
            if mandatory and str(mandatory).upper() != 'NONE':
                raise RuntimeError('MANDATORY_ROLES_REQUIRE_REVIEW')
            if rows:
                if len(rows) != 1 or rows[0]['Host'] != 'localhost':
                    raise RuntimeError('EXISTING_READER_HOSTS_REQUIRE_REVIEW')
                cursor.execute('SHOW GRANTS FOR %s@%s', ('craft_reader', 'localhost'))
                verify_select_only_grants(cursor.fetchall())
                evidence['admin_statement_types'].append('SHOW_EXISTING_READER_GRANTS')
                if config.get('LEGACY_DB_USER') != 'craft_reader' or not config.get('LEGACY_DB_PASSWORD'):
                    raise RuntimeError('EXISTING_READER_PASSWORD_REQUIRED_NO_ACCOUNT_CHANGED')
                password = config['LEGACY_DB_PASSWORD']
                evidence['account_created'] = False
            else:
                password = secrets.token_urlsafe(36) + '!aA9'
                cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s',
                               ('craft_reader', 'localhost', password))
                evidence['account_created'] = True
                evidence['admin_statement_types'].append('CREATE_NEW_READER_ONLY')
                # Persist immediately so a later grant/connect failure cannot
                # strand the new account with an unrecoverable random secret.
                for key, value in {'LEGACY_DB_USER': 'craft_reader',
                                   'LEGACY_DB_PASSWORD': password,
                                   'LEGACY_DB_HOST': host, 'LEGACY_DB_PORT': str(port),
                                   'LEGACY_DB_NAME': 'garment_fyp_db'}.items():
                    set_key(str(env_path), key, value, quote_mode='always')
                cursor.execute('GRANT SELECT ON `garment_fyp_db`.* TO %s@%s',
                               ('craft_reader', 'localhost'))
                evidence['admin_statement_types'].append('GRANT_SELECT_NEW_READER_ONLY')
        admin.close()
        admin = None
        reader = pymysql.connect(host=host, port=port, user='craft_reader', password=password,
                                 connect_timeout=5, read_timeout=20,
                                 cursorclass=pymysql.cursors.DictCursor)
        try:
            with reader.cursor() as cursor:
                cursor.execute('SHOW GRANTS')
                grants = list(cursor.fetchall())
                verify_select_only_grants(grants)
                cursor.execute('SELECT CURRENT_USER() AS account, CURRENT_ROLE() AS active_roles')
                identity = cursor.fetchone()
                if identity != {'account': 'craft_reader@localhost', 'active_roles': 'NONE'}:
                    raise RuntimeError('READER_IDENTITY_OR_ROLES_UNEXPECTED')
                evidence.update(reader_verified=True, reader_identity=identity,
                                reader_grants=[value for row in grants for value in row.values()],
                                status='VERIFIED')
        finally:
            reader.close()
    except Exception as exc:
        evidence['status'] = 'BLOCKED'
        evidence['error_type'] = type(exc).__name__
        # Driver errors may contain query text. Report only numeric code.
        evidence['error_code'] = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        if isinstance(exc, RuntimeError):
            evidence['blocker'] = str(exc)
    finally:
        if admin is not None:
            admin.close()
        output.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': evidence['status'], 'evidence': str(output.relative_to(ROOT)),
                      'account_created': evidence.get('account_created', False),
                      'error_code': evidence.get('error_code'), 'blocker': evidence.get('blocker')}))
    return 0 if evidence['status'] == 'VERIFIED' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--provision-reader', action='store_true', required=True)
    parser.parse_args()
    sys.exit(provision())
