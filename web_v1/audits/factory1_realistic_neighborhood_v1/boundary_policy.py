"""TEST-only contract for fixed schedules outside a bounded CRAFT neighborhood.

Unstarted operations have no future reservation. An approved outside schedule,
when one exists, must be a versioned snapshot from the application-owned
governance store. The snapshot is never inferred from source planned lots.
"""
from __future__ import annotations

from hashlib import sha256
import json


def assignment_hash(assignments: list[dict]) -> str:
    return sha256(json.dumps(assignments, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def check_boundary(boundary: dict | None, *, source_sha256: str,
                   selected_keys: set[str], known_keys: set[str]) -> tuple[str, ...]:
    if boundary is None:
        return ()  # No approved outside schedule was supplied; not closure proof.
    errors = []
    if (boundary.get("classification") != "APPROVED_TEST_SCHEDULE_SNAPSHOT" or
            boundary.get("status") != "ACTIVE" or
            not boundary.get("active_version_id") or
            boundary.get("source_sha256") != source_sha256):
        errors.append("OUTSIDE_APPROVAL_PROVENANCE")
    assignments = boundary.get("assignments")
    if not isinstance(assignments, list) or not assignments or (boundary.get("assignments_sha256") !=
            assignment_hash(assignments)):
        return tuple(errors + ["OUTSIDE_APPROVAL_FINGERPRINT"])
    seen = set()
    for row in assignments:
        key = row.get("operation_key")
        if key in seen or key in selected_keys or key not in known_keys:
            errors.append(f"OUTSIDE_APPROVAL_IDENTITY:{key}")
        seen.add(key)
        if (not isinstance(row.get("machine_id"), int) or
                not isinstance(row.get("start_seconds"), int) or
                not isinstance(row.get("end_seconds"), int) or
                row["start_seconds"] < 0 or row["end_seconds"] <= row["start_seconds"]):
            errors.append(f"OUTSIDE_APPROVAL_INTERVAL:{key}")
            continue
        for segment in row.get("worker_segments", ()):
            if (len(segment) != 3 or not isinstance(segment[0], int) or
                    not isinstance(segment[1], int) or not isinstance(segment[2], int) or
                    segment[1] < row["start_seconds"] or segment[2] > row["end_seconds"] or
                    segment[2] <= segment[1]):
                errors.append(f"OUTSIDE_APPROVAL_WORKER_SEGMENT:{key}")
    return tuple(sorted(set(errors)))
