from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest

from web_v1.backend.integrations.craft.boundary_validator import validate_snapshot
from web_v1.backend.integrations.craft.contracts import BoundaryUnresolved, digest
from web_v1.backend.integrations.craft.execution_state import normalize_lot
from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot, craft_boundary_payload
from web_v1.backend.integrations.craft.legacy_repository import LegacyExecutionRepository

REFERENCE = "2026-09-10T00:00:00"
DATASET = {"database": "garment_fyp_db", "dataset_version": "unit-test", "reference_time": REFERENCE,
           "timeline_generation_contract": "frozen-plan-then-actual-v1"}


def lot(identifier=1, state="Running", start="2026-09-09T23:00:00", end="2026-09-10T01:00:00", amount=10, op=1):
    return {"schedule_item_id": identifier, "production_order_id": 1, "operation_id": op, "lot_index": identifier,
            "lot_quantity": amount, "execution_status": state, "actual_start_time": start, "actual_end_time": end,
            "actual_work_segments": [[start, end]] if start and end else [], "asset_id": 10,
            "employee_id": 20, "line_id": 30, "batch_id": 40}


def source(lots=None):
    return {"reference_time": REFERENCE, "factory_id": 1, "dataset": deepcopy(DATASET),
            "orders": [{"production_order_id": 1, "factory_id": 1, "target_quantity": 10,
                        "scheduled_start_date": "2026-09-01"}],
            "operations": [{"production_order_id": 1, "operation_id": 1, "operation_sequence_number": 1}],
            "lots": lots if lots is not None else [lot(state="Scheduled", start="2026-09-10T01:00:00", end="2026-09-10T02:00:00")],
            "wip": [], "resources": {"machines": [{"asset_id": 10, "factory_id": 1, "line_id": 30}],
                                       "workers": [{"employee_id": 20, "factory_id": 1}],
                                       "lines": [{"line_id": 30, "factory_id": 1}]}}


def rehash(snapshot):
    snapshot["snapshot_hash"] = digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"})


