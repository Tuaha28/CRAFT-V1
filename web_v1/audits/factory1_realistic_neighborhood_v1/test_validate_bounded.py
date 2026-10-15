"""Negative checks against the independent assembled-schedule validator."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import unittest

from .analyze_scope import SOURCE, SNAPSHOT, SCENARIO
from .validate_bounded import HERE, validate_payload


class BoundedValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SOURCE.read_bytes()
        cls.source = json.loads(cls.raw)
        cls.snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cls.scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
        cls.saved = json.loads((HERE / "bounded_result.json").read_text(encoding="utf-8"))
        cls.saved16 = json.loads((HERE / "bounded_16_full_evidence_v2.json").read_text(encoding="utf-8"))
        cls.saved25 = json.loads((HERE / "bounded_25_cached_v2.json").read_text(encoding="utf-8"))

    def check_mutation(self, change, expected, *, change_scenario=None):
        result = deepcopy(self.saved)
        scenario = deepcopy(self.scenario) if change_scenario else self.scenario
        change(result)
        if change_scenario:
            change_scenario(scenario)
            content = {k: v for k, v in scenario.items() if k != "scenario_hash"}
            scenario["scenario_hash"] = sha256(json.dumps(content, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest()
            result["scenario_hash"] = scenario["scenario_hash"]
        outcome = validate_payload(self.source, self.snapshot, scenario, result, self.raw)
        self.assertEqual(outcome["status"], "FAIL")
        self.assertTrue(any(expected in error for error in outcome["errors"]), outcome["errors"])

    def test_saved_candidate(self):
        self.assertEqual(validate_payload(self.source, self.snapshot, self.scenario,
                                          self.saved, self.raw)["status"], "PASS")

    def test_saved_sixteen_operation_candidate(self):
        outcome = validate_payload(self.source, self.snapshot, self.scenario,
                                   self.saved16, self.raw)
        self.assertEqual(outcome["status"], "PASS", outcome["errors"])
        self.assertTrue(outcome["scheduling_input_fingerprint_verified"])

    def test_saved_twenty_five_operation_candidate(self):
        outcome = validate_payload(self.source, self.snapshot, self.scenario,
                                   self.saved25, self.raw)
        self.assertEqual(outcome["status"], "PASS", outcome["errors"])
        self.assertEqual(outcome["checked_operations"], 25)
        self.assertTrue(outcome["scheduling_input_fingerprint_verified"])

    def test_scheduling_input_fingerprint_changed(self):
        result = deepcopy(self.saved16)
        result["scheduling_input"]["ledger"]["790_972"]["tail"] += 1
        outcome = validate_payload(self.source, self.snapshot, self.scenario, result, self.raw)
        self.assertIn("SCHEDULING_INPUT_FINGERPRINT", outcome["errors"])

    def test_machine_overlap(self):
        def change(result):
            result["windows"][0]["selected_schedule"][2]["start_seconds"] = 0
        self.check_mutation(change, "MACHINE_OVERLAP")

    def test_worker_overlap(self):
        def change(result):
            rows = result["windows"][0]["selected_schedule"]
            rows[2]["worker_segments"] = deepcopy(rows[0]["worker_segments"])
        self.check_mutation(change, "WORKER_OVERLAP")

    def test_wrong_machine_capability(self):
        def change(result):
            row = result["windows"][0]["selected_schedule"][2]
            row["resource_configuration"]["machine_ids"] = [55]
        self.check_mutation(change, "SOURCE_RESOURCE_ELIGIBILITY")

    def test_wrong_worker_skill(self):
        skilled = {r["employee_id"] for r in self.source["resources"]["skills"]
                   if r["operation_type"].lower() == "seam edge overlock"}
        outsider = next(r["employee_id"] for r in self.source["resources"]["workers"]
                        if r["factory_id"] == 1 and r["is_active"] and r["employee_id"] not in skilled)
        def change(result):
            result["windows"][0]["selected_schedule"][2]["resource_configuration"]["worker_ids"] = [str(outsider)]
        self.check_mutation(change, "SOURCE_RESOURCE_ELIGIBILITY")

    def test_lost_identity(self):
        self.check_mutation(lambda r: r["windows"][0]["selected_schedule"].pop(), "OPERATION_IDENTITY")

    def test_duplicate_identity(self):
        def change(result):
            result["windows"][0]["selected_schedule"].append(deepcopy(result["windows"][0]["selected_schedule"][0]))
        self.check_mutation(change, "OPERATION_IDENTITY")

    def test_quantity_ledger_changed(self):
        self.check_mutation(lambda r: r["ledger"]["790_972"].__setitem__("tail", 20), "SCHEDULED_QUANTITY_LEDGER")

    def test_early_successor(self):
        def change(result):
            row = next(x for x in result["windows"][0]["selected_schedule"] if x["operation_key"] == "33_3579")
            row["start_seconds"] = 0
        self.check_mutation(change, "WHOLE_OPERATION_PRECEDENCE")

    def test_cross_window_key_change(self):
        def change(result):
            result["windows"][1]["keys"][0] = "790_972"
        self.check_mutation(change, "WINDOW_ACCEPTANCE_OR_CARRY")

    def test_fixed_hold_changed(self):
        def change_scenario(scenario):
            partial = next(p for p in scenario["partial_operations"] if p["operation_key"] == "790_974")
            partial["fixed_in_flight_holds"][0]["machine_id"] = 55
        self.check_mutation(lambda _r: None, "FIXED_HOLD_LOT_MAPPING", change_scenario=change_scenario)

    def test_scenario_hash_changed(self):
        self.check_mutation(lambda r: r.__setitem__("scenario_hash", "0" * 64), "SOURCE_OR_SIMULATION_IDENTITY")


if __name__ == "__main__":
    unittest.main()
