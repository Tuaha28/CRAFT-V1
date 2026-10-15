"""
Real, disposable-DB tests for database/ga_snapshot.py -- builds a small,
distinctively-named test snapshot against the real local MySQL server
(same server-side clone mechanism as scratch/clone_db_for_disruption_test.py),
verifies the metadata roundtrip, then drops it. No mocking of the database,
consistent with this project's standing test discipline.
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from database.ga_snapshot import (
    build_snapshot, get_snapshot_origin, get_snapshot_db_name,
    list_snapshots, activate_snapshot, _server_engine, SNAPSHOT_PREFIX,
)

TEST_NAME = "pytest_disposable_test"


@pytest.fixture(scope="module")
def snapshot():
    """Module-scoped: one real clone of the (26-table) live DB is enough
    to exercise every read-only assertion in this file -- rebuilding it
    per-test would multiply a ~20s operation across every test for no
    real gain, since none of the read-only tests mutate the snapshot."""
    info = build_snapshot(TEST_NAME)
    yield info
    engine = _server_engine()
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{info['snapshot_db']}`"))
        conn.commit()


def test_snapshot_db_name_uses_the_documented_prefix():
    assert get_snapshot_db_name("foo") == f"{SNAPSHOT_PREFIX}foo"


def test_build_snapshot_creates_a_real_database_with_cloned_tables(snapshot):
    engine = _server_engine()
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :db"
        ), {"db": snapshot["snapshot_db"]}).fetchone()
        assert exists is not None

        source_tables = {r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db"
        ), {"db": snapshot["source_db"]})}
        snapshot_tables = {r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db"
        ), {"db": snapshot["snapshot_db"]})}
        assert source_tables <= snapshot_tables

        for t in list(source_tables)[:3]:
            src_count = conn.execute(text(f"SELECT COUNT(*) FROM `{snapshot['source_db']}`.`{t}`")).scalar()
            dst_count = conn.execute(text(f"SELECT COUNT(*) FROM `{snapshot['snapshot_db']}`.`{t}`")).scalar()
            assert src_count == dst_count


def test_origin_timestamp_is_captured_before_cloning_not_after(snapshot):
    assert snapshot["origin_timestamp"] < datetime.now()


def test_get_snapshot_origin_matches_what_build_snapshot_recorded(snapshot):
    origin = get_snapshot_origin(TEST_NAME)
    assert abs((origin - snapshot["origin_timestamp"]).total_seconds()) < 1


def test_get_snapshot_origin_is_frozen_not_the_current_wall_clock(snapshot):
    origin = get_snapshot_origin(TEST_NAME)
    assert origin < datetime.now() - timedelta(seconds=0)
    assert (datetime.now() - origin).total_seconds() < 300


def test_get_snapshot_origin_raises_clearly_for_a_snapshot_that_does_not_exist():
    with pytest.raises(Exception):
        get_snapshot_origin("does_not_exist_pytest_probe")


def test_list_snapshots_finds_a_real_built_snapshot_with_correct_metadata(snapshot):
    found = [s for s in list_snapshots() if s["snapshot_db"] == snapshot["snapshot_db"]]
    assert len(found) == 1
    assert found[0]["snapshot_name"] == TEST_NAME
    assert found[0]["source_db"] == snapshot["source_db"]


def test_build_snapshot_overwrites_an_existing_snapshot_of_the_same_name(snapshot):
    engine = _server_engine()
    with engine.connect() as conn:
        conn.execute(text(
            f"INSERT INTO `{snapshot['snapshot_db']}`.`_ga_snapshot_metadata` "
            "(snapshot_name, source_db, created_at, origin_timestamp) VALUES "
            "('marker_row', 'marker', NOW(), NOW())"
        ))
        conn.commit()
        count_before = conn.execute(text(
            f"SELECT COUNT(*) FROM `{snapshot['snapshot_db']}`.`_ga_snapshot_metadata`"
        )).scalar()
        assert count_before == 2

    build_snapshot(TEST_NAME)
    with engine.connect() as conn:
        count_after = conn.execute(text(
            f"SELECT COUNT(*) FROM `{snapshot['snapshot_db']}`.`_ga_snapshot_metadata`"
        )).scalar()
        assert count_after == 1


def test_activate_snapshot_sets_the_db_name_environment_variable(snapshot):
    original = os.environ.get("DB_NAME")
    try:
        activate_snapshot(TEST_NAME)
        assert os.environ["DB_NAME"] == snapshot["snapshot_db"]
    finally:
        if original is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = original


def test_build_snapshot_reidentifies_lowercase_calibration_end_to_end():
    """Real source DB -> build_snapshot -> real destination calibration reader."""
    import json
    from uuid import uuid4
    from sqlalchemy import create_engine
    from tests.test_validate_seed_table_case import calibration
    from utils.derived_constants import load_derived_constants
    token = uuid4().hex[:12]
    source = 'garment_snapshot_case_source_' + token
    name = 'case_regression_' + token
    destination = get_snapshot_db_name(name)
    engine = _server_engine()
    destination_engine = None
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE DATABASE `{source}`'))
            conn.execute(text(f'CREATE TABLE `{source}`.dataset_calibration (singleton_id INT PRIMARY KEY, payload LONGTEXT NOT NULL)'))
            conn.execute(text(f'INSERT INTO `{source}`.dataset_calibration VALUES (1,:payload)'),
                         {'payload': json.dumps(calibration(source))})
        info = build_snapshot(name, source_db=source)
        assert info['snapshot_db'] == destination
        destination_engine = create_engine(engine.url.set(database=destination))
        values = load_derived_constants(engine=destination_engine)
        assert values['source_database'] == destination
        assert values['origin_database'] == source
        assert values['clone_provenance']['source_database'] == source
        assert values['clone_provenance']['source_dataset_version'] == 'test-version'
        assert values['dataset_version'] != 'test-version'
        with engine.connect() as conn:
            original = json.loads(conn.execute(text(f'SELECT payload FROM `{source}`.dataset_calibration')).scalar_one())
        assert original == calibration(source)
    finally:
        if destination_engine is not None:
            destination_engine.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS `{destination}`'))
            conn.execute(text(f'DROP DATABASE IF EXISTS `{source}`'))
        engine.dispose()
