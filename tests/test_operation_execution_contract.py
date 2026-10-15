"""Adversarial execution-contract checks using the six pinned TEST captures."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from crafter.execution_contract import OperationExecutionContract, TEST_ASSUMPTION
from utils.machine_taxonomy import machine_type_to_operation_type
from validation.schedule_validator import ScheduleValidationContext, validate_exported_schedule


CAPTURE_DIR = Path(__file__).resolve().parents[1] / "web_v1" / "audits" / "factory_source_integration_v1" / "capture_20261012T223420800478Z"
T0 = datetime(2026, 9, 10, 6, 0)


def _capture_case(factory_id):
    path = CAPTURE_DIR / f"factory{factory_id}_source.json"
    raw_bytes = path.read_bytes()
    source = json.loads(raw_bytes)
    readiness = json.loads((CAPTURE_DIR / f"factory{factory_id}_readiness.json").read_text())
    decisions = {row["operation_key"]: row for row in readiness["decisions"]}
    operation = next(row for row in source["operations"]
                     if decisions.get(f"{row['production_order_id']}_{row['operation_id']}", {}).get("execution_state") == "UNSTARTED")
    key = f"{operation['production_order_id']}_{operation['operation_id']}"
    order = next(row for row in source["orders"]
                 if str(row["production_order_id"]) == str(operation["production_order_id"]))
    capability = machine_type_to_operation_type(operation["required_machine_type"]).lower()
    skills = [row for row in source["resources"]["skills"]
              if row["operation_type"].lower() == capability]
    workers = {int(row["employee_id"]): row for row in source["resources"]["workers"]
               if int(row["factory_id"]) == factory_id and row["is_active"]}
    skill_by_worker = {int(row["employee_id"]): row for row in skills
                       if int(row["employee_id"]) in workers}
    worker_id = next(iter(sorted(skill_by_worker)))
    machines = {int(row["asset_id"]): row for row in source["resources"]["machines"]
                if int(row["factory_id"]) == factory_id and row["current_status"] == "Active"}
    machine_caps = {int(row["asset_id"]): row["operation_type"].lower()
                    for row in source["resources"]["machine_capabilities"]}
    machine_id = next(mid for mid in sorted(machines) if machine_caps.get(mid) == capability)
    line_id = int(machines[machine_id]["line_id"])
    quantity = int(order["target_quantity"])
    efficiency = str(skill_by_worker[worker_id]["efficiency_rating"])
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    contract = OperationExecutionContract(
        operation_key=key, factory_id=factory_id,
        required_worker_count=1, required_machine_count=1,
        required_skills=(capability,), eligible_worker_ids=(str(worker_id),),
        eligible_machine_ids=(machine_id,), eligible_line_ids=(line_id,),
        quantity=quantity, target_quantity=quantity, completed_quantity=0,
        in_flight_quantity=0, quantity_basis="UNSTARTED_REMAINING_UNITS",
        smv_minutes_per_unit=str(operation["standard_allowed_minutes"]),
        setup_seconds=900, evidence_class=TEST_ASSUMPTION,
        source_fingerprint=source_hash,
        snapshot_fingerprint=str(source["dataset"]["dataset_version"]),
        scenario_fingerprint="PINNED_CAPTURE_TEST_ASSUMPTION",
        worker_efficiency_percent=((str(worker_id), efficiency),),
        machine_speedup_factor="1", pairing_policy="EXPLICIT_PAIRS",
        worker_calendar_policy="TEST_ALWAYS_AVAILABLE_FOR_UNIT_ORACLE",
        machine_calendar_policy="TEST_CAPTURE_ACTIVE_STATUS_ONLY",
        allowed_worker_machine_pairs=((str(worker_id), machine_id),),
    )
    productive = contract.expected_productive_seconds((str(worker_id),), (machine_id,))
    end = T0 + timedelta(seconds=900 + productive)
    row = {
        "production_order_id": str(operation["production_order_id"]),
        "operation_id": str(operation["operation_id"]),
        "assigned_line_id": line_id,
        "machines_used": 1, "machine_ids_used": [machine_id],
        "operators_used": 1, "employee_ids_used": [str(worker_id)],
        "quantity_scheduled": quantity,
        "proposed_start_time": T0.isoformat(),
        "base_end_time": end.isoformat(), "cushioned_end_time": end.isoformat(),
        "worker_segments_by_employee": {str(worker_id): [
            {"start": (T0 + timedelta(seconds=900)).isoformat(), "end": end.isoformat()}]},
    }
    expected = {
        "production_order_id": str(operation["production_order_id"]),
        "operation_id": str(operation["operation_id"]),
        "operation_sequence_number": int(operation["operation_sequence_number"]),
        "eligible_line_ids": [line_id],
        "required_machine_type": operation["required_machine_type"],
        "required_operation_type": capability,
        "required_machine_count": 1, "required_workers": 1,
        "setup_minutes": 15, "productive_minutes": productive / 60,
    }
    context = ScheduleValidationContext(
        expected_operations=[expected],
        ownership_by_order={str(operation["production_order_id"]): factory_id},
        line_factory={line_id: factory_id},
        machines={machine_id: {"line_id": line_id, "factory_id": factory_id,
                               "machine_type": operation["required_machine_type"],
                               "capabilities": {capability}}},
        workers={str(worker_id): {"factory_id": factory_id, "skills": {capability}}},
        worker_is_available=lambda _worker, _at: True,
        operation_contracts={key: contract.as_mapping()},
        allow_test_assumptions=True, require_execution_contracts=True,
        worker_interval_is_available=lambda _worker, start, finish: finish > start,
        machine_interval_is_available=lambda _machine, start, finish: finish > start,
        expected_source_fingerprint=source_hash,
        expected_snapshot_fingerprint=str(source["dataset"]["dataset_version"]),
        expected_scenario_fingerprint="PINNED_CAPTURE_TEST_ASSUMPTION",
    )
    return row, context, contract


@pytest.mark.parametrize("factory_id", range(1, 7))
def test_pinned_factory_capture_representative_has_explicit_test_contract(factory_id):
    row, context, contract = _capture_case(factory_id)
    report = validate_exported_schedule([row], context)
    assert report.valid, report.errors
    assert contract.evidence_class == TEST_ASSUMPTION
    assert contract.required_worker_count == 1
    assert contract.required_machine_count == 1


def test_test_assumption_is_rejected_outside_test_validation_context():
    row, context, _ = _capture_case(1)
    context = replace(context, allow_test_assumptions=False)
    report = validate_exported_schedule([row], context)
    assert "non_authoritative_execution_contract" in {e["code"] for e in report.errors}


def test_contract_rejects_wrong_crew_quantity_duration_pair_and_unavailable_time():
    row, context, _ = _capture_case(1)
    bad = deepcopy(row)
    bad["employee_ids_used"].append("999999")
    bad["operators_used"] = 2
    bad["worker_segments_by_employee"]["999999"] = bad["worker_segments_by_employee"][bad["employee_ids_used"][0]]
    bad["quantity_scheduled"] -= 1
    bad["machine_ids_used"] = [999999]
    bad["machines_used"] = 1
    report = validate_exported_schedule([bad], context)
    codes = {error["code"] for error in report.errors}
    assert {"required_worker_count_mismatch", "scheduled_quantity_mismatch",
            "machine_not_in_execution_domain", "resource_pair_or_domain_violation"} <= codes

    row, context, _ = _capture_case(1)
    bad_duration = deepcopy(row)
    worker = bad_duration["employee_ids_used"][0]
    segment = bad_duration["worker_segments_by_employee"][worker][0]
    segment["end"] = (datetime.fromisoformat(segment["end"]) - timedelta(minutes=1)).isoformat()
    bad_duration["base_end_time"] = bad_duration["cushioned_end_time"] = segment["end"]
    report = validate_exported_schedule([bad_duration], context)
    assert "worker_productive_minutes_mismatch" in {e["code"] for e in report.errors}

    missing = deepcopy(row)
    missing["machine_ids_used"] = []
    missing["machines_used"] = 0
    missing["employee_ids_used"] = []
    missing["operators_used"] = 0
    missing["worker_segments_by_employee"] = {}
    report = validate_exported_schedule([missing], context)
    codes = {error["code"] for error in report.errors}
    assert {"missing_machine_assignment", "required_worker_count_mismatch",
            "required_machine_count_mismatch"} <= codes

    row, context, _ = _capture_case(1)
    context = replace(context,
        worker_interval_is_available=lambda _worker, _start, _end: False,
        machine_interval_is_available=lambda _machine, _start, _end: False)
    report = validate_exported_schedule([row], context)
    codes = {error["code"] for error in report.errors}
    assert {"worker_interval_calendar_violation", "machine_calendar_violation"} <= codes


def test_contract_validator_rejects_worker_machine_overlap():
    first, context, contract = _capture_case(3)
    other_order = "outside-order"
    expected = list(context.expected_operations)
    expected.append({**expected[0], "production_order_id": other_order,
                     "operation_id": "2", "operation_sequence_number": 1})
    second = deepcopy(first)
    second["production_order_id"] = other_order
    second["operation_id"] = "2"
    start = T0 + timedelta(minutes=1)
    end = datetime.fromisoformat(first["cushioned_end_time"]) + timedelta(minutes=1)
    second["proposed_start_time"] = start.isoformat()
    second["base_end_time"] = second["cushioned_end_time"] = end.isoformat()
    worker = second["employee_ids_used"][0]
    second["worker_segments_by_employee"][worker] = [{
        "start": (start + timedelta(seconds=900)).isoformat(), "end": end.isoformat()}]
    second_contract = replace(contract, operation_key=f"{other_order}_2")
    context = replace(context,
        expected_operations=expected,
        ownership_by_order={**context.ownership_by_order, other_order: contract.factory_id},
        operation_contracts={contract.operation_key: contract,
                             second_contract.operation_key: second_contract})
    report = validate_exported_schedule([first, second], context)
    codes = {error["code"] for error in report.errors}
    assert {"worker_overlap", "machine_overlap"} <= codes


def test_contract_validator_keeps_whole_operation_precedence():
    first, context, contract = _capture_case(4)
    expected = list(context.expected_operations)
    expected.append({**expected[0], "operation_id": "next",
                     "operation_sequence_number": 2})
    second = deepcopy(first)
    second["operation_id"] = "next"
    second_start = T0 + timedelta(minutes=1)
    duration = contract.expected_productive_seconds(
        first["employee_ids_used"], first["machine_ids_used"])
    second_end = second_start + timedelta(seconds=900 + duration)
    second["proposed_start_time"] = second_start.isoformat()
    second["base_end_time"] = second["cushioned_end_time"] = second_end.isoformat()
    worker = second["employee_ids_used"][0]
    second["worker_segments_by_employee"][worker] = [{
        "start": (second_start + timedelta(seconds=900)).isoformat(),
        "end": second_end.isoformat()}]
    second_key = f"{contract.operation_key.split('_')[0]}_next"
    second_contract = replace(contract, operation_key=second_key)
    context = replace(context,
        expected_operations=expected,
        operation_contracts={contract.operation_key: contract,
                             second_key: second_contract})
    report = validate_exported_schedule([first, second], context)
    assert "precedence_violation" in {error["code"] for error in report.errors}


def test_contract_rejects_quantity_ledger_and_missing_contract():
    row, context, contract = _capture_case(2)
    payload = contract.as_mapping()
    payload["target_quantity"] += 1
    context = replace(context, operation_contracts={contract.operation_key: payload})
    report = validate_exported_schedule([row], context)
    assert "invalid_execution_contract" in {e["code"] for e in report.errors}

    context = replace(context, operation_contracts={})
    report = validate_exported_schedule([row], context)
    assert "execution_contract_missing" in {e["code"] for e in report.errors}


def test_contract_rejects_source_snapshot_or_scenario_fingerprint_mismatch():
    row, context, contract = _capture_case(5)
    corrupted = replace(contract, scenario_fingerprint="different-scenario")
    context = replace(context, operation_contracts={contract.operation_key: corrupted})
    report = validate_exported_schedule([row], context)
    assert "execution_contract_provenance_mismatch" in {e["code"] for e in report.errors}


def test_multi_resource_duration_requires_explicit_crew_and_machine_productivity():
    base = _capture_case(1)[2]
    contract = replace(
        base,
        required_worker_count=2,
        required_machine_count=2,
        required_skills=("sew", "press"),
        eligible_worker_ids=("w1", "w2"),
        eligible_machine_ids=(101, 102),
        worker_efficiency_percent=(("w1", "90"), ("w2", "80")),
        crew_efficiency_percent=(("w1|w2", "80"),),
        allowed_worker_machine_pairs=(("w1", 101), ("w2", 102)),
        machine_speedup_factor="1",
        quantity=1,
        target_quantity=1,
        completed_quantity=0,
        in_flight_quantity=0,
        smv_minutes_per_unit="1",
    )
    # Two machines do not automatically halve duration: their declared
    # speedup is 1.0, and crew productivity is an explicit rule.
    assert contract.expected_productive_seconds(("w1", "w2"), (101, 102)) == 75
    assert replace(contract, machine_speedup_factor="2").expected_productive_seconds(
        ("w1", "w2"), (101, 102)) == 38
    with pytest.raises(ValueError, match="MISSING_AUTHORIZED_CREW_EFFICIENCY_RULE"):
        replace(contract, crew_efficiency_percent=()).expected_productive_seconds(
            ("w1", "w2"), (101, 102))
    with pytest.raises(ValueError, match="RESOURCE_PAIR_NOT_ALLOWED"):
        replace(contract, allowed_worker_machine_pairs=(("w1", 101), ("w2", 101))).expected_productive_seconds(
            ("w1", "w2"), (101, 102))


def test_contract_skill_slots_allow_a_heterogeneous_crew():
    row, context, base = _capture_case(6)
    lead = row["employee_ids_used"][0]
    helper = "helper-test-only"
    contract = replace(
        base, required_worker_count=2,
        required_skills=(base.required_skills[0], "helper"),
        eligible_worker_ids=(lead, helper),
        worker_efficiency_percent=((lead, "100"), (helper, "100")),
        crew_efficiency_percent=(("|".join(sorted((lead, helper))), "100"),),
        pairing_policy="ELIGIBLE_CARTESIAN_BUNDLE",
    )
    productive = contract.expected_productive_seconds((lead, helper), row["machine_ids_used"])
    end = T0 + timedelta(seconds=contract.setup_seconds + productive)
    row["employee_ids_used"] = [lead, helper]
    row["operators_used"] = 2
    row["base_end_time"] = row["cushioned_end_time"] = end.isoformat()
    segment = {"start": (T0 + timedelta(seconds=contract.setup_seconds)).isoformat(),
               "end": end.isoformat()}
    row["worker_segments_by_employee"] = {lead: [segment], helper: [segment]}
    expected = [dict(context.expected_operations[0], required_workers=2,
                      setup_minutes=contract.setup_seconds / 60,
                      productive_minutes=productive / 60)]
    workers = dict(context.workers)
    workers[helper] = {"factory_id": contract.factory_id, "skills": {"helper"}}
    context = replace(context, expected_operations=expected, workers=workers,
                      operation_contracts={contract.operation_key: contract})
    report = validate_exported_schedule([row], context)
    assert report.valid, report.errors

