"""
Frozen order-book snapshots for GA validation.

Why this exists: GA_AGENT_POSTMORTEM.md Section 7e found that two runs of
the "same" experiment (window 1, seed 123) produced opposite results
(-3.66% then +0.807%) not because the memetic mechanism is unstable, but
because "window 1, seed 123" identifies a RANK-based order selection
against the LIVE database, re-evaluated at whatever `datetime.now()` was
when the script happened to run. Real time passing between runs moves the
underlying order book (completions, releases, machine status), so the two
runs were silently different optimization problems wearing the same
label. A frozen snapshot fixes both halves of that: the DB content AND
the "now" origin used for every relative-time computation (release
floors, eligibility, deadlines).

Mechanism: clones every table from the live DB into a separate, PERSISTENT
database on the same MySQL server (same server-side CREATE TABLE ... LIKE +
INSERT ... SELECT pattern already used for disposable test clones in
scratch/clone_db_for_disruption_test.py -- this is the same idea, but kept
around and named, not dropped after one use). A one-row metadata table
inside the snapshot records the origin timestamp captured at build time,
so every future script that reads this snapshot uses the SAME "now" -- it
does not call datetime.now() itself.

Usage, from any validation script (this MUST happen before any other
project import -- database.db/utils.config read DB_NAME from the
environment at import time, exactly like the existing disposable-clone
scripts already require):

    import database.ga_snapshot as ga_snapshot
    ga_snapshot.activate_snapshot("20260825")   # sets DB_NAME env var
    # only now import anything that touches the DB:
    from database.db import get_engine
    from graph.graph_builder import FactoryGraphBuilder
    ...
    ORIGIN = ga_snapshot.get_snapshot_origin("20260825")
    sched = GAScheduler(..., current_time=ORIGIN, ...)

To build a new snapshot (from the live DB, run standalone, NOT imported
mid-script since it must run before any snapshot is activated):

    venv\\Scripts\\python.exe -m database.ga_snapshot build 20260825
"""
import os
from datetime import datetime
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

SNAPSHOT_PREFIX = "garment_fyp_db_ga_snapshot_"
METADATA_TABLE = "_ga_snapshot_metadata"


def get_snapshot_db_name(name: str) -> str:
    return f"{SNAPSHOT_PREFIX}{name}"


def _server_engine():
    """Connects at the SERVER level (no DB in the URL) using creds only --
    deliberately does not import utils.config's DATABASE_URL, since that
    picks up whatever DB_NAME is currently set in the environment, and
    this module must work correctly both before and after a snapshot has
    been activated."""
    db_user = os.getenv("DB_USER", "root")
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "3306")
    db_pass = os.getenv("DB_PASS")
    if not db_pass:
        raise RuntimeError("DB_PASS is not set -- see .env.example.")
    encoded_pass = quote_plus(db_pass)
    return create_engine(f"mysql+pymysql://{db_user}:{encoded_pass}@{db_host}:{db_port}/")


