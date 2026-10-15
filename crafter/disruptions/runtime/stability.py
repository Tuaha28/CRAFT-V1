"""Schedule churn and match-up metrics."""
from __future__ import annotations


def stability_metrics(baseline, candidate, *, future_keys=()) -> dict:
    old = {x.assignment.operation_key: x for x in baseline.operations}
    new = {x.assignment.operation_key: x for x in candidate.operations}
    keys = set(old) & set(new)
    changed = []
    start_shifts = []
    line_changes = machine_changes = worker_changes = k_changes = 0
    for key in sorted(keys):
        a, b = old[key], new[key]
        aa, bb = a.assignment, b.assignment
        shift = int(bb.start_seconds) - int(aa.start_seconds)
        if shift:
            start_shifts.append(abs(shift))
        if (aa.start_seconds, aa.end_seconds, aa.line_id, aa.machine_count,
                tuple(aa.machine_ids), tuple(a.employee_ids)) != (
                bb.start_seconds, bb.end_seconds, bb.line_id, bb.machine_count,
                tuple(bb.machine_ids), tuple(b.employee_ids)):
            changed.append(key)
        line_changes += int(aa.line_id != bb.line_id)
        k_changes += int(aa.machine_count != bb.machine_count)
        machine_changes += int(tuple(aa.machine_ids) != tuple(bb.machine_ids))
        worker_changes += int(tuple(a.employee_ids) != tuple(b.employee_ids))
    future = set(str(x) for x in future_keys)
    return {
        "operations_changed": len(changed), "changed_operation_keys": changed,
        "total_absolute_start_shift": sum(start_shifts),
        "maximum_start_shift": max(start_shifts, default=0),
        "line_changes": line_changes, "k_changes": k_changes,
        "machine_changes": machine_changes, "worker_changes": worker_changes,
        "repair_scope_ratio": len(set(changed) & future) / max(1, len(future)),
    }
