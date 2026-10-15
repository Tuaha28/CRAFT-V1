"""Source-derived boundary and monotone resource-domain expansion checks."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import unittest

from .analyze_scope import SOURCE, SNAPSHOT, SCENARIO, SOURCE_SHA
from .boundary_policy import assignment_hash, check_boundary
from .run_bounded import HERE, construct, next_earliest_order_keys
from .validate_bounded import validate_payload


class BoundaryDomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SOURCE.read_bytes()
        cls.source = json.loads(cls.raw)
        cls.snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cls.scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
        cls.saved = json.loads((HERE / "bounded_25_cached_v2.json").read_text(encoding="utf-8"))
        cls.keys = next_earliest_order_keys()
        cls.outside_key = next(row["operation_key"] for row in cls.snapshot["operations"]
                               if row["operation_key"] not in cls.keys and
                               row["execution_state"] == "UNSTARTED")

    def boundary(self, assignment):
        rows = [assignment]
        return {"classification": "APPROVED_TEST_SCHEDULE_SNAPSHOT", "status": "ACTIVE",
                "active_version_id": "TEST-V1", "source_sha256": SOURCE_SHA,
                "assignments": rows, "assignments_sha256": assignment_hash(rows)}

    def test_independent_validator_rejects_approved_machine_and_worker_conflict(self):
        selected = self.saved["windows"][0]["selected_schedule"][0]
        worker = int(selected["resource_configuration"]["worker_ids"][0])
        left, right = selected["worker_segments"][0]
        assignment = {"operation_key": self.outside_key,
                      "machine_id": selected["resource_configuration"]["machine_ids"][0],
                      "start_seconds": selected["start_seconds"],
                      "end_seconds": selected["end_seconds"],
                      "worker_segments": [[worker, left, right]]}
        boundary = self.boundary(assignment)
        self.assertEqual(check_boundary(boundary, source_sha256=SOURCE_SHA,
                                       selected_keys=set(self.keys),
                                       known_keys={r["operation_key"] for r in self.snapshot["operations"]}), ())
        candidate = deepcopy(self.saved)
        candidate["domain"]["outside_approved_version"] = boundary["active_version_id"]
        candidate["domain"]["outside_approved_assignment_hash"] = boundary["assignments_sha256"]
        candidate["scheduling_input"]["domain"] = candidate["domain"]
        candidate["scheduling_input_hash"] = sha256(json.dumps(
            candidate["scheduling_input"], sort_keys=True, default=str,
            separators=(",", ":")).encode()).hexdigest()
        missing = validate_payload(self.source, self.snapshot, self.scenario,
                                   candidate, self.raw)
        self.assertIn("OUTSIDE_BOUNDARY_MISSING", missing["errors"])
        outcome = validate_payload(self.source, self.snapshot, self.scenario,
                                   candidate, self.raw, boundary)
        self.assertEqual(outcome["status"], "FAIL")
        self.assertNotIn("OUTSIDE_BOUNDARY_FINGERPRINT_MISMATCH", outcome["errors"])
        self.assertTrue(any(x.startswith("MACHINE_OVERLAP:") for x in outcome["errors"]))
        self.assertTrue(any(x.startswith("WORKER_OVERLAP:") for x in outcome["errors"]))

    def test_approved_boundary_rejects_stale_fingerprint(self):
        row = {"operation_key": self.outside_key, "machine_id": 1,
               "start_seconds": 0, "end_seconds": 60, "worker_segments": []}
        boundary = self.boundary(row)
        boundary["assignments"][0]["end_seconds"] = 61
        errors = check_boundary(boundary, source_sha256=SOURCE_SHA,
                                selected_keys=set(self.keys),
                                known_keys={r["operation_key"] for r in self.snapshot["operations"]})
        self.assertIn("OUTSIDE_APPROVAL_FINGERPRINT", errors)

    def test_runner_rejects_outside_schedule_overlapping_fixed_hold(self):
        hold = next(hold for partial in self.scenario["partial_operations"]
                    for hold in partial["fixed_in_flight_holds"])
        origin = datetime.fromisoformat(self.source["reference_time"])
        left = int((datetime.fromisoformat(hold["machine_hold_from"]) - origin).total_seconds())
        right = int((datetime.fromisoformat(hold["machine_hold_until"]) - origin).total_seconds())
        boundary = self.boundary({"operation_key": self.outside_key,
                                  "machine_id": hold["machine_id"],
                                  "start_seconds": left, "end_seconds": right,
                                  "worker_segments": []})
        with self.assertRaisesRegex(RuntimeError, "OUTSIDE_APPROVED_MACHINE_CONFLICT"):
            construct(self.keys, approved_boundary=boundary)

    def test_expanded_source_domain_contains_original_configurations(self):
        _, _, _, _, base, _, base_meta = construct(self.keys, domain_width=3)
        _, _, _, _, expanded, _, expanded_meta = construct(self.keys, domain_width=4)
        self.assertEqual(sum(map(len, base.values())), 225)
        self.assertEqual(sum(map(len, expanded.values())), 400)
        for key in self.keys:
            self.assertTrue(set(base[key]).issubset(set(expanded[key])))
            self.assertEqual(len(expanded[key]), 16)
        self.assertFalse(base_meta["complete"])
        self.assertFalse(expanded_meta["complete"])
        self.assertTrue(expanded_meta["truncated"])


if __name__ == "__main__":
    unittest.main()
