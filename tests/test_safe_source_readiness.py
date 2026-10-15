"""Offline safety tests; no legacy or Django database connection."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web_v1/backend"))

from integrations.craft.legacy_repository import LegacyExecutionRepository, verify_select_only_grants
from integrations.craft.source_readiness import audit_capture


class GrantGateTests(unittest.TestCase):
    def test_explicit_select_only_grant(self):
        verify_select_only_grants([
            {"Grants": "GRANT USAGE ON *.* TO `craft_reader`@`localhost`"},
            {"Grants": "GRANT SELECT ON `garment_fyp_db`.* TO `craft_reader`@`localhost`"},
        ])

    def test_reject_write_grant_and_roles(self):
        for grant in (
            "GRANT SELECT, INSERT ON `garment_fyp_db`.* TO `reader`@`localhost`",
            "GRANT SELECT ON `garment_fyp_db`.* TO `reader`@`localhost` WITH GRANT OPTION",
            "GRANT `writer_role`@`%` TO `reader`@`localhost`",
        ):
            with self.subTest(grant=grant), self.assertRaises(PermissionError):
                verify_select_only_grants([{"Grants": grant}])

    def test_no_root_or_implicit_legacy_user(self):
        for config in ({"DB_USER": "root", "DB_PASS": "placeholder"},
                       {"LEGACY_DB_USER": "root", "LEGACY_DB_PASSWORD": "placeholder"}):
            with self.subTest(config=config), self.assertRaises(PermissionError):
                LegacyExecutionRepository.from_config(config)

    def test_grants_checked_before_any_extraction(self):
        commands = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, _params=None):
                commands.append(sql)

            def fetchall(self):
                return [{"Grants": "GRANT INSERT ON `garment_fyp_db`.* TO `reader`@`localhost`"}]

        class Connection:
            def cursor(self):
                return Cursor()

            def rollback(self):
                pass

            def close(self):
                pass

        with self.assertRaises(PermissionError):
            LegacyExecutionRepository(Connection).read_factory(1)
        self.assertEqual(commands, ["SHOW GRANTS"])

    def test_non_transactional_table_rejected_before_source_read(self):
        commands = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, _params=None): commands.append(sql)
            def fetchall(self):
                sql = commands[-1]
                if sql == "SHOW GRANTS":
                    return [{"Grants": "GRANT SELECT ON `garment_fyp_db`.* TO `reader`@`localhost`"}]
                if sql.startswith("SELECT DATABASE()"):
                    return [{"database_name": "garment_fyp_db", "read_only": 1}]
                if "information_schema.TABLES" in sql:
                    return [{"TABLE_NAME": "routing_operations", "ENGINE": "MyISAM"}]
                raise AssertionError("Unexpected data extraction")

        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass
            def close(self): pass

        with self.assertRaisesRegex(RuntimeError, "InnoDB"):
            LegacyExecutionRepository(Connection).read_factory(1)
        self.assertFalse(any("dataset_calibration" in sql for sql in commands))


class OfflineCaptureTests(unittest.TestCase):
    def test_factory1_pinned_capture_never_infers_one_worker(self):
        root = ROOT / "web_v1/audits/execution_boundary_v1"
        manifest = json.loads((root / "artifact_hash_manifest.json").read_text())
        report = audit_capture(root / "factory1_source.json",
                               expected_sha256=manifest["factory1_source.json"])
        self.assertEqual(report["operation_count"], 2879)
        self.assertEqual(report["counts"].get("COMPLETED"), 171)
        self.assertEqual(report["counts"].get("BLOCKED"), 82)
        self.assertEqual(report["counts"].get("REQUIRES_EVIDENCE"), 2626)
        self.assertEqual(report["counts"].get("READY_FOR_PREPARATION", 0), 0)
        self.assertTrue(all(row["crew_size"] is None for row in report["decisions"]))

    def test_wrong_capture_hash_fails_closed(self):
        path = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            audit_capture(path, expected_sha256="0" * 64)


if __name__ == "__main__":
    unittest.main()
