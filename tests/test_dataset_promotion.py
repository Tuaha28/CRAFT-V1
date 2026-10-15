"""Real MySQL promotion: real full gate, FK-preserving swap, rollback and crash recovery."""
import json
from pathlib import Path
from uuid import uuid4
import pytest
import mysql.connector
from sqlalchemy import create_engine
from database import promote_dataset as promotion
from database.ga_snapshot import _server_engine
from data_generation.seed_database_fixed import db_config
from utils.derived_constants import load_derived_constants
from utils.factory_ownership import load_factory_ownership
from tests.test_validate_seed_table_case import calibration

COUNTS = {'employee_count': 1, 'machine_count': 1, 'factory_count': 1}

SCHEMA = {
    'factories': 'factory_id INT PRIMARY KEY',
    'departments': 'department_id INT PRIMARY KEY, factory_id INT',
    'production_lines': 'line_id INT PRIMARY KEY, department_id INT, is_active INT',
    'employees': 'employee_id INT PRIMARY KEY, factory_id INT, role VARCHAR(40)',
    'employee_batches': 'batch_id INT PRIMARY KEY, line_id INT, supervisor_id INT, rotation_offset INT, rest_day_offset INT',
    'batch_members': 'batch_id INT, employee_id INT',
    'work_schedules': 'employee_id INT, is_on_leave INT',
    'machinery_assets': 'asset_id INT PRIMARY KEY, factory_id INT, line_id INT, current_status VARCHAR(40)',
    'machine_capabilities': 'asset_id INT, operation_type VARCHAR(40)',
    'operator_skill_matrix': 'employee_id INT, operation_type VARCHAR(40)',
    'production_orders': 'production_order_id INT PRIMARY KEY, product_id INT, target_quantity INT, status VARCHAR(40)',
    'routing_operations': 'operation_id INT PRIMARY KEY, product_id INT, required_machine_type VARCHAR(50), operation_sequence_number INT',
    'order_factory_ownership': 'production_order_id INT PRIMARY KEY, factory_id INT, FOREIGN KEY (production_order_id) REFERENCES production_orders(production_order_id), FOREIGN KEY (factory_id) REFERENCES factories(factory_id)',
    'dataset_calibration': 'singleton_id INT PRIMARY KEY, payload LONGTEXT',
    'job_shop_schedules': 'schedule_item_id INT PRIMARY KEY, production_order_id INT, operation_id INT, asset_id INT, employee_id INT, line_id INT, batch_id INT, lot_index INT, lot_quantity INT, execution_status VARCHAR(40), planned_start_time DATETIME, planned_end_time DATETIME, actual_start_time DATETIME, actual_end_time DATETIME, planned_work_segments LONGTEXT, actual_work_segments LONGTEXT, FOREIGN KEY (production_order_id) REFERENCES production_orders(production_order_id), FOREIGN KEY (asset_id) REFERENCES machinery_assets(asset_id)'
}


def seed_tiny(conn, db, order):
    """Small coherent data, not a mocked gate or a full production reseed."""
    promotion.rows(conn, f'CREATE DATABASE {promotion.ident(db)}')
    promotion.rows(conn, f'USE {promotion.ident(db)}')
    for name, columns in SCHEMA.items():
        promotion.rows(conn, f'CREATE TABLE {promotion.ident(name)} ({columns}) ENGINE=InnoDB')
    inserts = {
        'factories': '(1)', 'departments': '(1,1)', 'production_lines': '(1,1,1)',
        'employees': "(1,1,'Operator')", 'work_schedules': '(1,0)',
        'machinery_assets': "(1,1,1,'Idle')", 'machine_capabilities': "(1,'body assembly')",
        'operator_skill_matrix': "(1,'body assembly')",
        'production_orders': f"({order},1,1,'Pending')",
        'routing_operations': "(1,1,'single-needle lockstitch',1)",
        'order_factory_ownership': f'({order},1)'
    }
    for name, values in inserts.items():
        promotion.rows(conn, f'INSERT INTO {promotion.ident(name)} VALUES {values}')
    payload = calibration(db)
    payload['dataset_version'] = db + '-version'
    promotion.rows(conn, 'INSERT INTO dataset_calibration VALUES (1,%s)', (json.dumps(payload),))
    segments = json.dumps([['2026-09-10T06:00:00','2026-09-10T07:00:00']])
    promotion.rows(conn, "INSERT INTO job_shop_schedules VALUES (1,%s,1,1,1,1,NULL,0,1,'Scheduled','2026-09-10 06:00:00','2026-09-10 07:00:00','2026-09-10 06:00:00','2026-09-10 07:00:00',%s,%s)", (order,segments,segments))


