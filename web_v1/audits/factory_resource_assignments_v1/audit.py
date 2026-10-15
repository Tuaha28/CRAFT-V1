"""Read-only analysis of hash-pinned six-factory captures. No DB imports."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import heapq
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.machine_taxonomy import machine_type_to_operation_type, normalize
from utils.shift_rotation import batch_available_at, band_covers

CAPTURE = ROOT / 'web_v1/audits/factory_source_integration_v1/capture_20261012T223420800478Z'


def stamp(value):
    return datetime.fromisoformat(value) if value else None


def segments(row, reference=None, planned=False):
    raw = row.get('planned_work_segments' if planned else 'actual_work_segments') or []
    raw = json.loads(raw) if isinstance(raw, str) else raw
    result = []
    for left, right in raw:
        a, b = stamp(left), stamp(right)
        if not a or not b or b <= a:
            raise ValueError('INVALID_SEGMENT')
        if reference is not None:
            b = min(b, reference)
        if a < b:
            result.append((a, b))
    return result


def overlaps(intervals):
    """Half-open interval pair count, O(n log n); at most 12 example pairs."""
    heap, count, examples, peak = [], 0, [], 0
    for a, b, label in sorted(intervals):
        while heap and heap[0][0] <= a:
            heapq.heappop(heap)
        count += len(heap)
        if heap and len(examples) < 12:
            examples.append([heap[0][1], label, a.isoformat()])
        heapq.heappush(heap, (b, label))
        peak = max(peak, len(heap))
    return {'overlap_pairs': count, 'peak_intervals': peak, 'examples': examples}


def simultaneous(intervals):
    """Count distinct simultaneously productive resources, not crew demand."""
    events = []
    for a, b, worker, machine, lot in intervals:
        events.extend([(a, 1, worker, machine, lot), (b, -1, worker, machine, lot)])
    workers, machines, lots = Counter(), Counter(), Counter()
    result = {'workers': 0, 'machines': 0, 'lots': 0}
    for _, delta, worker, machine, lot in sorted(events):
        for counter, key in ((workers, worker), (machines, machine), (lots, lot)):
            counter[key] += delta
            if counter[key] == 0:
                del counter[key]
        result = {key: max(result[key], len(value)) for key, value in
                  (('workers', workers), ('machines', machines), ('lots', lots))}
    return result


def lot_precedence_errors(predecessors, successors, reference=None, planned=False):
    prefix = 'planned' if planned else 'actual'
    prior = {r['lot_index']: r for r in predecessors}
    errors = []
    for row in successors:
        start = stamp(row.get(prefix + '_start_time'))
        if start is None or (reference is not None and start >= reference):
            continue
        pred = prior.get(row['lot_index'])
        end = stamp(pred.get(prefix + '_end_time')) if pred else None
        if end is None or end > start or pred['lot_quantity'] != row['lot_quantity']:
            errors.append(str(row['schedule_item_id']))
    return errors


def band(row):
    def clock(value):
        if isinstance(value, dict):
            return (datetime.min + timedelta(seconds=value['seconds'])).time()
        return datetime.strptime(value, '%H:%M:%S').time()
    return ('captured', clock(row['start_time']), clock(row['end_time']))


def on_model_calendar(a, b, worker, memberships, batches, schedules):
    """Segment check at every possible rotation/static-band transition.

    Calendar semantics are the seeded model; they are not observed attendance.
    """
    member = memberships.get(worker, set())
    rows = schedules.get(worker, [])
    if len(member) > 1 or len(rows) != 1:
        return None
    if member:
        crew = batches[next(iter(member))]
        if not crew['is_active']:
            return None
        predicate = lambda t: batch_available_at(crew['rotation_offset'], crew['rest_day_offset'], t)
        hours = [0, 6, 14, 22]
    else:
        shift = band(rows[0])
        predicate = lambda t: band_covers(shift, t)
        hours = []
    probes = [a, b - timedelta(microseconds=1)]
    day = a.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < b:
        changes = [day + timedelta(hours=h) for h in hours]
        if not member:
            changes += [datetime.combine(day.date(), t) for t in shift[1:]]
            changes.append(day)
        for change in changes:
            if a <= change < b:
                probes.append(change)
        day += timedelta(days=1)
    return all(predicate(t) for t in probes)


def analyze(source, readiness):
    fid, ref = source['factory_id'], stamp(source['reference_time'])
    resources = source['resources']
    workers = {r['employee_id']: r for r in resources['workers']}
    machines = {r['asset_id']: r for r in resources['machines']}
    lines = {r['line_id']: r for r in resources['lines']}
    batches = {r['batch_id']: r for r in resources['batches']}
    orders = {r['production_order_id']: r for r in source['orders']}
    ops = {(r['production_order_id'], r['operation_id']): r for r in source['operations']}
    memberships, schedules, skills, capabilities = (defaultdict(set), defaultdict(list), defaultdict(dict), defaultdict(set))
    for r in resources['batch_members']:
        memberships[r['employee_id']].add(r['batch_id'])
    for r in resources['work_schedules']:
        schedules[r['employee_id']].append(r)
    for r in resources['skills']:
        skills[r['employee_id']][normalize(r['operation_type'])] = float(r['efficiency_rating'])
    for r in resources['machine_capabilities']:
        capabilities[r['asset_id']].add(normalize(r['operation_type']))
    byop, routes = defaultdict(list), defaultdict(list)
    for r in source['lots']:
        byop[(r['production_order_id'], r['operation_id'])].append(r)
    for key, op in ops.items():
        routes[key[0]].append(key)

    issues, examples = Counter(), defaultdict(list)
    def flag(code, value):
        issues[code] += 1
        if len(examples[code]) < 12:
            examples[code].append(value)

    pools = {}
    for machine_type in sorted({op['required_machine_type'] for op in ops.values()}):
        skill = machine_type_to_operation_type(machine_type)
        mids = {mid for mid, machine in machines.items() if machine['factory_id'] == fid
                and machine['line_id'] in lines and lines[machine['line_id']]['is_active']
                and skill in capabilities[mid]}
        wids = {wid for wid, worker in workers.items() if worker['factory_id'] == fid
                and worker['is_active'] and worker['role'] == 'Operator' and skills[wid].get(skill, 0) > 0}
        assigned_lines = {machines[mid]['line_id'] for mid in mids}
        line_wids = {wid for wid in wids if any(b in batches and batches[b]['is_active']
                     and batches[b]['line_id'] in assigned_lines for b in memberships[wid])}
        demand = [op for op in ops.values() if op['required_machine_type'] == machine_type]
        pools[machine_type] = {'skill_category': skill, 'machines': sorted(mids), 'workers': sorted(wids),
            'batch_line_compatible_workers': sorted(line_wids),
            'currently_active_or_idle_machines': sum(machines[m]['current_status'] in ('Active', 'Idle') for m in mids),
            'workers_with_calendar_row': sum(bool(schedules[w]) for w in wids),
            'workers_flagged_leave': sum(any(r['is_on_leave'] for r in schedules[w]) for w in wids),
            'operations': len(demand),
            'nominal_sam_quantity_minutes': round(sum(float(op['standard_allowed_minutes']) * float(orders[op['production_order_id']]['target_quantity']) for op in demand), 3),
            'eligibility_basis': 'CAPTURED_TEST_CAPABILITY_PLUS_CODE_TAXONOMY; NOT_FACTORY_APPROVED_CREW_RULE'}

    occupancy = {name: defaultdict(list) for name in ('worker_span', 'machine_span', 'worker_productive', 'machine_productive', 'planned_worker_span', 'planned_machine_span')}
    historical_rows, future_rows, calendar_pass, calendar_fail, calendar_unknown = 0, 0, 0, 0, 0
    line_assignment_counts = Counter()
    cross_line_assignments = Counter()
    seen_lot_ids = Counter()
    operation_evidence = []
    for key, op in sorted(ops.items()):
        lots = byop[key]
        history, intervals = [], []
        key_string = f'{key[0]}_{key[1]}'
        if sum(int(r['lot_quantity']) for r in lots) != int(orders[key[0]]['target_quantity']):
            flag('LOT_QUANTITY_VS_ORDER_MISMATCH', key_string)
        if float(op['standard_allowed_minutes']) <= 0:
            flag('NONPOSITIVE_SAM', key_string)
        for row in lots:
            lot = str(row['schedule_item_id'])
            seen_lot_ids[lot] += 1
            w, m, line = row['employee_id'], row['asset_id'], row['line_id']
            worker, machine = workers.get(w), machines.get(m)
            skill = machine_type_to_operation_type(op['required_machine_type'])
            if worker is None or worker['factory_id'] != fid:
                flag('WORKER_FACTORY_OR_ID', lot)
            if machine is None or machine['factory_id'] != fid:
                flag('MACHINE_FACTORY_OR_ID', lot)
            if not machine or machine['line_id'] != line or line not in lines:
                flag('MACHINE_LINE_MISMATCH', lot)
            if skill is None or skill not in capabilities[m]:
                flag('MACHINE_CAPABILITY_MISMATCH', lot)
            if skills[w].get(skill, 0) <= 0:
                flag('WORKER_SKILL_MISMATCH', lot)
            if row['batch_id'] is not None and (row['batch_id'] not in memberships[w] or row['batch_id'] not in batches):
                flag('RECORDED_BATCH_MEMBERSHIP_MISMATCH', lot)
            if memberships[w] and not any(b in batches and batches[b]['line_id'] == line for b in memberships[w]):
                # This is a policy question, not an assignment violation: the
                # captured seeder deliberately draws from factory-wide skill pools.
                cross_line_assignments['all_captured'] += 1
                if a := stamp(row['actual_start_time']):
                    if a < ref:
                        cross_line_assignments['historical'] += 1
            a, b = stamp(row['actual_start_time']), stamp(row['actual_end_time'])
            if a is not None and a < ref:
                historical_rows += 1
                history.append(row)
                end = min(b, ref) if b else ref
                if end <= a:
                    flag('INVALID_HISTORICAL_SPAN', lot)
                else:
                    occupancy['worker_span'][w].append((a, end, lot))
                    occupancy['machine_span'][m].append((a, end, lot))
                try:
                    productive = segments(row, ref)
                except (TypeError, ValueError):
                    flag('INVALID_SEGMENTS', lot)
                    productive = []
                if not productive:
                    flag('HISTORICAL_SEGMENTS_MISSING', lot)
                for left, right in productive:
                    if left < a or (b and right > b):
                        flag('SEGMENT_OUTSIDE_LOT_SPAN', lot)
                    intervals.append((left, right, w, m, lot))
                    occupancy['worker_productive'][w].append((left, right, lot))
                    occupancy['machine_productive'][m].append((left, right, lot))
                    valid = on_model_calendar(left, right, w, memberships, batches, schedules)
                    if valid is True: calendar_pass += 1
                    elif valid is False:
                        calendar_fail += 1
                        flag('SEEDED_MODEL_CALENDAR_SEGMENT_MISMATCH', lot)
                    else: calendar_unknown += 1
                line_assignment_counts[line] += 1
            else:
                future_rows += 1
            pa, pb = stamp(row['planned_start_time']), stamp(row['planned_end_time'])
            if pa and pb and pa < pb:
                occupancy['planned_worker_span'][w].append((pa, pb, lot))
                occupancy['planned_machine_span'][m].append((pa, pb, lot))
        peak = simultaneous(intervals)
        operation_evidence.append({'operation_key': key_string, 'operation_name': op['operation_name'],
            'machine_type': op['required_machine_type'], 'historical_lots': len(history),
            'distinct_recorded_workers': len({r['employee_id'] for r in history}),
            'distinct_recorded_machines': len({r['asset_id'] for r in history}),
            'peak_simultaneous_productive': peak,
            'staffing_evidence': 'OBSERVED' if history else 'UNKNOWN',
            'evidence_provenance': 'CAPTURED_SEEDED_TEST_HISTORY',
            'required_crew_status': 'UNKNOWN', 'required_crew_size': None,
            'quantity': {'total_lots': sum(int(r['lot_quantity']) for r in lots),
                         'completed_at_reference': sum(int(r['lot_quantity']) for r in lots if stamp(r['actual_end_time']) and stamp(r['actual_end_time']) <= ref)}})

    for lot, count in seen_lot_ids.items():
        if count != 1: flag('DUPLICATE_LOT_ID', lot)
    for row in source['lots']:
        if (row['production_order_id'], row['operation_id']) not in ops:
            flag('ORPHAN_LOT', str(row['schedule_item_id']))
    whole_operation_boundary_differences = []
    for order, route in routes.items():
        route.sort(key=lambda key: ops[key]['operation_sequence_number'])
        if len({ops[key]['operation_sequence_number'] for key in route}) != len(route):
            flag('DUPLICATE_ROUTING_SEQUENCE', order)
        for pred, succ in zip(route, route[1:]):
            starts = [stamp(r['actual_start_time']) for r in byop[succ] if stamp(r['actual_start_time']) and stamp(r['actual_start_time']) < ref]
            ends = [stamp(r['actual_end_time']) for r in byop[pred]]
            if starts and (not ends or any(end is None for end in ends) or max(ends) > min(starts)):
                whole_operation_boundary_differences.append([f'{pred[0]}_{pred[1]}', f'{succ[0]}_{succ[1]}'])
            for lot in lot_precedence_errors(byop[pred], byop[succ], ref):
                flag('HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY', lot)
            for lot in lot_precedence_errors(byop[pred], byop[succ], planned=True):
                flag('PLANNED_LOT_PRECEDENCE_OR_QUANTITY', lot)
    for row in source['wip']:
        key = (row['production_order_id'], row['operation_id'])
        if key not in ops: flag('ORPHAN_WIP', row['wip_id'])
    future_wip_rows = sum(bool(stamp(row['last_updated_time']) and stamp(row['last_updated_time']) > ref)
                          for row in source['wip'])

    conflict_reports = {}
    for name, grouped in occupancy.items():
        results = {str(resource): overlaps(values) for resource, values in grouped.items()}
        conflict_reports[name] = {'overlap_pairs': sum(r['overlap_pairs'] for r in results.values()),
                                 'resources_with_overlaps': sum(r['overlap_pairs'] > 0 for r in results.values()),
                                 'conflicting_resources': {key: r for key, r in results.items() if r['overlap_pairs']}}
    type_groups = defaultdict(list)
    for row in operation_evidence:
        type_groups[(row['operation_name'], row['machine_type'])].append(row)
    types = []
    for (name, mtype), rows in sorted(type_groups.items()):
        types.append({'operation_name': name, 'machine_type': mtype, 'instances': len(rows),
            'staffing_evidence': 'OBSERVED' if any(r['historical_lots'] for r in rows) else 'UNKNOWN',
            'required_crew_status': 'UNKNOWN', 'required_crew_size': None,
            'historical_lots': sum(r['historical_lots'] for r in rows),
            'max_simultaneous_workers_across_distinct_lots': max(r['peak_simultaneous_productive']['workers'] for r in rows),
            'required_crew_inference': 'NOT_MADE; distinct lots can run concurrently without forming one mandatory crew'})
    line_reports = []
    for lid in sorted(lines):
        line_workers = {w for w in workers if any(b in batches and batches[b]['line_id'] == lid for b in memberships[w])}
        mids = {m for m in machines if machines[m]['line_id'] == lid}
        line_reports.append({'line_id': lid, 'active': lines[lid]['is_active'], 'machines': len(mids),
            'roster_workers_all_rotations': len(line_workers), 'batches': sum(b['line_id'] == lid for b in batches.values()),
            'roster_workers_per_machine': round(len(line_workers) / len(mids), 3) if mids else None,
            'historical_lot_assignments': line_assignment_counts[lid],
            'staffing_capacity': 'NOT_CAPTURED; roster count is not simultaneous staffing demand'})
    efficiencies = [value for record in skills.values() for value in record.values()]
    model_calendar_workers = {w for w in workers if len(schedules[w]) == 1 and len(memberships[w]) <= 1
                              and all(b in batches and batches[b]['is_active'] for b in memberships[w])}
    return {'factory_id': fid, 'source_reference': source['reference_time'], 'dataset': source['dataset'],
        'totals': {'workers': len(workers), 'machines': len(machines), 'lines': len(lines),
                   'orders': len(orders), 'operations': len(ops), 'lots': len(source['lots'])},
        'admission_counts': readiness['counts'], 'scheduling_ready_proven': 0,
        'worker_roles': dict(Counter(w['role'] for w in workers.values())),
        'worker_factory_mismatches': sum(w['factory_id'] != fid for w in workers.values()),
        'machine_factory_mismatches': sum(m['factory_id'] != fid for m in machines.values()),
        'machine_statuses': dict(Counter(m['current_status'] for m in machines.values())),
        'worker_efficiency_range': [min(efficiencies), max(efficiencies)] if efficiencies else None,
        'workers_without_skills': sum(not skills[w] for w in workers),
        'workers_in_multiple_batches': sum(len(memberships[w]) > 1 for w in workers),
        'workers_without_batch': sum(not memberships[w] for w in workers),
        'workers_without_calendar_rows': sum(not schedules[w] for w in workers),
        'workers_flagged_leave': sum(any(r['is_on_leave'] for r in schedules[w]) for w in workers),
        'calendar_dates': sorted({r['shift_date'] for r in resources['work_schedules']}),
        'calendar_basis': 'SEEDED_ROTATION_FOR_BATCHES; STATIC_BAND_FOR_FLOATERS; NOT_OBSERVED_ATTENDANCE',
        'factory_approved_future_calendar_completeness': 'UNKNOWN',
        'cross_line_assignments': dict(cross_line_assignments),
        'cross_line_policy_status': 'UNKNOWN; TEST SEEDER ALLOWS FACTORY-WIDE SKILL-POOL SELECTION',
        'lot_successors_before_whole_predecessor_operation_complete': len(whole_operation_boundary_differences),
        'lot_successor_boundary_examples': whole_operation_boundary_differences[:12],
        'future_wip_rows_excluded_from_reference_state': future_wip_rows,
        'calendar_segment_checks': {'pass': calendar_pass, 'fail': calendar_fail, 'unknown': calendar_unknown},
        'operations_with_test_machine_pool': sum(bool(pools[o['required_machine_type']]['machines']) for o in ops.values()),
        'operations_with_test_worker_pool': sum(bool(pools[o['required_machine_type']]['workers']) for o in ops.values()),
        'operations_with_model_calendar_worker_pool': sum(bool(set(pools[o['required_machine_type']]['workers']) & model_calendar_workers) for o in ops.values()),
        'assignment_check_failures': {code:issues[code] for code in ('WORKER_FACTORY_OR_ID','MACHINE_FACTORY_OR_ID','MACHINE_LINE_MISMATCH','MACHINE_CAPABILITY_MISMATCH','WORKER_SKILL_MISMATCH','RECORDED_BATCH_MEMBERSHIP_MISMATCH','HISTORICAL_LOT_PRECEDENCE_OR_QUANTITY','PLANNED_LOT_PRECEDENCE_OR_QUANTITY','LOT_QUANTITY_VS_ORDER_MISMATCH','DUPLICATE_LOT_ID','DUPLICATE_ROUTING_SEQUENCE')},
        'orders_missing_due_date': sum(not o['delivery_deadline'] for o in orders.values()),
        'historical_lots_before_reference': historical_rows, 'future_or_unknown_lots_excluded_from_history': future_rows,
        'operations_with_historical_assignments': sum(r['historical_lots'] > 0 for r in operation_evidence),
        'historical_operations_with_one_distinct_assigned_worker': sum(r['historical_lots'] > 0 and r['distinct_recorded_workers'] == 1 for r in operation_evidence),
        'historical_operations_with_multiple_distinct_assigned_workers': sum(r['distinct_recorded_workers'] > 1 for r in operation_evidence),
        'operations_with_simultaneous_distinct_lot_workers': sum(r['peak_simultaneous_productive']['workers'] > 1 for r in operation_evidence),
        'max_simultaneous_workers_on_distinct_lots_for_one_operation': max((r['peak_simultaneous_productive']['workers'] for r in operation_evidence), default=0),
        'max_simultaneous_lots_for_one_operation': max((r['peak_simultaneous_productive']['lots'] for r in operation_evidence), default=0),
        'authoritative_crew_requirements': 0, 'inferred_mandatory_crew_requirements': 0,
        'single_worker_machine_row_schema': True, 'same_lot_multiworker_crew_not_represented': True,
        'eligibility_pools': pools, 'line_staffing': line_reports, 'operation_types': types,
        'operation_evidence': operation_evidence, 'issues': dict(issues), 'issue_examples': dict(examples),
        'resource_conflicts': conflict_reports}


def markdown(report):
    t = report['totals']
    lines = [f"# Factory {report['factory_id']} resource assignment audit", '',
        '| Metric | Result |', '| --- | --- |',
        f"| Operations / orders | {t['operations']} / {t['orders']} |",
        f"| Workers / machines / lines | {t['workers']} / {t['machines']} / {t['lines']} |",
        f"| Operations with TEST machine / worker eligibility pools | {report['operations_with_test_machine_pool']} / {report['operations_with_test_worker_pool']} |",
        f"| Operations with a worker pool carrying a defined TEST calendar | {report['operations_with_model_calendar_worker_pool']} |",
        f"| Historical lots / future lots excluded | {report['historical_lots_before_reference']} / {report['future_or_unknown_lots_excluded_from_history']} |",
        f"| Operations with historical assignments | {report['operations_with_historical_assignments']} |",
        f"| Historical ops with one / multiple distinct workers across lots | {report['historical_operations_with_one_distinct_assigned_worker']} / {report['historical_operations_with_multiple_distinct_assigned_workers']} |",
        f"| Operations with multiple simultaneous lot workers | {report['operations_with_simultaneous_distinct_lot_workers']} |",
        f"| Cross-line assignments (all captured / historical) | {report['cross_line_assignments'].get('all_captured', 0)} / {report['cross_line_assignments'].get('historical', 0)} |",
        f"| Successor lot started before whole predecessor operation completed | {report['lot_successors_before_whole_predecessor_operation_complete']} |",
        f"| Future WIP rows excluded at reference time | {report['future_wip_rows_excluded_from_reference_state']} |",
        '| Authoritative crew requirements / proven scheduling-ready operations | 0 / 0 |',
        f"| Source admission classifications | {report['admission_counts']} |", '',
        'Eligibility uses captured factory/line/capability/skill records and the existing code taxonomy. It is not evidence of approved staffing or complete legal line mobility. Historical means clipped before the dataset reference time; the source is generated TEST data, not verified factory actuals.', '',
        f"Worker roles: `{report['worker_roles']}`. Recorded skill efficiency range: `{report['worker_efficiency_range']}` percent. Workers without skills: {report['workers_without_skills']}; without batch membership: {report['workers_without_batch']}; with multiple batches: {report['workers_in_multiple_batches']}; flagged leave: {report['workers_flagged_leave']}; missing calendar rows: {report['workers_without_calendar_rows']}.", '',
        f"Machine current statuses: `{report['machine_statuses']}`. Missing order deadlines: {report['orders_missing_due_date']}.", '',
        '## Resource pools and potential bottlenecks', '',
        '| Machine requirement | Machines | Skilled active operators | Same-line batch candidates | Nominal SAM × quantity minutes |',
        '| --- | ---: | ---: | ---: | ---: |']
    for name, pool in report['eligibility_pools'].items():
        lines.append(f"| {name} | {len(pool['machines'])} | {len(pool['workers'])} | {len(pool['batch_line_compatible_workers'])} | {pool['nominal_sam_quantity_minutes']} |")
    lines += ['', 'Nominal workload is a demand screen across all captured operations, including completed work. It is not residual workload, utilization, elapsed duration, or proven bottleneck capacity; crew and calendar authority are missing.', '',
        '## Assignment and calendar checks', '', f"Issues: `{report['issues']}`.", '',
        f"Specific assignment checks (zero means no inconsistency detected): `{report['assignment_check_failures']}`.", '',
        f"Model calendar segment checks: `{report['calendar_segment_checks']}`. Explicit schedule dates: `{report['calendar_dates']}`.", '',
        '| Conflict check | Overlap pairs | Resources affected |', '| --- | ---: | ---: |']
    for name, value in report['resource_conflicts'].items():
        lines.append(f"| {name} | {value['overlap_pairs']} | {value['resources_with_overlaps']} |")
    lines += ['', 'Span overlap includes pauses; productive overlap means actual productive segments overlap. Planned assignments are checked separately and do not become approved commitments. Current machine status and leave flags cannot establish historical breakdown or attendance intervals.', '',
        'The seeder explicitly selects workers from factory-wide skill pools (`seed_database_fixed.py::build_frozen_planned_schedule`) and releases each successor after its matching lot predecessor (`simulate_actual_execution`). Therefore cross-batch-line assignments and successor lots starting before every predecessor lot completes are differences from stricter CRAFT policies requiring factory confirmation, not proof of a violation of the seeded lot model. Concurrent distinct lots do not establish one-lot crew size.', '',
        '## Line staffing patterns', '',
        '| Line | Machines | Roster across shifts | Batches | Roster / machine | Historical lot assignments |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for row in report['line_staffing']:
        lines.append(f"| {row['line_id']} | {row['machines']} | {row['roster_workers_all_rotations']} | {row['batches']} | {row['roster_workers_per_machine']} | {row['historical_lot_assignments']} |")
    lines += ['', 'These are roster ratios across rotations, not simultaneous crews, capacity guarantees, or utilization.', '',
        '## Staffing evidence for every operation type', '',
        '| Operation | Machine type | Evidence | Historical lots | Max concurrent lot workers | Required crew |',
        '| --- | --- | --- | ---: | ---: | --- |']
    for row in report['operation_types']:
        lines.append(f"| {row['operation_name']} | {row['machine_type']} | {row['staffing_evidence']} (seeded TEST records) | {row['historical_lots']} | {row['max_simultaneous_workers_across_distinct_lots']} | UNKNOWN |")
    lines += ['', 'A scalar employee/machine pair per lot and parallel execution of distinct lots do not establish a mandatory crew size. No requirement is classified AUTHORITATIVE or silently inferred. Factory-approved operation staffing rules, line mobility, calendar/absence authority, and unresolved execution boundaries remain required.', '',
        'Detailed pool IDs, every operation identity, line roster ratios, quantities and bounded conflict examples are in the corresponding JSON. No database or scheduling component was modified.']
    return '\n'.join(lines) + '\n'


def main():
    started = time.perf_counter()
    manifest = json.loads((CAPTURE / 'manifest.json').read_text())
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    if not all(sha(CAPTURE / name) == expected for name, expected in manifest.items()):
        raise RuntimeError('Pinned input hash mismatch')
    out = Path(__file__).resolve().parent / ('run_' + datetime.now().strftime('%Y%m%dT%H%M%S%f'))
    out.mkdir(exist_ok=False)
    summaries, resource_owners = [], defaultdict(set)
    for fid in range(1, 7):
        source = json.loads((CAPTURE / f'factory{fid}_source.json').read_text())
        readiness = json.loads((CAPTURE / f'factory{fid}_readiness.json').read_text())
        for kind, table, field in [('worker','workers','employee_id'),('machine','machines','asset_id')]:
            for row in source['resources'][table]: resource_owners[(kind,row[field])].add(fid)
        report = analyze(source, readiness)
        report['source_sha256'] = manifest[f'factory{fid}_source.json']
        (out / f'factory{fid}.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        (out / f'factory{fid}.md').write_text(markdown(report), encoding='utf-8')
        summary = {k:report[k] for k in ('factory_id','totals','admission_counts','issues','assignment_check_failures','historical_lots_before_reference','future_or_unknown_lots_excluded_from_history','operations_with_historical_assignments','historical_operations_with_one_distinct_assigned_worker','historical_operations_with_multiple_distinct_assigned_workers','operations_with_simultaneous_distinct_lot_workers','max_simultaneous_workers_on_distinct_lots_for_one_operation','lot_successors_before_whole_predecessor_operation_complete','cross_line_assignments','calendar_segment_checks','authoritative_crew_requirements','scheduling_ready_proven')}
        summary['conflicts'] = {k:{'pairs':v['overlap_pairs'],'resources':v['resources_with_overlaps']} for k,v in report['resource_conflicts'].items()}
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
    if not all(sha(CAPTURE / name) == expected for name, expected in manifest.items()):
        raise RuntimeError('Protected inputs changed')
    consolidated = {'factories':summaries, 'cross_factory_duplicate_resource_identities':[str(key) for key,value in resource_owners.items() if len(value)>1],
        'input_manifest_sha256':sha(CAPTURE/'manifest.json'), 'verified_input_files':len(manifest),
        'database_connections':0, 'database_writes':0, 'production_activation':'DISABLED', 'wall_seconds':time.perf_counter()-started}
    (out/'comparison.json').write_text(json.dumps(consolidated, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    comparison_lines = ['# Six-factory worker and machine assignment audit', '',
        f"Pinned input files verified: {len(manifest)}. Database connections: 0. Database writes: 0. Production activation: DISABLED.", '',
        'All counts below are from isolated generated TEST captures, not verified factory actuals. Crew-size requirements are absent from the captured schema; historical worker rows are observations only.', '',
        '| Factory | Orders | Operations | Workers | Machines | Lines | Historical lots | Ops 1 worker / >1 workers across lots | Ops with concurrent lot workers | Lot successors before full predecessor completion | Cross-line assignments captured / historical | Model-calendar segments pass/fail | Span/productive conflicts | Blocked partial | Crew-unknown unstarted |',
        '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in summaries:
        t, c = row['totals'], row['conflicts']
        conflict_pairs = sum(v['pairs'] for k, v in c.items() if k in ('worker_span','machine_span','worker_productive','machine_productive','planned_worker_span','planned_machine_span'))
        comp_lines = f"{t['orders']} | {t['operations']} | {t['workers']} | {t['machines']} | {t['lines']} | {row['historical_lots_before_reference']} | {row['historical_operations_with_one_distinct_assigned_worker']}/{row['historical_operations_with_multiple_distinct_assigned_workers']} | {row['operations_with_simultaneous_distinct_lot_workers']} | {row['lot_successors_before_whole_predecessor_operation_complete']} | {row['cross_line_assignments'].get('all_captured', 0)}/{row['cross_line_assignments'].get('historical', 0)} | {row['calendar_segment_checks']['pass']}/{row['calendar_segment_checks']['fail']} | {conflict_pairs} | {row['admission_counts'].get('BLOCKED',0)} | {row['admission_counts'].get('REQUIRES_EVIDENCE',0)}"
        comparison_lines.append(f"| {row['factory_id']} | {comp_lines} |")
    comparison_lines += ['', 'No observed multi-worker crews are represented: each captured lot row stores one employee and one machine. Different lots of an operation run concurrently, which is not evidence of a crew requirement. Resource eligibility pools use the captured TEST capabilities/skills plus the existing taxonomy crosswalk; factory approval, complete legal alternatives, contemporaneous attendance, and line-mobility policy remain unverified.', '', 'Individual factory Markdown and JSON files contain operation-type staffing evidence, resource pools, line roster ratios, conflict examples, calendars, and operation accounting.']
    (out/'comparison.md').write_text('\n'.join(comparison_lines)+'\n', encoding='utf-8')
    (out/'README.md').write_text(
        '# Audit provenance and interpretation\n\n'
        'This run read six immutable source/readiness JSON captures and their pinned manifest. It did not connect to MySQL and did not import or call the scheduler. Inputs were hash-checked before and after analysis. Outputs are new audit artifacts; no source capture, Django record, schedule, or frozen benchmark was modified.\n\n'
        'Historical execution means captured actual-work segments clipped at the snapshot reference timestamp. These captures are generated TEST data under `frozen-plan-then-actual-v1`, not verified operational history. Calendar checks verify conformance to the seeder model only. Single employee/machine per lot records are observed schema rows, not authoritative crew rules. The seeded route model checks matching lot-index predecessor completion; CRAFT whole-operation release is stricter. Cross-line allocations are allowed by this seeder’s factory-wide skilled-worker selection, but the factory policy is unknown.\n\n'
        'No crew-size field or factory-approved staffing rule was found in the captured source. The 200-operation cycle / 25-operation CP-SAT-window target is not established safe by this audit: all unresolved real partials remain blocked, crew requirements are unknown, and factory policies for lot transfer, calendars, and line mobility require authoritative confirmation.\n', encoding='utf-8')
    output_hashes = {p.name:sha(p) for p in out.iterdir() if p.is_file()}
    (out/'manifest.json').write_text(json.dumps(output_hashes, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    print(json.dumps({'output':str(out.relative_to(ROOT)), 'wall_seconds':consolidated['wall_seconds']}), flush=True)


if __name__ == '__main__':
    main()
