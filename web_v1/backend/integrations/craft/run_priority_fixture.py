"""Background-only controlled priority solve; no factory database connection."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import pymysql

pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LEGACY_DB_CONNECTION_FORBIDDEN"))

from web_v1.backend.integrations.craft.priority_fixture import ORIGIN, solve


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Expected output and validated request snapshot paths")
    output, snapshot_path = map(Path, sys.argv[1:])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("contract") != "SYNTHETIC_PRIORITY_TWO_ORDER_V1" or snapshot.get("source") != "SYNTHETIC_PRIORITY_FIXTURE":
        raise RuntimeError("UNSUPPORTED_PRIORITY_CONTRACT")
    priority = snapshot.get("priority_state") or {}
    override = priority.get("override")
    started = time.perf_counter()
    if override:
        result = solve(override["tier"], order_id=override["order_id"],
                       target_due_seconds=override["target_due_at"],
                       entered_by=override["entered_by"], override_id=override["override_id"],
                       revision=override["revision"])
    else:
        result = solve()
    result.update({
        "controller_status": result["acceptance"]["status"],
        "manager_overrides": [override] if override else [],
        "ledger": {"source_evidence": {"as_of": ORIGIN.isoformat(),
                                       "classification": "SYNTHETIC_PRIORITY_TEST_NOT_LIVE"}},
        "runtime_seconds": time.perf_counter() - started,
    })
    output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["controller_status"],
                      "order_completion_seconds": result["fixture"]["order_completion_seconds"],
                      "priority_tier": result["priority_tier"]}))


if __name__ == "__main__":
    main()
