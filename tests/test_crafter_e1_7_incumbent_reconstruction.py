import json
from pathlib import Path

from crafter.authoritative_semantics import authoritative_duration_contract
from crafter.lns_probe.joint_modes import incumbent_prefix_worker_reservations


PARITY = Path("outputs/crafter/e1_7_incumbent_reconstruction_parity_v10")


def test_duration_contract_matches_calendar_ceiling_not_legacy_rounding():
    contract = authoritative_duration_contract(
        {"setup_mins": 15.0, "serial_processing_mins": 51.903544522999994 * 8},
        8,
    )
    assert contract.productive_seconds == 3115
    assert round(contract.productive_minutes * 60.0) == 3114


def test_prefix_reservations_stop_before_target_operation():
    class Fixed:
        employee_id = "fixed"
        start_seconds = 1
        end_seconds = 2

    class Problem:
        origin = __import__("datetime").datetime(2026, 1, 1)
        class Resources:
            fixed_worker_commitments = (Fixed(),)
        resources = Resources()

    rows = (
        {"operation_key": "A", "worker_segments_by_employee": {
            "w": [["2026-01-01T00:00:00", "2026-01-01T00:00:10"]]}},
        {"operation_key": "B", "worker_segments_by_employee": {
            "w": [["2026-01-01T00:00:20", "2026-01-01T00:00:30"]]}},
    )
    index = type("Index", (), {"rows": rows})()
    reservations = incumbent_prefix_worker_reservations(Problem(), index, "B")
    assert len(reservations["w"]) == 1
    assert reservations["w"][0][0].isoformat() == "2026-01-01T00:00:00"
    assert reservations["fixed"][0][0].isoformat() == "2026-01-01T00:00:01"


def test_all_six_cells_have_exact_incumbent_mode_parity():
    summary = json.loads((PARITY / "six_cell_parity_summary.json").read_text())
    assert summary["exact_incumbent_parity"] == "180/180"
    assert all(cell["exact_count"] == 30 for cell in summary["cells"])


def test_replay_has_no_segment_or_end_mismatch():
    rows = json.loads((PARITY / "cr_replay_comparison.json").read_text())
    assert all(row["exact_count"] == 30 for row in rows)
    assert all(not item["replay_mismatches"] for row in rows for item in row["rows"])


def test_incumbent_only_cp_is_feasible_in_all_cells():
    results = json.loads((PARITY / "incumbent_only_cp_results.json").read_text())
    assert len(results) == 6
    assert all(item["status"] in {"OPTIMAL", "FEASIBLE"} for item in results)


def test_duration_trace_exposes_legacy_projection_without_accepting_tolerance():
    trace = json.loads((PARITY / "duration_parity_trace.json").read_text())
    assert len(trace) == 431
    assert any(item["legacy_round_seconds"] != item["authoritative_calendar_required_seconds"]
               for item in trace)
    assert all(item["authoritative_calendar_required_seconds"] == item["mode_productive_seconds"]
               for item in trace)


def test_joint_mode_generation_uses_cr_prefix_only_for_incumbents():
    source = Path("crafter/lns_probe/joint_modes.py").read_text()
    assert "incumbent_prefix_worker_reservations" in source
    assert "if is_incumbent else worker_res" in source
    assert "authoritative_duration_contract" in source
