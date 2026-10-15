"""Audit sealed captures only. Never imports a database client or Django."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "web_v1/backend"))
from integrations.craft.source_readiness import audit_capture


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    capture_dir = ROOT / "web_v1/audits/execution_boundary_v1"
    capture = capture_dir / "factory1_source.json"
    manifest = json.loads((capture_dir / "artifact_hash_manifest.json").read_text())
    expected = manifest["factory1_source.json"]
    historical_path = ROOT / "web_v1/audits/production_platform_v2/database_and_boundary_evidence.json"
    schema_path = ROOT / "outputs/database_audit/schema_and_fk_audit.json"
    before = {str(path.relative_to(ROOT)): sha256(path)
              for path in (capture, historical_path, schema_path)}
    report = audit_capture(capture, expected_sha256=expected)
    historical = json.loads(historical_path.read_text())
    schema = json.loads(historical_path.read_text())["schema"]
    tables = sorted({row["TABLE_NAME"] for row in schema})
    crew_columns = [row for row in schema if row["TABLE_NAME"] == "routing_operations"
                    and ("crew" in row["COLUMN_NAME"].lower() or "worker" in row["COLUMN_NAME"].lower())]
    after = {str(path.relative_to(ROOT)): sha256(path)
             for path in (capture, historical_path, schema_path)}
    if before != after:
        raise RuntimeError("Protected offline evidence changed during audit")
    summary = {key: value for key, value in report.items() if key != "decisions"}
    summary.update({
        "extraction_mode": "OFFLINE_PINNED_CAPTURE_ONLY",
        "live_database_connection_attempted": False,
        "live_permission_verification": "BLOCKED_NO_CONFIGURED_SELECT_ONLY_ACCOUNT",
        "legacy_database_write_path_invoked": False,
        "django_database_access": False,
        "protected_file_hashes_before": before,
        "protected_file_hashes_after": after,
        "historical_six_factory_counts_not_current_readiness": historical["factory_counts"],
        "historical_schema_audit_sha256": sha256(schema_path),
        "historical_schema_tables": tables,
        "routing_crew_columns_in_historical_schema": crew_columns,
        "readiness_scope": "Factory 1 pinned capture only; other five factories lack equivalent pinned captures",
    })
    output = Path(__file__).resolve().parent / "offline_readiness_report.json"
    output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"factory_id": summary["factory_id"], "counts": summary["counts"],
                      "source_hash": summary["raw_file_sha256"],
                      "protected_inputs_unchanged": before == after}, sort_keys=True))


if __name__ == "__main__":
    main()
