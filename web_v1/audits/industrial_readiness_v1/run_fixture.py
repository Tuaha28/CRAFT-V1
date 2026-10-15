"""Controlled CRAFT subprocess proof. No legacy reads and no live scheduling."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import pymysql
def reject_database_connection(*args, **kwargs):
    raise RuntimeError('Isolated residual proof forbids legacy database connections')
pymysql.connect = reject_database_connection
from web_v1.backend.integrations.craft.tests.test_residual_execution import solve_fixture

if __name__ == '__main__':
    start = time.perf_counter()
    result = solve_fixture()
    result['source'] = 'SYNTHETIC_FIXTURE'
    result['runtime_seconds'] = time.perf_counter() - start
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name('fixture_result.json')
    target.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding='utf-8')
    print('Controlled residual vertical slice:', result['acceptance']['status'])