class ExecutionBoundaryTests(unittest.TestCase):
    def test_running_in_flight(self):
        self.assertEqual(normalize_lot(lot(), REFERENCE, DATASET)["state"], "IN_FLIGHT")

    def test_delayed_in_flight_identical_treatment(self):
        a, b = normalize_lot(lot(), REFERENCE, DATASET), normalize_lot(lot(state="Delayed"), REFERENCE, DATASET)
        a.pop("source_status"); b.pop("source_status")
        self.assertEqual(a, b)

    def test_completed_lot_half_open_endpoint(self):
        result = normalize_lot(lot(state="Done", end=REFERENCE), REFERENCE, DATASET)
        self.assertEqual(result["state"], "COMPLETED")

    def test_start_at_reference_in_flight(self):
        self.assertEqual(normalize_lot(lot(start=REFERENCE), REFERENCE, DATASET)["state"], "IN_FLIGHT")

    def test_started_missing_end_is_in_flight_but_boundary_unresolved(self):
        snapshot = normalize_snapshot(source([lot(end=None)]))
        self.assertEqual(snapshot["operations"][0]["lots"][0]["state"], "IN_FLIGHT")
        self.assertEqual(snapshot["operations"][0]["boundary_status"], "BOUNDARY_UNRESOLVED")

    def test_future_lot_is_not_started(self):
        row = lot(state="Scheduled", start="2026-09-11T00:00:00", end="2026-09-11T01:00:00")
        self.assertEqual(normalize_lot(row, REFERENCE, DATASET)["state"], "NOT_STARTED")

    def test_missing_start_not_treated_as_unstarted(self):
        self.assertEqual(normalize_lot(lot(state="Scheduled", start=None), REFERENCE, DATASET)["state"], "UNRESOLVED")

    def test_timestamp_status_conflict_rejected(self):
        payload = source([lot(state="Done")])
        self.assertFalse(validate_snapshot(normalize_snapshot(payload), payload)["valid_for_craft"])

    def test_other_reference_uses_timestamps_not_stale_status(self):
        result = normalize_lot(lot(state="Running"), "2026-09-11T00:00:00", DATASET)
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(result["errors"], [])

    def test_mixed_quantity_and_no_tail_discarded(self):
        rows = [lot(1, "Done", end=REFERENCE, amount=3), lot(2, amount=2),
                lot(3, "Scheduled", "2026-09-11T00:00:00", "2026-09-11T01:00:00", 5)]
        op = normalize_snapshot(source(rows))["operations"][0]
        self.assertEqual(op["execution_state"], "PARTIALLY_EXECUTED")
        self.assertEqual(op["quantity"]["completed"], 3)
        self.assertEqual(op["quantity"]["remaining"], 7)
        self.assertEqual(op["quantity"]["in_flight"], 2)
        self.assertEqual(op["quantity"]["not_started"], 5)
        self.assertEqual(op["residual_lot_ids"], ["2", "3"])
        self.assertFalse(op["optimizable"])
        self.assertIsNone(op["completion_boundary"])

    def test_completed_prefix_without_current_running_still_partial(self):
        rows = [lot(1, "Done", end=REFERENCE, amount=4),
                lot(2, "Scheduled", "2026-09-11T00:00:00", "2026-09-11T01:00:00", 6)]
        op = normalize_snapshot(source(rows))["operations"][0]
        self.assertEqual(op["execution_state"], "PARTIALLY_EXECUTED")
        self.assertFalse(op["optimizable"])

    def test_fully_completed_safe_historical_boundary(self):
        payload = source([lot(state="Done", end=REFERENCE)])
        snapshot = normalize_snapshot(payload)
        self.assertTrue(validate_snapshot(snapshot, payload)["valid_for_craft"])
        self.assertFalse(snapshot["operations"][0]["optimizable"])
        self.assertEqual(snapshot["operations"][0]["quantity"]["remaining"], 0)
        self.assertEqual(craft_boundary_payload(snapshot, payload)["routing_completion_state"]["1_1"]["authoritative_end_seconds"], 0)

    def test_fully_unstarted_valid(self):
        payload = source()
        snapshot = normalize_snapshot(payload)
        self.assertEqual(snapshot["operations"][0]["execution_state"], "UNSTARTED")
        self.assertTrue(validate_snapshot(snapshot, payload)["valid_for_craft"])

    def test_future_release_does_not_become_ready(self):
        payload = source()
        payload["orders"][0]["scheduled_start_date"] = "2026-09-12"
        op = normalize_snapshot(payload)["operations"][0]
        self.assertTrue(op["optimizable"])
        self.assertFalse(op["precedence_boundary"]["ready_at_reference"])

    def test_machine_worker_preserved_with_unknown_release(self):
        op = normalize_snapshot(source([lot()]))["operations"][0]
        self.assertEqual(op["resource_holds"][0]["machine_id"], 10)
        self.assertEqual(op["resource_holds"][0]["worker_id"], 20)
        self.assertIsNone(op["resource_holds"][0]["release_at"])

    def test_future_actuals_not_commitments(self):
        op = normalize_snapshot(source([lot()]))["operations"][0]
        self.assertEqual(op["committed_execution"], [])
        self.assertIsNotNone(op["projected_completion_not_committed"])
        self.assertIsNone(op["completion_boundary"])
        self.assertEqual(op["lots"][0]["observed_productive_segments"][-1][1], REFERENCE)

    def test_unresolved_handoff_rejected_without_scheduler_import(self):
        payload = source([lot()])
        before = {k for k in sys.modules if k.startswith("crafter")}
        with self.assertRaises(BoundaryUnresolved):
            craft_boundary_payload(normalize_snapshot(payload), payload)
        self.assertEqual(before, {k for k in sys.modules if k.startswith("crafter")})

    def test_precedence_completed_boundary_and_unresolved_predecessor(self):
        payload = source([lot(state="Done", end=REFERENCE),
                          lot(2, "Scheduled", "2026-09-11T00:00:00", "2026-09-11T01:00:00", op=2)])
        payload["operations"].append({"production_order_id": 1, "operation_id": 2, "operation_sequence_number": 2})
        snapshot = normalize_snapshot(payload)
        self.assertTrue(validate_snapshot(snapshot, payload)["valid_for_craft"])
        self.assertEqual(snapshot["operations"][1]["precedence_boundary"]["earliest_ready_lower_bound"], REFERENCE)
        payload["lots"][0] = lot()
        snapshot = normalize_snapshot(payload)
        self.assertIsNone(snapshot["operations"][1]["precedence_boundary"]["earliest_ready_lower_bound"])
        self.assertFalse(snapshot["operations"][1]["precedence_boundary"]["ready_at_reference"])

    def test_deterministic_hash_independent_of_row_order(self):
        payload = source([lot(1, "Done", end=REFERENCE, amount=4), lot(2, amount=6)])
        first = normalize_snapshot(payload)
        payload["lots"].reverse()
        self.assertEqual(first["snapshot_hash"], normalize_snapshot(payload)["snapshot_hash"])
        payload["reference_time"] = "2026-09-10T00:01:00"
        self.assertNotEqual(first["snapshot_hash"], normalize_snapshot(payload)["snapshot_hash"])

    def test_negative_missing_or_excess_quantity_rejected(self):
        for value in (-1, None, 11):
            with self.subTest(value=value):
                payload = source([lot(amount=value)])
                self.assertFalse(validate_snapshot(normalize_snapshot(payload), payload)["valid_for_craft"])

    def test_unknown_provenance_rejected(self):
        payload = source()
        payload["dataset"]["timeline_generation_contract"] = "unknown"
        self.assertFalse(validate_snapshot(normalize_snapshot(payload), payload)["valid_for_craft"])

    def test_timezone_is_not_guessed(self):
        payload = source()
        payload["lots"][0]["actual_start_time"] += "+05:00"
        self.assertFalse(validate_snapshot(normalize_snapshot(payload), payload)["valid_for_craft"])

    def test_duplicate_lot_rejected(self):
        payload = source()
        payload["lots"].append(deepcopy(payload["lots"][0]))
        self.assertIn("DUPLICATE_LOT_IDENTITY", validate_snapshot(normalize_snapshot(payload), payload)["error_counts"])

    def test_missing_resource_or_foreign_ownership_blocks(self):
        for resource, key in (("machines", "asset_id"), ("workers", "employee_id"), ("lines", "line_id")):
            with self.subTest(resource=resource):
                payload = source([lot()])
                payload["resources"][resource][0]["factory_id"] = 2
                result = validate_snapshot(normalize_snapshot(payload), payload)
                self.assertTrue(any(e["code"].startswith("FOREIGN_") for e in result["errors"]))
                payload["resources"][resource] = []
                result = validate_snapshot(normalize_snapshot(payload), payload)
                self.assertTrue(any(e["code"].startswith("MISSING_") for e in result["errors"]))

    def test_wip_does_not_double_count_completed_quantity(self):
        payload = source([lot(state="Done", end=REFERENCE)])
        payload["wip"] = [{"production_order_id": 1, "operation_id": 1, "pieces_completed": 10,
                           "last_updated_time": "2026-09-14T14:31:38"}]
        snapshot = normalize_snapshot(payload)
        self.assertEqual(snapshot["operations"][0]["quantity"]["completed"], 10)
        self.assertTrue(validate_snapshot(snapshot, payload)["valid_for_craft"])

    def test_validator_detects_tampering_even_if_hash_recomputed(self):
        mutations = {
            "STARTED_WORK_OPTIMIZABLE": lambda s: s["operations"][0].update(optimizable=True),
            "IN_FLIGHT_RESOURCE_HOLD_MISSING_OR_DUPLICATE": lambda s: s["operations"][0].update(resource_holds=[]),
            "RESOURCE_HOLD_CHANGED": lambda s: s["operations"][0]["resource_holds"][0].update(worker_id=999),
            "NEGATIVE_REMAINING_QUANTITY": lambda s: s["operations"][0]["quantity"].update(remaining=-1),
            "EXECUTION_BOUNDARY_MISSING": lambda s: s.update(boundary_evidence=[]),
            "OPERATION_IDENTITY_MISMATCH": lambda s: s.update(operations=[]),
            "UNPROVEN_COMPLETION_BOUNDARY": lambda s: s["operations"][0].update(completion_boundary="2026-09-10T01:00:00"),
            "EXECUTION_SEGMENTS_CHANGED": lambda s: s["operations"][0]["lots"][0].update(observed_productive_segments=[]),
            "UNPROVEN_OPERATION_COMMITMENT": lambda s: s["operations"][0].update(committed_execution=[{"asserted": True}]),
        }
        for code, mutate in mutations.items():
            with self.subTest(code=code):
                payload = source([lot()]); snapshot = normalize_snapshot(payload)
                mutate(snapshot); rehash(snapshot)
                self.assertIn(code, validate_snapshot(snapshot, payload)["error_counts"])

    def test_completed_work_cannot_reappear_as_remaining(self):
        payload = source([lot(state="Done", end=REFERENCE)])
        snapshot = normalize_snapshot(payload)
        snapshot["operations"][0]["quantity"]["remaining"] = 10
        snapshot["operations"][0]["residual_lot_ids"] = ["1"]
        rehash(snapshot)
        result = validate_snapshot(snapshot, payload)
        self.assertIn("QUANTITY_ACCOUNTING_MISMATCH", result["error_counts"])
        self.assertIn("COMPLETED_OR_RESIDUAL_WORK_LOST", result["error_counts"])

    def test_successor_cannot_bypass_completion_boundary(self):
        payload = source([lot(state="Done", end=REFERENCE),
                          lot(2, "Scheduled", "2026-09-11T00:00:00", "2026-09-11T01:00:00", op=2)])
        payload["operations"].append({"production_order_id": 1, "operation_id": 2, "operation_sequence_number": 2})
        snapshot = normalize_snapshot(payload)
        snapshot["operations"][1]["precedence_boundary"]["earliest_ready_lower_bound"] = "2026-09-01T00:00:00"
        rehash(snapshot)
        self.assertIn("SUCCESSOR_READINESS_PRECEDES_OR_CHANGES_BOUNDARY", validate_snapshot(snapshot, payload)["error_counts"])


class RecordedFactoryCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[5]
        capture = root / "web_v1/audits/execution_boundary_v1/factory1_source.json"
        if not capture.exists():
            raise AssertionError("Run the read-only boundary audit to capture the real regression evidence")
        cls.payload = json.loads(capture.read_text())
        cls.snapshot = normalize_snapshot(cls.payload)
        cls.by_key = {op["operation_key"]: op for op in cls.snapshot["operations"]}

    def test_all_nine_real_cases_protected(self):
        for key in ("40_902", "40_903", "40_905", "212_2584", "212_2586", "411_2598", "411_2599", "979_952", "212_2585"):
            with self.subTest(operation=key):
                op = self.by_key[key]
                self.assertEqual(op["execution_state"], "PARTIALLY_EXECUTED")
                self.assertFalse(op["optimizable"])
                self.assertEqual(op["boundary_status"], "BOUNDARY_UNRESOLVED")
                self.assertEqual(len(op["resource_holds"]), op["lot_counts"]["IN_FLIGHT"])
                self.assertEqual(op["quantity"]["completed"] + op["quantity"]["remaining"], op["quantity"]["total"])
                self.assertTrue(op["residual_lot_ids"])

    def test_mixed_212_2585_exact_captured_counts(self):
        op = self.by_key["212_2585"]
        self.assertEqual(op["lot_counts"], {"COMPLETED": 56, "IN_FLIGHT": 1, "NOT_STARTED": 3, "UNRESOLVED": 0})
        self.assertEqual(op["quantity"]["completed"], 1867)
        self.assertEqual(op["quantity"]["remaining"], 133)

    def test_real_factory_scope_fails_closed(self):
        result = validate_snapshot(self.snapshot, self.payload)
        self.assertFalse(result["valid_for_craft"])
        self.assertIn("212_2585", result["unresolved_operation_keys"])
        with self.assertRaises(BoundaryUnresolved):
            craft_boundary_payload(self.snapshot, self.payload)


class RepositorySafetyTests(unittest.TestCase):
    def test_connection_identity_failure_rolls_back_and_never_writes(self):
        statements = []

        class Connection:
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params=()): statements.append(sql)
            def fetchall(self):
                if statements[-1] == "SHOW GRANTS":
                    return [{"Grants": "GRANT SELECT ON `garment_fyp_db`.* TO `reader`@`localhost`"}]
                return [{"database_name": "wrong_database", "read_only": 0}]
            def rollback(self): statements.append("ROLLBACK")
            def close(self): statements.append("CLOSE")

        with self.assertRaises(RuntimeError):
            LegacyExecutionRepository(Connection).read_factory(1)
        self.assertEqual(statements[0], "SHOW GRANTS")
        self.assertTrue(all(sql.startswith(("SHOW GRANTS", "SELECT ", "SET SESSION TRANSACTION ", "START TRANSACTION ", "ROLLBACK", "CLOSE")) for sql in statements))
        self.assertEqual(statements[-2:], ["ROLLBACK", "CLOSE"])


if __name__ == "__main__":
    unittest.main()