def build_snapshot(name: str, source_db: str = None) -> dict:
    """
    Clones every table from `source_db` (default: DB_NAME's current live
    value, i.e. the real garment_fyp_db unless overridden) into a new,
    persistent `garment_fyp_db_ga_snapshot_{name}` database, and records
    the origin timestamp (captured NOW, before any cloning happens) in a
    one-row metadata table inside the snapshot.

    Overwrites any existing snapshot of the same name (DROP + CREATE) --
    call with a new `name` to keep an old one around instead.
    """
    origin_timestamp = datetime.now()
    source_db = source_db or os.getenv("DB_NAME", "garment_fyp_db")
    snapshot_db = get_snapshot_db_name(name)

    engine = _server_engine()
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{snapshot_db}`"))
        conn.execute(text(f"CREATE DATABASE `{snapshot_db}`"))
        conn.commit()
        print(f"Created {snapshot_db}")

        tables = [r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :src"
        ), {"src": source_db})]
        print(f"Cloning {len(tables)} tables from {source_db}: {tables}")

        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        conn.commit()

        for t in tables:
            if t == METADATA_TABLE:
                continue  # destination records its own snapshot metadata
            conn.execute(text(f"CREATE TABLE `{snapshot_db}`.`{t}` LIKE `{source_db}`.`{t}`"))
            conn.commit()
            conn.execute(text(f"INSERT INTO `{snapshot_db}`.`{t}` SELECT * FROM `{source_db}`.`{t}`"))
            conn.commit()
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{snapshot_db}`.`{t}`")).scalar()
            print(f"  {t}: {count} rows cloned")

        if 'dataset_calibration' in {table.lower() for table in tables}:
            import json
            from utils.derived_constants import calibration_for_clone
            raw = conn.execute(text(f"SELECT payload FROM `{snapshot_db}`.Dataset_Calibration WHERE singleton_id=1")).scalar_one()
            calibration = calibration_for_clone(json.loads(raw), source_db, snapshot_db)
            conn.execute(text(f"UPDATE `{snapshot_db}`.Dataset_Calibration SET payload=:payload WHERE singleton_id=1"),
                         {'payload': json.dumps(calibration)})
            conn.commit()

        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        conn.commit()

        conn.execute(text(
            f"CREATE TABLE `{snapshot_db}`.`{METADATA_TABLE}` ("
            "snapshot_name VARCHAR(128), source_db VARCHAR(128), "
            "created_at DATETIME, origin_timestamp DATETIME)"
        ))
        conn.commit()
        conn.execute(text(
            f"INSERT INTO `{snapshot_db}`.`{METADATA_TABLE}` "
            "(snapshot_name, source_db, created_at, origin_timestamp) VALUES "
            "(:name, :source_db, :created_at, :origin_timestamp)"
        ), {"name": name, "source_db": source_db, "created_at": datetime.now(),
            "origin_timestamp": origin_timestamp})
        conn.commit()

    print(f"\nDONE -- snapshot ready at database: {snapshot_db}")
    print(f"Origin timestamp (use this as `current_time` in every future run "
          f"against this snapshot, never datetime.now()): {origin_timestamp}")
    return {"snapshot_db": snapshot_db, "source_db": source_db, "origin_timestamp": origin_timestamp}


def get_snapshot_origin(name: str) -> datetime:
    """The frozen 'now' every script using this snapshot must pass as
    GAScheduler's `current_time` -- NOT datetime.now()."""
    snapshot_db = get_snapshot_db_name(name)
    engine = _server_engine()
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT origin_timestamp FROM `{snapshot_db}`.`{METADATA_TABLE}` LIMIT 1"
        )).fetchone()
    if row is None:
        raise RuntimeError(f"No metadata row found in snapshot '{name}' -- was it built with build_snapshot()?")
    return row[0]


def activate_snapshot(name: str):
    """
    Points DB_NAME at this snapshot for the rest of the process.

    MUST be called before importing anything that touches the database
    (database.db, utils.config, graph.graph_builder, ga.scheduler, ...) --
    those modules read DB_NAME from the environment at import time.
    """
    snapshot_db = get_snapshot_db_name(name)
    os.environ["DB_NAME"] = snapshot_db
    print(f"[ga_snapshot] DB_NAME activated -> {snapshot_db}")


def list_snapshots() -> list:
    engine = _server_engine()
    with engine.connect() as conn:
        dbs = [r[0] for r in conn.execute(text(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE :pat"
        ), {"pat": f"{SNAPSHOT_PREFIX}%"})]
        out = []
        for db in dbs:
            row = conn.execute(text(
                f"SELECT snapshot_name, source_db, created_at, origin_timestamp "
                f"FROM `{db}`.`{METADATA_TABLE}` LIMIT 1"
            )).fetchone()
            if row:
                out.append({"snapshot_db": db, "snapshot_name": row[0], "source_db": row[1],
                             "created_at": row[2], "origin_timestamp": row[3]})
        return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3 or sys.argv[1] != "build":
        print("Usage: python -m database.ga_snapshot build <name>")
        sys.exit(1)
    build_snapshot(sys.argv[2])
