import hashlib
import json
from pathlib import Path

from crafter.baselines.fifo_v1 import (
    _next_calendar_boundary,
    build_workload_from_neutral_payload,
    edd_priority_key,
    fifo_priority_key,
    run_edd_schedule,
    run_fifo_schedule,
    run_spt_schedule,
    spt_priority_key,
    validate_fifo_schedule,
)


def payload(operations, *, machines=None, workers=None, commitments=None):
    orders = []
    for order_id in sorted({str(row["order_id"]) for row in operations}):
        orders.append({
            "order_id": order_id, "factory_id": 2, "release_seconds": 0,
            "deadline_seconds": 100000, "priority_weight": 1,
        })
    machines = machines or [
        {"machine_id": i + 1, "factory_id": 2, "line_id": 1,
         "capabilities": ["sew"], "is_usable": True}
        for i in range(max(1, len(operations)))
    ]
    workers = workers or [
        {"worker_id": str(i + 1), "factory_id": 2, "skills": ["sew"],
         "calendar": {"kind": "ROTATING_CREW", "rotation_offset": 0,
                      "rest_day_offset": 0}, "assignable_at_snapshot": True}
        for i in range(max(1, len(operations)))
    ]
    rows = []
    for row in operations:
        rows.append({
            "operation_key": row["operation_key"], "order_id": str(row["order_id"]),
            "operation_id": str(row["operation_key"]).split("_", 1)[1],
            "sequence": row.get("sequence", 1), "required_capability": "sew",
            "eligible_line_ids": row.get("eligible_line_ids", [1]),
            "serial_productive_seconds": row.get("serial_productive_seconds", 60),
            "setup_seconds": row.get("setup_seconds", 0), "min_machine_count": 1,
            "max_machine_count": 1, "eligible_machine_ids": row.get(
                "eligible_machine_ids", [m["machine_id"] for m in machines]),
            "eligible_worker_ids": row.get(
                "eligible_worker_ids", [w["worker_id"] for w in workers]),
            "predecessor_operation_keys": row.get("predecessor_operation_keys", []),
            "successor_operation_keys": row.get("successor_operation_keys", []),
            "execution_state": {"state": "RELEASED_FUTURE_WORK"},
        })
    keys = sorted(row["operation_key"] for row in rows)
    return {
        "contract_version": "NEUTRAL_FACTORY_WORKLOAD_V1", "factory_id": 2,
        "snapshot_as_of_seconds": 0, "evaluation_origin": "2024-01-02T00:00:00",
        "orders": orders, "operations": rows,
        "precedence": [], "lines": [{"line_id": 1, "factory_id": 2, "name": "L1"}],
        "machines": machines, "workers": workers,
        "resource_commitments": commitments or {"machine": [], "worker": []},
        "accounting": {"benchmark_operation_keys": keys, "duplicates": []},
    }


def test_fifo_first_operation_queue_entry_is_release():
    data = payload([{"operation_key": "2_1", "order_id": "2"}])
    data["orders"][0]["release_seconds"] = -50
    result = run_fifo_schedule(build_workload_from_neutral_payload(data))
    assert result["schedule"][0]["queue_entry_time"] == -50


def test_fifo_successor_queue_entry_is_predecessor_completion():
    data = payload([
        {"operation_key": "1_1", "order_id": "1", "sequence": 1,
         "successor_operation_keys": ["1_2"]},
        {"operation_key": "1_2", "order_id": "1", "sequence": 2,
         "predecessor_operation_keys": ["1_1"]},
    ])
    result = run_fifo_schedule(build_workload_from_neutral_payload(data))
    rows = {row["operation_key"]: row for row in result["schedule"]}
    assert rows["1_2"]["queue_entry_time"] == rows["1_1"]["end_seconds"]
    assert rows["1_2"]["start_seconds"] >= rows["1_1"]["end_seconds"]


