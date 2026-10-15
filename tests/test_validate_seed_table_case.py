"""Assert the real gate verdict using SQL-response fixtures, not copied logic."""
import json
import pytest
from data_generation.validate_seed import validate_seed


def calibration(database='some_db'):
    return {'artifact_version': 1, 'dataset_version': 'test-version',
            'source_database': database, 'duration_calibration_factor': 0.825454,
            'slack_days_quantiles': {'p25': -12, 'p50': 27, 'p75': 88},
            'order_count': 1, 'reference_time': '2026-09-10T00:00:00',
            'source_hashes': {n: 'a'*64 for n in
                ('grid.xls', 'Plan.xlsx', 'PPC-1.xlsx', 'WorkStation.xlsx')}}


class Cursor:
    def __init__(self, tables):
        self.tables = tables
        self.rows = []

    def execute(self, sql, *args):
        s = ' '.join(sql.lower().split())
        if s == 'select database() database_name':
            self.rows = [{'database_name': 'some_db'}]
        elif 'information_schema.tables' in s:
            self.rows = [{'name': n} for n in self.tables]
        elif s == 'select singleton_id,payload from dataset_calibration':
            self.rows = [{'singleton_id': 1, 'payload': json.dumps(calibration())}]
        elif s == 'select payload from dataset_calibration where singleton_id=1':
            self.rows = [{'payload': json.dumps(calibration())}]
        elif s.startswith('select p.production_order_id,r.required_machine_type'):
            self.rows = []
        elif s.startswith('select m.asset_id,m.factory_id'):
            self.rows = [dict(asset_id=1, factory_id=1, line_id=1,
                              current_status='Idle', operation_type='body assembly', is_active=1)]
        elif 'information_schema.key_column_usage' in s or s.startswith('select j.planned_start_time'):
            self.rows = []
        elif s.startswith('select count(*) n from '):
            populations = {'production_orders': 1, 'machinery_assets': 3000,
                           'employees': 9000, 'factories': 6}
            self.rows = [{'n': populations.get(s.removeprefix('select count(*) n from '), 0)}]
        else:
            raise AssertionError(f'Unexpected gate query: {sql}')

    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def __iter__(self): return iter(self.rows)
    def close(self): pass


class Connection:
    def __init__(self, tables): self.tables = tables
    def cursor(self, dictionary=False): return Cursor(self.tables)


@pytest.mark.parametrize('tables', [
    ['dataset_calibration', 'order_factory_ownership'],
    ['Dataset_Calibration', 'Order_Factory_Ownership'],
])
def test_present_artifacts_pass_real_validator(tables):
    result = validate_seed(Connection(tables))
    assert result['valid'] is True, result
    assert result['checks']['missing_calibration_table'] == 0
    assert result['checks']['missing_ownership_table'] == 0


@pytest.mark.parametrize('tables,missing', [
    ([], {'missing_calibration_table', 'missing_ownership_table'}),
    (['dataset_calibration'], {'missing_ownership_table'}),
])
def test_absent_artifacts_fail_real_validator(tables, missing):
    result = validate_seed(Connection(tables))
    assert result['valid'] is False
    assert set(result['errors']) == missing
