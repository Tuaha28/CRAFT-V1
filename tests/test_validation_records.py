"""Recorder preserves previous verdicts and records the gate's real result."""
import hashlib
import json
import pytest
from data_generation.validation_records import validate_and_record
from tests.test_validate_seed_table_case import Connection


def test_failed_history_survives_later_pass(tmp_path):
    failed = validate_and_record(Connection([]), output_root=tmp_path)
    passed = validate_and_record(Connection(['dataset_calibration', 'order_factory_ownership']), output_root=tmp_path)
    folder = tmp_path/'some_db'
    assert json.loads((folder/(failed['validation_id']+'.json')).read_text())['result']['valid'] is False
    latest = json.loads((folder/'latest.json').read_text())
    assert latest['validation_id'] == passed['validation_id']
    record_path = folder/latest['record']
    assert latest['record_sha256'] == hashlib.sha256(record_path.read_bytes()).hexdigest()
    assert passed['dataset_version'] == 'test-version'
    assert passed['validator_sha256']
    assert passed['started_at'] <= passed['finished_at']
    assert passed['result']['valid'] is True


def test_exception_invalidates_previous_latest_pass(tmp_path, monkeypatch):
    from data_generation import validation_records as module
    connection = Connection(['dataset_calibration', 'order_factory_ownership'])
    validate_and_record(connection, output_root=tmp_path)
    def broken(*args, **kwargs): raise RuntimeError('injected gate failure')
    monkeypatch.setattr(module, 'validate_seed', broken)
    with pytest.raises(RuntimeError, match='injected gate failure'):
        validate_and_record(connection, output_root=tmp_path)
    latest = json.loads((tmp_path/'some_db/latest.json').read_text())
    assert latest['valid'] is False
    record = json.loads((tmp_path/'some_db'/latest['record']).read_text())
    assert record['result']['errors'] == {'validation_exception': 1}