@pytest.fixture
def databases(tmp_path):
    token = uuid4().hex[:12]
    live, staging = 'promotion_test_live_'+token, 'promotion_test_stage_'+token
    config = dict(db_config)
    conn = mysql.connector.connect(**dict(config, autocommit=True))
    seed_tiny(conn, live, 1)
    seed_tiny(conn, staging, 2)
    output = tmp_path/'validations'
    journals = tmp_path/'promotions'
    assert promotion.gate(config, staging, output, COUNTS)['result']['valid']
    try:
        yield config, conn, live, staging, output, journals
    finally:
        names = {live, staging}
        if journals.exists():
            for path in journals.glob('*.json'):
                j = json.loads(path.read_text())
                # .get, not [k]: a journal missing one of these keys would
                # otherwise raise KeyError here and abort cleanup entirely,
                # leaking every disposable schema for that test.
                names.update(v for v in (j.get(k) for k in ('prepared','backup','retired','rejected')) if v)
        promotion.rows(conn, 'SET FOREIGN_KEY_CHECKS=0')
        for name in names:
            promotion.rows(conn, f'DROP DATABASE IF EXISTS {promotion.ident(name)}')
        conn.close()


def call(context):
    config, conn, live, stage, output, journals = context
    return promotion.promote(config, stage, live, maintenance=True,
        output_root=output, journal_root=journals, expected_counts=COUNTS)


def read_artifact_consumers(db):
    server = _server_engine()
    engine = create_engine(server.url.set(database=db))
    try:
        return load_derived_constants(engine=engine), load_factory_ownership(engine)
    finally:
        engine.dispose(); server.dispose()


def test_promote_real_gate_identity_ownership_and_usable_backup(databases):
    config, conn, live, stage, output, journals = databases
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    result = call(databases)
    assert result['state'] == 'validated'
    values, owners = read_artifact_consumers(live)
    assert values['source_database'] == live
    assert values['origin_database'] == stage
    assert owners == {'2': 1}
    backup, old_owners = read_artifact_consumers(result['backup'])
    assert backup['source_database'] == result['backup']
    assert backup['origin_database'] == live
    assert old_owners == {'1': 1}
    assert promotion.gate(config, result['backup'], output, COUNTS)['result']['valid']
    assert promotion.fingerprint(conn, result['retired'], sorted(SCHEMA)) == before
    assert promotion.rows(conn, "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA IS NOT NULL AND REFERENCED_TABLE_SCHEMA<>%s", (live,live))[0][0] == 0
    # The FK actually enforces the new live parent, not merely its metadata name.
    with pytest.raises(mysql.connector.IntegrityError):
        promotion.rows(conn, f'INSERT INTO {promotion.table(live,"order_factory_ownership")} VALUES (999,1)')
    assert json.loads((output/live/'latest.json').read_text())['valid'] is True
    # Manual atomic rollback exercises the usable retained original table set.
    recovered = promotion.recover(config, result['journal_path'], maintenance=True, rollback=True)
    assert recovered['state'] == 'rolled_back'
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before


def test_latest_failed_verdict_refuses_promotion_without_changing_live(databases):
    config, conn, live, stage, output, journals = databases
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    promotion.rows(conn, f'DELETE FROM {promotion.table(stage,"order_factory_ownership")}')
    assert promotion.gate(config, stage, output, COUNTS)['result']['valid'] is False
    with pytest.raises(promotion.PromotionError, match='valid latest validation'):
        call(databases)
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before


@pytest.mark.parametrize('phase', ['before_cutover', 'after_cutover'])
def test_interrupted_process_leaves_complete_old_or_new_then_recovers(databases, monkeypatch, phase):
    class ProcessDied(BaseException): pass
    def interrupt(name):
        if name == phase: raise ProcessDied()
    monkeypatch.setattr(promotion, 'checkpoint', interrupt)
    config, conn, live, stage, output, journals = databases
    with pytest.raises(ProcessDied): call(databases)
    path = next(journals.glob('*.json'))
    if phase == 'before_cutover':
        assert read_artifact_consumers(live)[1] == {'1': 1}
    else:
        assert read_artifact_consumers(live)[1] == {'2': 1}
        assert promotion.gate(config, live, output, COUNTS)['result']['valid']
    state = promotion.recover(config, path, maintenance=True)
    assert state['state'] == ('old_dataset_active' if phase == 'before_cutover' else 'validated')


