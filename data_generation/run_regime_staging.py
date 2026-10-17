"""Create one isolated operating-regime staging dataset; never promotes it."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mysql.connector
from dotenv import load_dotenv

from data_generation import seed_database_fixed as seed
from data_generation.operating_regimes import get_operating_regime
from data_generation.validation_records import validate_and_record


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_regime_staging(*, regime_name: str, reference_time: datetime, planning_seed: int = 42, actual_seed: int = 42) -> dict:
    """Generate and validate a non-promoted, uniquely named regime database."""
    profile = get_operating_regime(regime_name)
    source = seed.db_config["database"]
    target = f"{source}_seed_{profile.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not all(c.isalnum() or c == "_" for c in target):
        raise ValueError("Unsafe staging database name")
    output = ROOT / "outputs" / "regime_experiments" / target
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "manifest.json"
    manifest = {
        "manifest_version": 1,
        "experiment_id": str(uuid.uuid4()),
        "source_database": source,
        "database": target,
        "regime": profile.manifest(),
        "reference_time": reference_time.isoformat(),
        "planning_seed": int(planning_seed),
        "actual_execution_seed": int(actual_seed),
        "promotion": "forbidden; isolated staging only",
        "pairing": "same source order templates/resources/planning seed/actual seed; Phase-B operating severity only",
        "success": False,
        "source_hashes": {
            name: _sha256(ROOT / "data_generation" / name)
            for name in ("seed_database_fixed.py", "simulation_calendar.py", "validate_seed.py", "operating_regimes.py")
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    config = dict(seed.db_config)
    connection = mysql.connector.connect(**config)
    try:
        cursor = connection.cursor()
        cursor.execute(f"CREATE DATABASE `{target}`")
        cursor.execute(f"SHOW FULL TABLES FROM `{source}` WHERE Table_type='BASE TABLE'")
        tables = [row[0] for row in cursor.fetchall()]
        ddls = []
        for table in tables:
            cursor.execute(f"SHOW CREATE TABLE `{source}`.`{table}`")
            ddls.append(cursor.fetchone()[1])
        cursor.execute(f"USE `{target}`")
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        for ddl in ddls:
            cursor.execute(ddl)
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        cursor.close()
        connection.close()

    original_database = seed.db_config["database"]
    seed.db_config["database"] = target
    try:
        seed.execute_complete_seed(
            reference_time=reference_time,
            planning_seed=planning_seed,
            actual_seed=actual_seed,
            operating_regime=profile,
        )
        validation_connection = mysql.connector.connect(**dict(config, database=target))
        try:
            validation_connection.start_transaction(readonly=True)
            record = validate_and_record(validation_connection)
            result = record["result"]
            cursor = validation_connection.cursor(dictionary=True)
            cursor.execute("SELECT COUNT(*) AS orders FROM Production_Orders")
            counts = dict(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) AS schedule_rows, COUNT(DISTINCT production_order_id) AS scheduled_orders, COUNT(DISTINCT operation_id) AS operations FROM Job_Shop_Schedules")
            counts.update(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) AS machines FROM Machinery_Assets")
            counts.update(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) AS workers FROM Employees")
            counts.update(cursor.fetchone())
            cursor.close()
        finally:
            validation_connection.rollback()
            validation_connection.close()
        if not result["valid"]:
            raise RuntimeError(f"Staging validation failed: {result['errors']}")
        manifest.update({
            "dataset_version": record["dataset_version"],
            "validation_id": record["validation_id"],
            "validation_record": str(ROOT / "outputs" / "dataset_validations" / target / "latest.json"),
            "row_counts": counts,
            "success": True,
        })
    except BaseException as exc:
        manifest["error"] = repr(exc)
        raise
    finally:
        seed.db_config["database"] = original_database
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regime", choices=("nominal", "busy"))
    parser.add_argument("--reference-time", default="2026-09-10T00:00:00")
    parser.add_argument("--planning-seed", type=int, default=42)
    parser.add_argument("--actual-seed", type=int, default=42)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    manifest = create_regime_staging(
        regime_name=args.regime,
        reference_time=datetime.fromisoformat(args.reference_time),
        planning_seed=args.planning_seed,
        actual_seed=args.actual_seed,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
