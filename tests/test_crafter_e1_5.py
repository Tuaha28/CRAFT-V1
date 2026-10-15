"""Bounded contracts for the isolated E1.5 realization feedback layer."""
from crafter.lns_probe.run_e1_5 import _cuts_from_failure, _option_fp


def test_feedback_cut_is_timing_specific_not_mode_wide():
    selected = {"A": {"line_id": 4, "machine_count": 2,
                       "start_seconds": 100, "end_seconds": 200}}
    diag = {"machine_failures": [{"operation_key": "A",
                                   "reason": "outside machine boundary",
                                   "machine_id": 9}],
            "worker_failures": [], "envelope_deltas": {}}
    cuts = _cuts_from_failure(diag, selected, {"A"}, 1)
    assert len(cuts) == 1
    assert cuts[0]["type"] == "TIMING_OPTION_NO_GOOD"
    assert cuts[0]["fingerprint"] == list(_option_fp("A", selected["A"]))
    assert cuts[0]["k"] == 2


def test_worker_extension_creates_envelope_cut():
    selected = {"A": {"line_id": 4, "machine_count": 1,
                       "start_seconds": 100, "end_seconds": 200}}
    diag = {"machine_failures": [], "worker_failures": [],
            "envelope_deltas": {"A": {"preview_end_seconds": 200,
                                        "realized_end_seconds": 240,
                                        "extension_seconds": 40}}}
    cuts = _cuts_from_failure(diag, selected, {"A"}, 2)
    assert cuts and cuts[0]["type"] == "WORKER_ENVELOPE_TIGHTENING"
    assert cuts[0]["evidence"]["extension_seconds"] == 40


def test_duplicate_feedback_witnesses_are_deduplicated():
    selected = {"A": {"line_id": 4, "machine_count": 1,
                       "start_seconds": 100, "end_seconds": 200}}
    diag = {"machine_failures": [
                {"operation_key": "A", "reason": "outside machine boundary"},
                {"operation_key": "A", "reason": "outside machine boundary"}],
            "worker_failures": [], "envelope_deltas": {}}
    assert len(_cuts_from_failure(diag, selected, {"A"}, 1)) == 1