def test_live_gate_failure_rolls_back_all_tables(databases, monkeypatch):
    config, conn, live, stage, output, journals = databases
    original = promotion.gate
    def fail_live(config, db, out, counts):
        if db == live: raise RuntimeError('injected post-activation gate failure')
        return original(config, db, out, counts)
    monkeypatch.setattr(promotion, 'gate', fail_live)
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    with pytest.raises(RuntimeError, match='post-activation'):
        call(databases)
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before
    assert json.loads(next(journals.glob('*.json')).read_text())['state'] == 'rolled_back'


def test_lost_rename_acknowledgement_uses_database_marker(databases, monkeypatch):
    config, conn, live, stage, output, journals = databases
    original = promotion.rows
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    injected = False
    def lose_ack(connection, sql, args=()):
        nonlocal injected
        result = original(connection, sql, args)
        if sql.startswith('RENAME TABLE') and not injected:
            injected = True
            raise RuntimeError('simulated lost acknowledgement after committed RENAME')
        return result
    monkeypatch.setattr(promotion, 'rows', lose_ack)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        call(databases)
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before
    assert json.loads(next(journals.glob('*.json')).read_text())['state'] == 'rolled_back'


@pytest.mark.parametrize('phase,expected_order', [('before_cutover', '1'), ('after_cutover', '2')])
def test_actual_process_exit_then_recovery(databases, phase, expected_order):
    import subprocess
    import sys
    config, conn, live, stage, output, journals = databases
    script = """
import json,sys,os
from database import promote_dataset as p
from data_generation.seed_database_fixed import db_config
args=json.load(sys.stdin)
def die(phase):
    if phase == args['phase']: os._exit(73)
p.checkpoint=die
p.promote(db_config,args['stage'],args['live'],maintenance=True,
          output_root=args['output'],journal_root=args['journals'],
          expected_counts={'employee_count':1,'machine_count':1,'factory_count':1})
"""
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps({
        'phase': phase, 'stage': stage, 'live': live,
        'output': str(output), 'journals': str(journals)}),
        text=True, capture_output=True, timeout=120)
    assert result.returncode == 73, result.stderr
    values, ownership = read_artifact_consumers(live)
    assert ownership == {expected_order: 1}
    assert values['source_database'] == live
    path = next(journals.glob('*.json'))
    recovered = promotion.recover(config, path, maintenance=True)
    assert recovered['state'] == ('old_dataset_active' if phase == 'before_cutover' else 'validated')


def test_stale_pass_does_not_bypass_fresh_prepared_validation(databases):
    config, conn, live, stage, output, journals = databases
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    # Leave the old passing record untouched, but corrupt its underlying rows.
    promotion.rows(conn, f'DELETE FROM {promotion.table(stage,"order_factory_ownership")}')
    with pytest.raises(promotion.PromotionError, match='Prepared dataset failed gate'):
        call(databases)
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before


@pytest.mark.parametrize('phase', ['original_recorded', 'prepared_validated'])
@pytest.mark.parametrize('hard_exit', [False, True])
def test_early_failure_recovery_is_repeatable(databases, monkeypatch, phase, hard_exit):
    class Interrupted(BaseException): pass
    failure = Interrupted if hard_exit else RuntimeError
    def stop(name):
        if name == phase: raise failure('pre-activation interruption')
    monkeypatch.setattr(promotion, 'checkpoint', stop)
    config, conn, live, stage, output, journals = databases
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    with pytest.raises(failure): call(databases)
    path = next(journals.glob('*.json'))
    for _ in range(2):
        assert promotion.recover(config, path, maintenance=True)['state'] == 'old_dataset_active'
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before


def _interrupt_before_preparation(context, monkeypatch):
    class Interrupted(BaseException): pass
    def stop(name):
        if name == 'original_recorded': raise Interrupted()
    monkeypatch.setattr(promotion, 'checkpoint', stop)
    with pytest.raises(Interrupted): call(context)
    return next(context[-1].glob('*.json'))