def test_fifo_tie_breaking_is_deterministic():
    operations = [
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [1],
         "eligible_worker_ids": ["1"], "sequence": 1},
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [2],
         "eligible_worker_ids": ["2"], "sequence": 1},
    ]
    result = run_fifo_schedule(build_workload_from_neutral_payload(payload(operations)))
    assert result["event_log"][0]["scheduled_operations"] == ["1_1", "2_1"]
    assert fifo_priority_key(operations[1], 0) < fifo_priority_key(operations[0], 0)


def test_edd_earliest_due_date_wins_among_ready_operations():
    data = payload([
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [1]},
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [1]},
    ], machines=[{"machine_id": 1, "factory_id": 2, "line_id": 1,
                  "capabilities": ["sew"], "is_usable": True}])
    data["orders"][0]["deadline_seconds"] = 200
    data["orders"][1]["deadline_seconds"] = 100
    result = run_edd_schedule(build_workload_from_neutral_payload(data))
    assert result["policy"] == "EDD"
    assert result["schedule"][0]["operation_key"] == "2_1"


def test_edd_missing_due_date_is_after_valid_due_date():
    data = payload([
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [1]},
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [1]},
    ], machines=[{"machine_id": 1, "factory_id": 2, "line_id": 1,
                  "capabilities": ["sew"], "is_usable": True}])
    data["orders"][0]["deadline_seconds"] = None
    data["orders"][1]["deadline_seconds"] = 100
    result = run_edd_schedule(build_workload_from_neutral_payload(data))
    assert result["schedule"][0]["operation_key"] == "2_1"
    assert edd_priority_key(data["operations"][0], 0, {"1": None, "2": 100}) > \
        edd_priority_key(data["operations"][1], 0, {"1": None, "2": 100})


def test_spt_shorter_intrinsic_processing_time_wins_among_ready_operations():
    data = payload([
        {"operation_key": "1_1", "order_id": "1", "serial_productive_seconds": 200,
         "eligible_machine_ids": [1]},
        {"operation_key": "2_1", "order_id": "2", "serial_productive_seconds": 50,
         "eligible_machine_ids": [1]},
    ], machines=[{"machine_id": 1, "factory_id": 2, "line_id": 1,
                  "capabilities": ["sew"], "is_usable": True}])
    result = run_spt_schedule(build_workload_from_neutral_payload(data))
    assert result["policy"] == "SPT"
    assert result["schedule"][0]["operation_key"] == "2_1"
    assert spt_priority_key(data["operations"][1], 0) < spt_priority_key(data["operations"][0], 0)


def test_spt_priority_uses_intrinsic_duration_not_calendar_or_machine_choice():
    operation = {"operation_key": "1_1", "order_id": "1", "sequence": 1,
                 "serial_productive_seconds": 75}
    same = dict(operation)
    same["eligible_machine_ids"] = [9, 10, 11]
    assert spt_priority_key(operation, 12) == spt_priority_key(same, 12)


def test_independent_operations_start_concurrently_without_global_clock():
    operations = [
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [1],
         "eligible_worker_ids": ["1"]},
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [2],
         "eligible_worker_ids": ["2"]},
    ]
    result = run_fifo_schedule(build_workload_from_neutral_payload(payload(operations)))
    assert result["status"] == "COMPLETE"
    assert {row["start_seconds"] for row in result["schedule"]} == {0}


def test_machine_overlap_is_prevented_and_event_advances():
    machine = [{"machine_id": 1, "factory_id": 2, "line_id": 1,
                "capabilities": ["sew"], "is_usable": True}]
    operations = [
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [1]},
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [1]},
    ]
    result = run_fifo_schedule(build_workload_from_neutral_payload(payload(operations, machines=machine)))
    rows = sorted(result["schedule"], key=lambda row: row["start_seconds"])
    assert rows[1]["start_seconds"] >= rows[0]["end_seconds"] + 1500
    assert any(event["time_seconds"] == rows[0]["end_seconds"] for event in result["event_log"])


