"""Independent validation of exported schedules.

Uses plain records, not the GA decoder, selectors, allocator, or calendar
walker. Intervals are half-open [start,end). Segmented worker execution needs
explicit worker_segments_by_employee; an operation span is not enough.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional


def _norm(v):
    return "" if v is None else str(v)


def _dt(v):
    if isinstance(v, datetime):
        return v
    if v is None:
        raise ValueError("missing timestamp")
    return datetime.fromisoformat(str(v))


def _ids(v):
    if v is None:
        return []
    return [v] if isinstance(v, (str, bytes)) else list(v)


def _key(r):
    return (_norm(r.get("production_order_id")), _norm(r.get("operation_id")))


def _overlaps(a, b, c, d, tolerance):
    return a.timestamp() + tolerance < d.timestamp() and c.timestamp() + tolerance < b.timestamp()


def _issue(out, code, message, key=None, resource=None):
    row = {"code": code, "message": message}
    if key is not None:
        row["operation_key"] = key
    if resource is not None:
        row["resource_id"] = _norm(resource)
    out.append(row)


@dataclass(frozen=True)
class ScheduleValidationContext:
    expected_operations: Iterable[Mapping[str, Any]]
    ownership_by_order: Mapping[Any, Any]
    line_factory: Mapping[Any, Any]
    machines: Mapping[Any, Mapping[str, Any]]
    workers: Mapping[Any, Mapping[str, Any]]
    committed_machine_intervals: Iterable[Mapping[str, Any]] = field(default_factory=tuple)
    committed_worker_intervals: Iterable[Mapping[str, Any]] = field(default_factory=tuple)
    worker_is_available: Optional[Callable[[str, datetime], bool]] = None
    require_worker_segments: bool = True
    tolerance_seconds: float = 1e-6
    operation_contracts: Mapping[str, Any] = field(default_factory=dict)
    allow_test_assumptions: bool = False
    require_execution_contracts: bool = False
    worker_interval_is_available: Optional[Callable[[str, datetime, datetime], bool]] = None
    machine_interval_is_available: Optional[Callable[[str, datetime, datetime], bool]] = None
    expected_source_fingerprint: Optional[str] = None
    expected_snapshot_fingerprint: Optional[str] = None
    expected_scenario_fingerprint: Optional[str] = None


@dataclass
class ScheduleValidationReport:
    valid: bool
    errors: list
    warnings: list
    counts: dict
    reconstructed: dict

    def as_dict(self):
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings,
                "counts": self.counts, "reconstructed": self.reconstructed}


def validate_exported_schedule(schedule_rows, context):
    """Validate an export independently of production scheduling helpers."""
    errors, warnings, expected, scheduled = [], [], {}, {}
    exp_dups = sched_dups = 0
    for rec in context.expected_operations:
        key = _key(rec)
        if not all(key):
            _issue(errors, "invalid_expected_identity", "Expected operation lacks identity")
        elif key in expected:
            exp_dups += 1
            _issue(errors, "duplicate_expected_operation", "Expected operation appears twice", key)
        else:
            expected[key] = rec
    for rec in schedule_rows:
        key = _key(rec)
        if not all(key):
            _issue(errors, "invalid_schedule_identity", "Schedule row lacks identity")
        elif key in scheduled:
            sched_dups += 1
            _issue(errors, "duplicate_scheduled_operation", "Operation is scheduled twice", key)
        else:
            scheduled[key] = rec

    missing = sorted(set(expected) - set(scheduled))
    unexpected = sorted(set(scheduled) - set(expected))
    for key in missing:
        _issue(errors, "missing_operation", "Expected operation is unscheduled", key)
    for key in unexpected:
        _issue(errors, "unexpected_operation", "Unexpected operation is scheduled", key)

    machine_intervals, worker_intervals, parsed = {}, {}, {}

    def add_committed(source, target, kind):
        for rec in source:
            rid = _norm(rec.get("resource_id", rec.get("asset_id", rec.get("employee_id"))))
            try:
                start, end = _dt(rec.get("start")), _dt(rec.get("end"))
            except (TypeError, ValueError) as exc:
                _issue(errors, "invalid_committed_"+kind+"_interval", str(exc), resource=rid)
                continue
            if end <= start:
                _issue(errors, "invalid_committed_"+kind+"_interval", "end <= start", resource=rid)
            else:
                target.setdefault(rid, []).append((start, end, rec.get("operation_key", "committed")))

    add_committed(context.committed_machine_intervals, machine_intervals, "machine")
    add_committed(context.committed_worker_intervals, worker_intervals, "worker")

    for key in sorted(set(expected) & set(scheduled)):
        exp, row = expected[key], scheduled[key]
        operation_key = f"{key[0]}_{key[1]}"
        contract = context.operation_contracts.get(operation_key,
                    context.operation_contracts.get(key))
        if contract is None and context.require_execution_contracts:
            _issue(errors, "execution_contract_missing",
                   "Strict validation requires a versioned operation contract", key)
        if contract is not None:
            from crafter.execution_contract import OperationExecutionContract, AUTHORITATIVE
            try:
                contract = (contract if isinstance(contract, OperationExecutionContract)
                            else OperationExecutionContract.from_mapping(contract))
            except (TypeError, ValueError, KeyError) as exc:
                _issue(errors, "invalid_execution_contract", str(exc), key)
                contract = None
            if contract is not None:
                if contract.operation_key != operation_key:
                    _issue(errors, "execution_contract_identity_mismatch",
                           "Contract operation identity differs from schedule", key)
                if contract.evidence_class != AUTHORITATIVE and not context.allow_test_assumptions:
                    _issue(errors, "non_authoritative_execution_contract",
                           "TEST assumptions are not admitted by this validation context", key)
                if context.require_execution_contracts:
                    if not contract.source_fingerprint:
                        _issue(errors, "execution_contract_provenance_missing",
                               "Contract lacks a source fingerprint", key)
                    if contract.worker_calendar_policy == "UNSPECIFIED":
                        _issue(errors, "worker_calendar_evidence_missing",
                               "Contract has no worker calendar policy", key)
                    if contract.machine_calendar_policy == "UNSPECIFIED":
                        _issue(errors, "machine_calendar_evidence_missing",
                               "Contract has no machine calendar policy", key)
                    for label, expected_value, actual_value in (
                        ("source", context.expected_source_fingerprint, contract.source_fingerprint),
                        ("snapshot", context.expected_snapshot_fingerprint, contract.snapshot_fingerprint),
                        ("scenario", context.expected_scenario_fingerprint, contract.scenario_fingerprint),
                    ):
                        if expected_value is not None and str(actual_value) != str(expected_value):
                            _issue(errors, "execution_contract_provenance_mismatch",
                                   f"Contract {label} fingerprint differs from validation snapshot", key)
                    if context.worker_interval_is_available is None:
                        _issue(errors, "worker_interval_validator_missing",
                               "Strict contract validation requires exact interval checks", key)
                    if context.machine_interval_is_available is None:
                        _issue(errors, "machine_interval_validator_missing",
                               "Strict contract validation requires machine interval checks", key)
        try:
            start = _dt(row.get("proposed_start_time"))
            base_end = _dt(row.get("base_end_time", row.get("cushioned_end_time")))
            end = _dt(row.get("cushioned_end_time", row.get("base_end_time")))
        except (TypeError, ValueError) as exc:
            _issue(errors, "invalid_operation_timestamp", str(exc), key)
            continue
        if not start <= base_end <= end:
            _issue(errors, "invalid_operation_interval", "Expected start <= base_end <= end", key)
            continue
        parsed[key] = (start, end)

        release = exp.get("release_time", exp.get("release_date"))
        if release is not None:
            try:
                if start.timestamp() + context.tolerance_seconds < _dt(release).timestamp():
                    _issue(errors, "release_violation", "Operation starts before release", key)
            except (TypeError, ValueError) as exc:
                _issue(errors, "invalid_release_time", str(exc), key)

        oid, line = key[0], row.get("assigned_line_id")
        owned = context.ownership_by_order.get(oid, context.ownership_by_order.get(exp.get("production_order_id")))
        line_factory = context.line_factory.get(line, context.line_factory.get(_norm(line)))
        if owned is None:
            _issue(errors, "missing_factory_ownership", "Order has no factory", key)
        elif _norm(line_factory) != _norm(owned):
            _issue(errors, "line_factory_mismatch", "Line is outside owned factory", key, line)
        eligible = exp.get("eligible_line_ids")
        if eligible is not None and _norm(line) not in {_norm(v) for v in eligible}:
            _issue(errors, "ineligible_line", "Line is not eligible", key, line)

        mids = _ids(row.get("machine_ids_used"))
        if len({_norm(v) for v in mids}) != len(mids):
            _issue(errors, "duplicate_machine_within_operation", "Machine repeated", key)
        if row.get("machines_used") is not None and int(row["machines_used"]) != len(mids):
            _issue(errors, "machine_count_mismatch", "Machine count disagrees with IDs", key)
        if exp.get("required_machine_count", 1) > 0 and not mids:
            _issue(errors, "missing_machine_assignment", "Positive work has no machine", key)
        req_type = _norm(exp.get("required_machine_type")).strip().lower()
        for mid in mids:
            machine = context.machines.get(mid, context.machines.get(_norm(mid)))
            if machine is None:
                _issue(errors, "unknown_machine", "Unknown machine", key, mid)
                continue
            if _norm(machine.get("line_id")) != _norm(line):
                _issue(errors, "machine_line_mismatch", "Machine is not on line", key, mid)
            if owned is not None and _norm(machine.get("factory_id")) != _norm(owned):
                _issue(errors, "machine_factory_mismatch", "Machine is outside factory", key, mid)
            caps = {_norm(v).strip().lower() for v in machine.get("capabilities", ())}
            mtype = _norm(machine.get("machine_type")).strip().lower()
            if req_type and req_type not in caps and req_type != mtype:
                _issue(errors, "machine_capability_mismatch", "Machine lacks capability", key, mid)
            machine_intervals.setdefault(_norm(mid), []).append((start, end, key))

        eids = [_norm(v) for v in _ids(row.get("employee_ids_used"))]
        if len(set(eids)) != len(eids):
            _issue(errors, "duplicate_worker_within_operation", "Worker repeated", key)
        if row.get("operators_used") is not None and int(row["operators_used"]) != len(eids):
            _issue(errors, "worker_count_mismatch", "Worker count disagrees with IDs", key)
        if exp.get("required_workers") is not None and len(eids) < int(exp["required_workers"]):
            _issue(errors, "worker_shortage", "Crew is smaller than required", key)
        req_skill = _norm(exp.get("required_operation_type", exp.get("required_skill"))).strip().lower()
        segments_map = row.get("worker_segments_by_employee")
        if eids and context.require_worker_segments and not isinstance(segments_map, Mapping):
            _issue(errors, "worker_segments_missing", "Segmented execution needs worker segments", key)
            segments_map = {}
        elif not isinstance(segments_map, Mapping):
            segments_map = {}

        setup = float(exp.get("setup_minutes", 0.0) or 0.0)
        required_work = exp.get("productive_minutes")
        if contract is not None:
            if owned is None or contract.factory_id != int(owned):
                _issue(errors, "execution_contract_factory_mismatch",
                       "Contract factory differs from order ownership", key)
            # Adaptive contracts allow any declared crew size, with one
            # machine per worker; fixed contracts keep their single size.
            crew_sizes = getattr(contract, "crew_sizes", (contract.required_worker_count,))
            adaptive = bool(getattr(contract, "allowed_crew_sizes", ()))
            if len(eids) not in crew_sizes:
                _issue(errors, "required_worker_count_mismatch",
                       "Assignment does not match contract crew size", key)
            if len(mids) != (len(eids) if adaptive else contract.required_machine_count):
                _issue(errors, "required_machine_count_mismatch",
                       "Assignment does not match contract machine count", key)
            if _norm(line) not in {_norm(v) for v in contract.eligible_line_ids}:
                _issue(errors, "execution_contract_line_ineligible",
                       "Assigned line is not contract-eligible", key, line)
            if row.get("quantity_scheduled") is None:
                _issue(errors, "scheduled_quantity_missing",
                       "Contract validation requires scheduled quantity", key)
            elif int(row["quantity_scheduled"]) != int(contract.quantity):
                _issue(errors, "scheduled_quantity_mismatch",
                       "Scheduled quantity differs from execution contract", key)
            if contract.pairing_policy == "EXPLICIT_PAIRS":
                try:
                    contract.expected_productive_seconds(eids, mids)
                except ValueError as exc:
                    _issue(errors, "resource_pair_or_domain_violation", str(exc), key)
            worker_skills_by_id = {}
            for eid in eids:
                if eid not in set(contract.eligible_worker_ids):
                    _issue(errors, "worker_not_in_execution_domain",
                           "Worker is outside the versioned operation domain", key, eid)
                worker = context.workers.get(int(eid) if eid.isdigit() and int(eid) in context.workers else eid)
                worker_skills = {_norm(v).strip().lower() for v in (worker or {}).get("skills", ())}
                worker_skills_by_id[eid] = worker_skills
            if eids and len(eids) in crew_sizes:
                required_skills = (contract.skills_for(len(eids)) if adaptive
                                   else contract.required_skills)
                # Match crew members to required skill slots; checking that
                # every worker has any one of the skills can falsely accept
                # crews that leave a specialist slot unfilled.
                slots = sorted(range(len(required_skills)),
                               key=lambda i: sum(
                                   _norm(required_skills[i]).strip().lower() in skills
                                   for skills in worker_skills_by_id.values()))
                matched = {}
                def assign_skill(slot, seen):
                    wanted = _norm(required_skills[slot]).strip().lower()
                    for worker_id in sorted(worker_skills_by_id):
                        if worker_id in seen or wanted not in worker_skills_by_id[worker_id]:
                            continue
                        seen.add(worker_id)
                        previous = matched.get(worker_id)
                        if previous is None or assign_skill(previous, seen):
                            matched[worker_id] = slot
                            return True
                    return False
                if not all(assign_skill(slot, set()) for slot in slots):
                    _issue(errors, "contract_worker_skill_mismatch",
                           "Crew cannot fill every required skill slot", key)
            for mid in mids:
                if int(mid) not in set(contract.eligible_machine_ids):
                    _issue(errors, "machine_not_in_execution_domain",
                           "Machine is outside the versioned operation domain", key, mid)
            try:
                expected_seconds = contract.expected_productive_seconds(eids, mids)
                required_work = expected_seconds / 60.0
                contract_setup = contract.setup_seconds / 60.0
                if abs(setup - contract_setup) > max(1/60, context.tolerance_seconds/60):
                    _issue(errors, "setup_duration_mismatch",
                           "Setup duration differs from execution contract", key)
            except (ValueError, ArithmeticError) as exc:
                _issue(errors, "duration_contract_unresolved", str(exc), key)
            if context.require_execution_contracts and contract.worker_calendar_policy != "UNSPECIFIED" and context.worker_interval_is_available is None:
                _issue(errors, "worker_calendar_evidence_unverified",
                       "Worker calendar could not be checked exactly", key)
        for eid in eids:
            worker_key = int(eid) if eid.isdigit() and int(eid) in context.workers else eid
            worker = context.workers.get(worker_key)
            if worker is None:
                _issue(errors, "unknown_worker", "Unknown worker", key, eid)
            else:
                if owned is not None and _norm(worker.get("factory_id")) != _norm(owned):
                    _issue(errors, "worker_factory_mismatch", "Worker is outside factory", key, eid)
                skills = {_norm(v).strip().lower() for v in worker.get("skills", ())}
                # Versioned contracts can define heterogeneous skill slots
                # (for example operator + helper).  The slot matching above
                # is authoritative in that case; imposing the legacy single
                # operation-capability on every crew member would reject a
                # valid mixed-skill crew.
                if contract is None and req_skill and req_skill not in skills:
                    _issue(errors, "worker_skill_mismatch", "Worker lacks skill", key, eid)
            segment_key = int(eid) if eid.isdigit() and int(eid) in segments_map else eid
            segments = segments_map.get(segment_key, ())
            if not segments and context.require_worker_segments:
                _issue(
                    errors, "worker_segments_missing",
                    "Assigned worker lacks productive segment evidence",
                    key, eid,
                )
            if not segments and not context.require_worker_segments:
                segments = [(start, end)]
            total, previous_end = 0.0, None
            for seg in segments:
                ss, ee = ((seg.get("start"), seg.get("end")) if isinstance(seg, Mapping) else seg)
                try:
                    ss, ee = _dt(ss), _dt(ee)
                except (TypeError, ValueError) as exc:
                    _issue(errors, "invalid_worker_segment", str(exc), key, eid)
                    continue
                if ee <= ss or ss < start or ee > end:
                    _issue(errors, "invalid_worker_segment", "Segment outside operation span", key, eid)
                    continue
                if previous_end is not None and ss < previous_end:
                    _issue(errors, "worker_segment_self_overlap", "Segments overlap", key, eid)
                if setup and ss.timestamp() + context.tolerance_seconds < start.timestamp() + setup*60:
                    _issue(errors, "worker_segment_during_setup", "Productive segment starts during setup", key, eid)
                if context.worker_is_available is not None:
                    probe = ss
                    while probe < ee:
                        if not context.worker_is_available(eid, probe):
                            _issue(errors, "worker_calendar_violation", "Segment includes unavailable time", key, eid)
                            break
                        probe += timedelta(hours=1)
                    if not context.worker_is_available(eid, ee-timedelta(microseconds=1)):
                        _issue(errors, "worker_calendar_violation", "Segment ends in unavailable time", key, eid)
                if context.worker_interval_is_available is not None and not context.worker_interval_is_available(eid, ss, ee):
                    _issue(errors, "worker_interval_calendar_violation",
                           "Worker interval intersects unavailable time", key, eid)
                total += (ee-ss).total_seconds()/60
                worker_intervals.setdefault(eid, []).append((ss, ee, key))
                previous_end = ee
            if required_work is not None and segments:
                if abs(total-float(required_work)) > max(1/60, context.tolerance_seconds/60):
                    _issue(errors, "worker_productive_minutes_mismatch", "Segments do not cover work", key, eid)

        assigned_segment_ends = []
        for eid in eids:
            segment_key = int(eid) if eid.isdigit() and int(eid) in segments_map else eid
            worker_segments = segments_map.get(segment_key, ())
            for segment in worker_segments:
                raw_end = segment.get("end") if isinstance(segment, Mapping) else segment[1]
                try:
                    assigned_segment_ends.append(_dt(raw_end))
                except (TypeError, ValueError):
                    pass
        if assigned_segment_ends:
            worker_completion = max(assigned_segment_ends)
            if abs((worker_completion - end).total_seconds()) > context.tolerance_seconds:
                _issue(
                    errors,
                    "operation_completion_worker_mismatch",
                    "Operation completion does not equal the last assigned worker segment",
                    key,
                )
        if contract is not None and context.machine_interval_is_available is not None:
            for mid in mids:
                if not context.machine_interval_is_available(_norm(mid), start, end):
                    _issue(errors, "machine_calendar_violation",
                           "Machine is unavailable during the operation reservation", key, mid)

    for kind, resources in (("machine", machine_intervals), ("worker", worker_intervals)):
        for rid, intervals in resources.items():
            intervals.sort(key=lambda v: (v[0], v[1]))
            for left, right in zip(intervals, intervals[1:]):
                if _overlaps(left[0], left[1], right[0], right[1], context.tolerance_seconds):
                    _issue(errors, kind+"_overlap", kind.title()+" intervals overlap", right[2], rid)

    by_order = {}
    for key, exp in expected.items():
        if key not in parsed:
            continue
        seq = exp.get("operation_sequence_number")
        if seq is None:
            _issue(errors, "missing_routing_sequence", "Missing routing sequence", key)
        else:
            by_order.setdefault(key[0], []).append((float(seq), key))
    completions, tardiness = {}, {}
    for oid, chain in by_order.items():
        chain.sort(key=lambda v: v[0])
        if len({seq for seq, _ in chain}) != len(chain):
            _issue(errors, "duplicate_routing_sequence", "Duplicate routing sequence", oid)
        for (_, pred), (_, succ) in zip(chain, chain[1:]):
            if parsed[succ][0].timestamp()+context.tolerance_seconds < parsed[pred][1].timestamp():
                _issue(errors, "precedence_violation", "Successor starts before predecessor ends", succ)
        final_key = chain[-1][1]
        completions[oid] = parsed[final_key][1]
        deadline = next((expected[k].get("deadline", expected[k].get("delivery_deadline"))
                         for _, k in reversed(chain)
                         if expected[k].get("deadline", expected[k].get("delivery_deadline")) is not None), None)
        if deadline is not None:
            tardiness[oid] = max(0.0, (completions[oid]-_dt(deadline)).total_seconds()/3600)

    counts = {
        "expected_operations": len(expected), "scheduled_operations": len(scheduled),
        "matched_operations": len(set(expected)&set(scheduled)),
        "missing_operations": len(missing), "unexpected_operations": len(unexpected),
        "duplicate_expected_operations": exp_dups,
        "duplicate_scheduled_operations": sched_dups,
        "errors": len(errors), "warnings": len(warnings),
    }
    rebuilt = {
        "order_completion": {k: v.isoformat() for k, v in completions.items()},
        "tardiness_hours": tardiness,
        "orders_with_reconstructed_completion": len(completions),
        "late_orders": sum(v > 0 for v in tardiness.values()),
    }
    return ScheduleValidationReport(not errors, errors, warnings, counts, rebuilt)