def test_recovery_rejects_incomplete_legacy_journal_clearly(databases, monkeypatch):
    path = _interrupt_before_preparation(databases, monkeypatch)
    journal = json.loads(path.read_text())
    del journal['old_tables']
    del journal['old_fingerprint']
    path.write_text(json.dumps(journal))
    with pytest.raises(promotion.PromotionError, match='Incomplete journal'):
        promotion.recover(databases[0], path, maintenance=True)


def test_recovery_rejects_extra_old_dataset_table(databases, monkeypatch):
    path = _interrupt_before_preparation(databases, monkeypatch)
    config, conn, live, *_ = databases
    promotion.rows(conn, f'CREATE TABLE {promotion.table(live,"unexpected")} (id INT) ENGINE=InnoDB')
    with pytest.raises(promotion.PromotionError, match='table inventory'):
        promotion.recover(config, path, maintenance=True)


def test_recovery_rejects_later_activation_even_with_same_table_inventory(databases, monkeypatch):
    config, conn, live, *_ = databases
    promotion.rows(conn, f'CREATE TABLE {promotion.table(live,promotion.MARKER)} (promotion_id VARCHAR(32) PRIMARY KEY) ENGINE=InnoDB')
    promotion.rows(conn, f'INSERT INTO {promotion.table(live,promotion.MARKER)} VALUES ("previous_activation")')
    path = _interrupt_before_preparation(databases, monkeypatch)
    promotion.rows(conn, f'UPDATE {promotion.table(live,promotion.MARKER)} SET promotion_id="unrelated_later_activation"')
    with pytest.raises(promotion.PromotionError, match='activation identity'):
        promotion.recover(config, path, maintenance=True)


def test_real_connection_loss_requires_fresh_connection_recovery(databases, monkeypatch):
    original = promotion.rows
    cutover = False
    def disconnect(conn, sql, args=()):
        nonlocal cutover
        result = original(conn, sql, args)
        if sql.startswith('RENAME TABLE') and not cutover:
            cutover = True
            conn.close()  # The actual MySQL session is gone, not just a Python error.
            raise mysql.connector.OperationalError('connection lost after commit')
        return result
    monkeypatch.setattr(promotion, 'rows', disconnect)
    with pytest.raises(mysql.connector.Error): call(databases)
    config, conn, live, stage, output, journals = databases
    path = next(journals.glob('*.json'))
    assert json.loads(path.read_text())['state'] == 'recovery_required'
    assert read_artifact_consumers(live)[1] == {'2': 1}
    assert promotion.recover(config, path, maintenance=True)['state'] == 'validated'


@pytest.mark.parametrize('through_recovery', [False, True])
def test_real_false_live_gate_verdict_restores_original(databases, monkeypatch, through_recovery):
    class Interrupted(BaseException): pass
    config, conn, live, stage, output, journals = databases
    before = promotion.fingerprint(conn, live, sorted(SCHEMA))
    def stop(name):
        if name == 'after_cutover' and through_recovery: raise Interrupted()
    monkeypatch.setattr(promotion, 'checkpoint', stop)
    original_gate = promotion.gate
    def invalidate_then_gate(config, db, out, counts):
        if db == live:
            promotion.rows(conn, f'DELETE FROM {promotion.table(live,"order_factory_ownership")}')
        return original_gate(config, db, out, counts)
    monkeypatch.setattr(promotion, 'gate', invalidate_then_gate)
    with pytest.raises(Interrupted if through_recovery else promotion.PromotionError):
        call(databases)
    path = next(journals.glob('*.json'))
    if through_recovery:
        assert promotion.recover(config, path, maintenance=True)['state'] == 'rolled_back'
    assert json.loads(path.read_text())['state'] == 'rolled_back'
    assert json.loads((output/live/'latest.json').read_text())['valid'] is False
    assert promotion.fingerprint(conn, live, sorted(SCHEMA)) == before
    # Neither repeated finalization nor repeated rollback tries to rename again.
    for rollback in (False, True):
        assert promotion.recover(config, path, maintenance=True, rollback=rollback)['state'] == 'old_dataset_active'