def test_worker_segments_never_overlap_and_calendar_is_respected():
    workers = [{"worker_id": "1", "factory_id": 2, "skills": ["sew"],
                "calendar": {"kind": "ROTATING_CREW", "rotation_offset": 0,
                             "rest_day_offset": 0}, "assignable_at_snapshot": True}]
    operations = [
        {"operation_key": "1_1", "order_id": "1", "eligible_machine_ids": [1],
         "eligible_worker_ids": ["1"]},
        {"operation_key": "2_1", "order_id": "2", "eligible_machine_ids": [2],
         "eligible_worker_ids": ["1"]},
    ]
    result = run_fifo_schedule(build_workload_from_neutral_payload(payload(operations, workers=workers)))
    segments = sorted(tuple(segment) for row in result["schedule"] for segment in row["worker_segments"])
    assert all(left[1] <= right[0] for left, right in zip(segments, segments[1:]))
    assert all(segment[0] >= 6 * 3600 for segment in segments)


def test_machine_and_worker_eligibility_are_respected():
    operation = {"operation_key": "1_1", "order_id": "1",
                 "eligible_machine_ids": [2], "eligible_worker_ids": ["2"]}
    machines = [
        {"machine_id": value, "factory_id": 2, "line_id": 1,
         "capabilities": ["sew"], "is_usable": True} for value in (1, 2)]
    workers = [
        {"worker_id": str(value), "factory_id": 2, "skills": ["sew"],
         "calendar": {"kind": "ROTATING_CREW", "rotation_offset": 0,
                      "rest_day_offset": 0}, "assignable_at_snapshot": True}
        for value in (1, 2)]
    result = run_fifo_schedule(build_workload_from_neutral_payload(
        payload([operation], machines=machines, workers=workers)))
    cfg = result["schedule"][0]["resource_configuration"]
    assert cfg["machine_ids"] == (2,) or cfg["machine_ids"] == [2]
    assert cfg["worker_ids"] == ("2",) or cfg["worker_ids"] == ["2"]


def test_next_calendar_event_is_exact_boundary():
    workload = build_workload_from_neutral_payload(payload([
        {"operation_key": "1_1", "order_id": "1"}
    ]))
    assert _next_calendar_boundary(workload.problem.origin, 0) == 6 * 3600


def test_resource_matching_does_not_enumerate_combinations_or_apply_cap():
    workers = [
        {"worker_id": str(i), "factory_id": 2, "skills": ["sew"],
         "calendar": {"kind": "ROTATING_CREW", "rotation_offset": 0,
                      "rest_day_offset": 0}, "assignable_at_snapshot": True}
        for i in range(1, 101)
    ]
    result = run_fifo_schedule(build_workload_from_neutral_payload(payload([
        {"operation_key": "1_1", "order_id": "1",
         "eligible_worker_ids": [str(i) for i in range(1, 101)]}
    ], workers=workers)))
    assert result["status"] == "COMPLETE"
    assert result["allocation_profile"]["worker_combinations_enumerated"] == 0


def test_complete_small_schedule_passes_replay_physical_and_scope():
    workload = build_workload_from_neutral_payload(payload([
        {"operation_key": "1_1", "order_id": "1"}
    ]))
    result = run_fifo_schedule(workload)
    validation = validate_fifo_schedule(workload, result["schedule"])
    assert validation["scope_status"] == "SCOPE_VALID"
    assert validation["replay_status"] == "PASS"
    assert validation["physical_status"] == "VALID"
    assert validation["accepted"]


def test_factory2_neutral_identity_and_evaluation_artifacts_unchanged():
    root = Path("outputs/crafter/evaluation_v2")
    neutral = json.loads((root / "neutral_factory2_state.json").read_text())
    identity = json.loads((root / "neutral_factory2_identity.json").read_text())
    keys = neutral["accounting"]["benchmark_operation_keys"]
    digest = hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()
    assert len(keys) == 172 and identity["identity_match"]
    assert digest == identity["neutral_sha256"]
    assert json.loads((root / "craft_factory2_metrics_v2.json").read_text())["database_writes"] == 0
