from pathlib import Path
import json


def test_incumbent_audit_reports_exact_mode_parity_failure_without_cp_fallback():
    path = Path("outputs/crafter/e1_7_incumbent_feasibility_audit_final")
    result = json.loads((path / "root_cause.json").read_text())
    assert result["decision"] == "E1_7_INCUMBENT_MODE_PARITY_FAILURE"
    assert result["root_cause"] == "INCUMBENT_MODE_NOT_EXACT"
    cells = json.loads((path / "six_cell_status.json").read_text())
    assert len(cells) == 6
    assert all(cell["incumbent_only_status"] == "NOT_RUN" for cell in cells)


def test_incumbent_audit_detects_productive_and_pause_field_drift():
    path = Path("outputs/crafter/e1_7_incumbent_feasibility_audit_final")
    report = json.loads((path / "incumbent_mode_field_parity.json").read_text())
    assert len(report) == 6
    mismatches = {
        field
        for cell in report
        for operation in cell["fields"]
        for item in operation["mismatched_fields"]
        for field in [item["field"]]
    }
    assert "productive_seconds" in mismatches
    assert "pause_seconds" in mismatches


def test_incumbent_audit_manifest_is_present():
    path = Path("outputs/crafter/e1_7_incumbent_feasibility_audit_final")
    manifest = json.loads((path / "artifact_hash_manifest.json").read_text())
    assert manifest["decision"] == "E1_7_INCUMBENT_MODE_PARITY_FAILURE"
    assert len(manifest["files"]) >= 19
