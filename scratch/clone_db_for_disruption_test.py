"""
One-off setup: clones the live garment_fyp_db into a disposable
garment_fyp_db_postmortem_test database on the SAME local MySQL server
(root access, per .env DB_HOST=localhost), for real, mutating disruption
tests (agent/event_handler.py::handle_machine_breakdown) that must NOT
touch production data. Server-side CREATE TABLE ... LIKE + INSERT ... SELECT
per table -- fast, no data leaves the local MySQL instance.

Run once. scratch/disruption_stress_test.py (run with DB_NAME env pointed
at the clone) does the actual mutating test, and drops this database when
done.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from utils.config import DB_USER, DB_HOST, DB_PORT, DB_NAME
from urllib.parse import quote_plus

CLONE_DB = "garment_fyp_db_postmortem_test"

import urllib.parse
pw = os.getenv("DB_PASS")
encoded_pass = quote_plus(pw)
server_url = f"mysql+pymysql://{DB_USER}:{encoded_pass}@{DB_HOST}:{DB_PORT}/"

engine = create_engine(server_url)

with engine.connect() as conn:
    conn.execute(text(f"DROP DATABASE IF EXISTS `{CLONE_DB}`"))
    conn.execute(text(f"CREATE DATABASE `{CLONE_DB}`"))
    conn.commit()
    print(f"Created {CLONE_DB}")

    tables = [r[0] for r in conn.execute(text(
        f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{DB_NAME}'"
    ))]
    print(f"Cloning {len(tables)} tables from {DB_NAME}: {tables}")

    # FK constraint ordering doesn't matter with this off -- avoids having to
    # topologically sort table creation, and this is a disposable DB anyway.
    conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    conn.commit()

    for t in tables:
        conn.execute(text(f"CREATE TABLE `{CLONE_DB}`.`{t}` LIKE `{DB_NAME}`.`{t}`"))
        conn.commit()
        conn.execute(text(f"INSERT INTO `{CLONE_DB}`.`{t}` SELECT * FROM `{DB_NAME}`.`{t}`"))
        conn.commit()
        count = conn.execute(text(f"SELECT COUNT(*) FROM `{CLONE_DB}`.`{t}`")).scalar()
        print(f"  {t}: {count} rows cloned")

    conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    conn.commit()

print("\nDONE -- clone ready at database:", CLONE_DB)