def test_recovery_rejects_missing_old_dataset_table(databases, monkeypatch):
    """A DROPPED live table must be caught before recovery declares the old
    dataset active.

    The inventory comparison is what catches this. Without it, recovery would
    fall through to fingerprinting a table that no longer exists and die with
    a raw MySQL error instead of a classified refusal -- so this asserts the
    specific PromotionError, not merely that something failed.
    """
    path = _interrupt_before_preparation(databases, monkeypatch)
    config, conn, live, *_ = databases
    promotion.rows(conn, 'SET FOREIGN_KEY_CHECKS=0')
    # work_schedules is a leaf: nothing references it, so its absence is a
    # pure inventory change rather than an FK-cascade artefact.
    promotion.rows(conn, f'DROP TABLE {promotion.table(live,"work_schedules")}')
    promotion.rows(conn, 'SET FOREIGN_KEY_CHECKS=1')
    with pytest.raises(promotion.PromotionError, match='Original table inventory does not match'):
        promotion.recover(config, path, maintenance=True)
    assert json.loads(path.read_text())['state'] != 'old_dataset_active'


def test_unrelated_later_activation_is_left_completely_untouched(databases, monkeypatch):
    """Recovery must refuse a later, unrelated activation -- and leave it alone.

    Refusing is only half the requirement: an abandoned journal must not roll
    a DIFFERENT promotion's live dataset back, nor relabel it as this
    journal's old dataset. The marker exists before the journal is written so
    the table inventory matches exactly, forcing the activation-identity check
    to be the thing that refuses rather than a coincidental inventory diff.
    """
    config, conn, live, *_ = databases
    promotion.rows(conn, f'CREATE TABLE {promotion.table(live,promotion.MARKER)} (promotion_id VARCHAR(32) PRIMARY KEY) ENGINE=InnoDB')
    promotion.rows(conn, f'INSERT INTO {promotion.table(live,promotion.MARKER)} VALUES ("previous_activation")')
    path = _interrupt_before_preparation(databases, monkeypatch)

    # A later, unrelated promotion succeeds: new marker AND new rows.
    promotion.rows(conn, f'UPDATE {promotion.table(live,promotion.MARKER)} SET promotion_id="unrelated_later_activation"')
    promotion.rows(conn, f"INSERT INTO {promotion.table(live,'production_orders')} VALUES (77,1,1,'Pending')")
    names = sorted(set(SCHEMA) | {promotion.MARKER})
    after_later_activation = promotion.fingerprint(conn, live, names)

    with pytest.raises(promotion.PromotionError, match='activation identity'):
        promotion.recover(config, path, maintenance=True)

    # Untouched: not rolled back, not reclassified, marker still the later one.
    assert promotion.fingerprint(conn, live, names) == after_later_activation
    assert promotion.activation_id(conn, live) == 'unrelated_later_activation'
    assert json.loads(path.read_text())['state'] != 'old_dataset_active'


def test_recovery_demands_original_evidence_even_when_activation_succeeded(databases, monkeypatch):
    """recover() must demand rollback evidence UP FRONT, not only when rolling back.

    This pins recover()'s OWN early guard, which no other test isolates.
    test_recovery_rejects_incomplete_legacy_journal_clearly cannot: on its
    not-activated path require_original() raises the identical 'Incomplete
    journal' message, so that test still passes with recover()'s guard
    removed and therefore proves only that SOME layer refuses.

    The finalize path never calls require_original(). So without the early
    guard, an ACTIVATED promotion whose journal lost its original-state
    evidence would report 'validated' -- telling the operator the promotion
    is complete and reversible when rollback has in fact become impossible.
    """
    class ProcessDied(BaseException):
        pass

    def interrupt(name):
        if name == 'after_cutover':
            raise ProcessDied()

    monkeypatch.setattr(promotion, 'checkpoint', interrupt)
    config, conn, live, *_ = databases
    with pytest.raises(ProcessDied):
        call(databases)

    path = next(databases[-1].glob('*.json'))
    journal = json.loads(path.read_text())
    assert promotion.activation_id(conn, live) == journal['promotion_id'], \
        'precondition: the cutover must really have committed'
    del journal['old_tables']
    del journal['old_fingerprint']
    path.write_text(json.dumps(journal))

    # Finalizing (not rolling back) must still refuse: the evidence needed to
    # reverse this operation is gone, and 'validated' would hide that.
    with pytest.raises(promotion.PromotionError, match='Incomplete journal'):
        promotion.recover(config, path, maintenance=True)
    assert json.loads(path.read_text())['state'] != 'validated'
