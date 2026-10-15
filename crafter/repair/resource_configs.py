"""Timing-independent physical resource domains for deterministic repair."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from itertools import combinations
from math import comb

from crafter.authoritative_semantics import (authoritative_duration_contract, authoritative_setup_seconds,
                                            calendar_minutes_from_integer_seconds)
from crafter.disruptions.benchmark import _candidate_domains
from crafter.lns_probe.line_k_model import _available_machine_pool
from .event_semantics import event_semantic


@dataclass(frozen=True)
class ResourceConfiguration:
    operation_key: str
    factory_id: int
    line_id: int
    k: int
    machine_ids: tuple[int, ...]
    worker_ids: tuple[str, ...]
    setup_seconds: int
    productive_seconds: int
    provenance: tuple[str, ...] = ()
    # Calendar execution consumes the authoritative native minute value.  The
    # integer-second projection is retained for audit/CP duration accounting,
    # but reconstructing minutes as ``seconds / 60`` can cross a floating ceil
    # boundary and manufacture one extra productive second.
    productive_minutes: float = 0.0
    quantity: int | None = None
    execution_contract_version: str | None = None
    evidence_class: str | None = None


def _calendar_key(index, employee):
    if employee in index.get('crew_of', {}):
        return ('crew',) + tuple(index['crew_of'][employee])
    if employee in index.get('shift_of', {}):
        return ('shift',) + tuple(index['shift_of'][employee])
    return ('always',)


def _diverse_sets(pool, k, limit):
    """Deterministic prefix of legal sets, eventually complete at the exact cap.

    Spread witnesses are tried first; a lazy combination iterator fills any
    remaining budget. The sequence does not depend on ``limit``, so increasing
    the cap never removes an already exposed alternative.
    """
    pool=tuple(pool); k=int(k); limit=int(limit); out=[]
    if len(pool)<k or limit <= 0: return out,_legal_combination_count(len(pool), k)>0
    last=max(0,len(pool)-k)
    offsets=[0,last]
    offsets.extend(range(max(1,k),last,max(1,k)))
    for offset in offsets:
        item=tuple(pool[offset:offset+k])
        if len(item)==k and item not in out: out.append(item)
        if len(out)>=limit: break
    if len(out)<limit:
        seen=set(out)
        for item in combinations(pool,k):
            if item in seen: continue
            out.append(item); seen.add(item)
            if len(out)>=limit: break
    return out, _legal_combination_count(len(pool), k) > len(out)


def _legal_combination_count(pool_size, k):
    """Return the exact finite set count without materializing combinations."""
    if int(pool_size) < int(k):
        return 0
    return int(comb(int(pool_size), int(k)))


def _source_authoritative_configurations(state, keys, machine_cap, worker_cap):
    """Generate exact source-eligible single-worker choices for a pinned state.

    This opt-in contract deliberately does not infer line/k or duration from
    frozen research inputs. A multi-worker duration rule is not present in the
    Factory 1 capture, so admitting one here would invent execution semantics.
    Calendar and commitment feasibility remain the exact regime/CP concern.
    """
    source = state['source_resource_domain']
    if source.get('contract_version') != 'SOURCE_RESOURCE_DOMAIN_V1':
        raise ValueError('UNKNOWN_SOURCE_RESOURCE_DOMAIN_CONTRACT')
    if not source.get('source_sha256') or not source.get('snapshot_hash') or not source.get('scenario_hash'):
        raise ValueError('INCOMPLETE_SOURCE_RESOURCE_PROVENANCE')
    expected = state.get('source_resource_provenance')
    if expected is None or any(source[name] != expected.get(name)
                               for name in ('source_sha256', 'snapshot_hash', 'scenario_hash')):
        raise ValueError('SOURCE_RESOURCE_PROVENANCE_MISMATCH')
    problem = state['problem']
    machines_by_id = problem.machine_by_id
    operations_by_key = problem.operation_by_key
    orders_by_id = problem.order_by_id
    result, audit = {}, []
    for key in keys:
        spec = source['operations'].get(key)
        if spec is None:
            raise ValueError(f'SOURCE_RESOURCE_OPERATION_MISSING:{key}')
        operation = operations_by_key[key]
        factory = int(orders_by_id[operation.order_id].factory_id)
        if int(spec['factory_id']) != factory or int(spec['crew_size']) != 1 or \
                operation.min_machine_count != 1 or operation.max_machine_count != 1:
            raise ValueError(f'SOURCE_RESOURCE_UNSUPPORTED_CREW_OR_FACTORY:{key}')
        capability = str(operation.required_capability).strip().lower()
        machine_pool = tuple(int(mid) for mid in spec['machine_ids'])
        worker_pool = tuple(str(wid) for wid in spec['worker_ids'])
        if len(machine_pool) != len(set(machine_pool)) or len(worker_pool) != len(set(worker_pool)):
            raise ValueError(f'SOURCE_RESOURCE_DUPLICATE_ALTERNATIVE:{key}')
        for mid in machine_pool:
            machine = machines_by_id.get(mid)
            if machine is None or int(machine.factory_id) != factory or not machine.is_usable or \
                    not machine.supports(capability) or int(machine.line_id) not in operation.eligible_line_ids:
                raise ValueError(f'SOURCE_RESOURCE_MACHINE_INELIGIBLE:{key}:{mid}')
        efficiency = spec['worker_efficiency']
        if set(worker_pool) != set(str(wid) for wid in efficiency):
            raise ValueError(f'SOURCE_RESOURCE_WORKER_EFFICIENCY_MISSING:{key}')
        source_skilled = {str(wid) for wid in state['operator_index'].get('by_factory', {}).get(
            (factory, capability), ())}
        if not set(worker_pool).issubset(source_skilled):
            raise ValueError(f'SOURCE_RESOURCE_WORKER_INELIGIBLE:{key}')
        for wid in worker_pool:
            if Decimal(str(efficiency[wid])) <= 0:
                raise ValueError(f'SOURCE_RESOURCE_WORKER_EFFICIENCY_INVALID:{key}:{wid}')
        selected_machines, _ = _diverse_sets(machine_pool, 1, machine_cap)
        selected_workers, _ = _diverse_sets(worker_pool, 1, worker_cap)
        execution_contract = spec.get('execution_contract')
        if execution_contract is None:
            raise ValueError(f'EXECUTION_CONTRACT_REQUIRED:{key}')
        from crafter.execution_contract import OperationExecutionContract
        contract = (execution_contract if isinstance(execution_contract, OperationExecutionContract)
                    else OperationExecutionContract.from_mapping(execution_contract))
        if (contract.operation_key != key or contract.factory_id != factory
                or contract.required_worker_count != 1 or contract.required_machine_count != 1
                or set(contract.eligible_worker_ids) != set(worker_pool)
                or set(contract.eligible_machine_ids) != set(machine_pool)
                or not set(contract.eligible_line_ids).issubset(set(operation.eligible_line_ids))
                or contract.source_fingerprint != source['source_sha256']
                or contract.snapshot_fingerprint != source['snapshot_hash']
                or contract.scenario_fingerprint != source['scenario_hash']
                or contract.quantity <= 0
                or set(contract.worker_efficiencies) != set(worker_pool)
                or tuple(contract.required_skills) != (capability,)
                or any(contract.worker_efficiencies[wid] / Decimal(100)
                       != Decimal(str(efficiency[wid])) for wid in worker_pool)
                or contract.setup_seconds != int(spec['setup_seconds'])):
            raise ValueError(f'EXECUTION_CONTRACT_DOMAIN_MISMATCH:{key}')
        base_minutes = Decimal(str(spec['base_productive_minutes']))
        if base_minutes <= 0 or int(spec['setup_seconds']) != operation.setup_seconds:
            raise ValueError(f'SOURCE_RESOURCE_DURATION_INVALID:{key}')
        configs = []
        for (mid,) in selected_machines:
            for (wid,) in selected_workers:
                if int(machines_by_id[mid].line_id) not in set(contract.eligible_line_ids):
                    continue
                try:
                    duration = contract.expected_productive_seconds((wid,), (mid,))
                except ValueError as exc:
                    if str(exc) == 'RESOURCE_PAIR_NOT_ALLOWED':
                        continue
                    raise
                productive_minutes = calendar_minutes_from_integer_seconds(duration)
                if duration != int((base_minutes * 60 / Decimal(str(efficiency[wid]))).to_integral_value(rounding=ROUND_CEILING)):
                    raise ValueError(f'EXECUTION_CONTRACT_DURATION_MISMATCH:{key}:{wid}')
                configs.append(ResourceConfiguration(
                    key, factory, int(machines_by_id[mid].line_id), 1, (mid,), (wid,),
                    int(spec['setup_seconds']), duration,
                    ('SOURCE_RESOURCE_DOMAIN_V1', source['source_sha256']),
                    float(productive_minutes), int(contract.quantity),
                    contract.contract_version, contract.evidence_class))
        result[key] = tuple(configs)
        truncated = len(selected_machines) < len(machine_pool) or len(selected_workers) < len(worker_pool)
        audit.append({
            'operation_key': key, 'factory': factory,
            'eligible_lines': list(operation.eligible_line_ids),
            'legal_k_by_line': {str(line): [1] for line in operation.eligible_line_ids},
            'total_resource_configurations': len(configs),
            'additional_legal_configurations_available': truncated, 'truncated': truncated,
            'source_sha256': source['source_sha256'], 'snapshot_hash': source['snapshot_hash'],
            'scenario_hash': source['scenario_hash'],
            'domain_rows': [{
                'line_id': None, 'k': 1, 'machine_pool': list(machine_pool),
                'worker_pool': list(worker_pool),
                'candidate_machine_sets_after_pruning': [list(x) for x in selected_machines],
                'worker_crews_after_pruning': [list(x) for x in selected_workers],
                'total_legal_machine_sets': len(machine_pool),
                'total_legal_worker_crews': len(worker_pool),
                'machine_pool_complete': bool(spec.get('machine_pool_complete', False)),
                'worker_pool_complete': bool(spec.get('worker_pool_complete', False)),
                'more_machine_sets_available': len(selected_machines) < len(machine_pool),
                'more_worker_crews_available': len(selected_workers) < len(worker_pool),
                'worker_eligibility_scope': 'SOURCE_SKILL_AND_ATTENDANCE_CALENDAR_IN_REGIMES',
            }],
        })
    return result, audit, {
        'operations': len(keys), 'configurations': sum(map(len, result.values())),
        'zero_domains': [key for key in keys if not result[key]],
        'generator': 'SOURCE_RESOURCE_DOMAIN_V1', 'joint_modes_consumed': False,
        'joint_mode_generator_called': False,
    }


def generate_matching_worker_crews(slots, candidates_by_slot, *, forbidden=(),
                                   max_solutions=8, worker_pressure=None,
                                   worker_usage=None):
    """Construct a compact deterministic set of feasible worker matchings.

    This is deliberately not a Cartesian-product generator.  Each requested
    crew is produced by a Kuhn-style augmenting-path matching over worker
    slots, with deterministic contention/diversity tie breaks.  The returned
    crews are witnesses only; canonical calendar regimes and CP-SAT remain
    authoritative for execution feasibility.
    """
    limit = max(0, int(max_solutions))
    if limit == 0 or not slots or len(candidates_by_slot) != len(slots):
        return ()
    forbidden_set = {tuple(sorted(str(x) for x in crew)) for crew in forbidden}
    pressure = {str(k): int(v) for k, v in (worker_pressure or {}).items()}
    usage = {str(k): int(v) for k, v in (worker_usage or {}).items()}
    results = []
    normalized = [tuple(sorted({str(x) for x in candidates}))
                  for candidates in candidates_by_slot]
    if any(not row for row in normalized):
        return ()
    # The common fixed-crew case has identical candidate sets for every
    # required slot.  Produce deterministic sliding/offset witnesses directly
    # instead of repeatedly solving the same matching graph; this is still an
    # exact matching because every selected worker is distinct.
    if all(row == normalized[0] for row in normalized):
        ordered = list(normalized[0])
        ordered.sort(key=lambda worker: (pressure.get(worker, 0),
                                         usage.get(worker, 0), worker))
        k = len(slots)
        if len(ordered) < k:
            return ()
        offsets = [0]
        stride = max(1, k)
        offsets.extend(range(stride, max(1, len(ordered) - k + 1), stride))
        offsets.append(max(0, len(ordered) - k))
        for offset in offsets:
            crew = tuple(sorted(ordered[offset:offset + k]))
            if len(crew) == k and crew not in forbidden_set and crew not in results:
                results.append(crew)
            if len(results) >= limit:
                break
        return tuple(results)
    slot_order = sorted(range(len(slots)),
                        key=lambda i: (len(normalized[i]), str(slots[i]), i))
    all_workers = sorted({worker for row in normalized for worker in row})
    # This rank is immutable for the matching call.  Looking up every worker
    # with ``list.index`` inside every strategy/slot sort made the compact
    # matcher quadratic in the eligible pool without changing its ordering.
    worker_rank = {worker: index for index, worker in enumerate(all_workers)}
    # A bounded deterministic family of orderings yields diversity without
    # enumerating combinations.  The augmenting-path matcher guarantees an
    # exact matching when one exists for the chosen ordering.
    attempts = max(limit * 4, limit)
    for strategy in range(attempts):
        assignment: dict[str, int] = {}
        chosen_by_slot: dict[int, str] = {}

        def ordered_candidates(slot_index):
            row = list(normalized[slot_index])
            row.sort(key=lambda worker: (
                pressure.get(worker, 0), usage.get(worker, 0),
                (worker_rank[worker] + strategy) % max(1, len(all_workers)),
                worker))
            return row

        def augment(slot_index, seen_workers):
            for worker in ordered_candidates(slot_index):
                if worker in seen_workers:
                    continue
                seen_workers.add(worker)
                other_slot = assignment.get(worker)
                if other_slot is None or augment(other_slot, seen_workers):
                    previous = chosen_by_slot.get(slot_index)
                    if previous is not None and previous != worker:
                        assignment.pop(previous, None)
                    assignment[worker] = slot_index
                    chosen_by_slot[slot_index] = worker
                    return True
            return False

        if not all(augment(slot_index, set()) for slot_index in slot_order):
            continue
        crew = tuple(sorted(chosen_by_slot.values()))
        if len(set(crew)) != len(slots) or crew in forbidden_set or crew in results:
            continue
        results.append(crew)
        if len(results) >= limit:
            break
    return tuple(results)


def generate_resource_configurations(state, *, max_machine_sets=3,
                                     max_worker_crews=3,
                                     machine_pool_limit=30,
                                     operation_keys=None):
    """Generate physical choices without calling or consuming joint modes.

    The only pruning is deterministic candidate bounding.  Movable incumbent
    reservations are deliberately absent: resource concurrency belongs to CP.
    """
    problem, index, operator_index = state['problem'], state['after_index'], state['operator_index']
    keys = tuple(sorted(operation_keys if operation_keys is not None else state['keys']))
    if state.get('source_resource_domain') is not None:
        return _source_authoritative_configurations(
            state, keys, max_machine_sets, max_worker_crews)
    domains = _candidate_domains(index, problem, keys, event=state.get('event'))
    result, audit = {}, []
    for key in keys:
        op, row = problem.operation_by_key[key], index.by_key[key]
        factory = int(problem.order_by_id[op.order_id].factory_id)
        incumbent_machines = tuple(sorted(int(x) for x in row.get('machine_ids', ())))
        incumbent_workers = tuple(sorted(str(x) for x in row.get('employee_ids', ())))
        configs, seen = [], set(); line_rows = []
        any_more = False
        for domain in domains.get(key, ()):
            line, k = int(domain['line_id']), int(domain['k'])
            semantic=event_semantic(state.get('event'))
            event=state.get('event'); metadata=getattr(event,'metadata',{}) or {}
            affected_machines={int(x) for x in metadata.get(
                'unavailable_machine_ids',getattr(event,'machine_ids',()) or ())}
            if semantic == 'M2_CAPACITY_REDUCTION':
                target=str(metadata.get('capacity_target_operation_key',''))
                capacity_line=int(metadata.get('capacity_line_id',-1))
                allowed={int(x) for x in metadata.get('capacity_legal_k_values',())}
                if key==target and (line!=capacity_line or k not in allowed):
                    continue
            line_spec = problem.line_by_id.get(line)
            if line_spec is None or int(line_spec.factory_id) != factory:
                continue
            machine_pool = tuple(_available_machine_pool(problem, op, line))
            machine_pool = tuple(mid for mid in machine_pool
                                 if int(problem.machine_by_id[mid].factory_id) == factory
                                 and int(problem.machine_by_id[mid].line_id) == line)
            if (semantic == 'M2_CAPACITY_REDUCTION'
                    and key == str(metadata.get('capacity_target_operation_key',''))
                    and line == int(metadata.get('capacity_line_id',-1))):
                machine_pool=tuple(mid for mid in machine_pool if int(mid) not in affected_machines)
            # The configuration cap already bounds materialization. A second
            # fixed pool cap made eligible machines beyond its prefix
            # unreachable even after adaptive expansion.
            bounded_machine_pool = machine_pool
            machine_sets = []
            if line == int(row['line_id']) and len(incumbent_machines) == k and set(incumbent_machines).issubset(machine_pool):
                machine_sets.append(incumbent_machines)
            generated, machine_more = _diverse_sets(bounded_machine_pool, k, max_machine_sets)
            for item in generated:
                item = tuple(sorted(int(x) for x in item))
                if item not in machine_sets: machine_sets.append(item)
            machine_sets = machine_sets[:max_machine_sets]
            machine_set_capacity = _legal_combination_count(len(machine_pool), k)
            machine_more = machine_set_capacity > len(machine_sets)

            capability = str(op.required_capability).strip().lower()
            line_pool = list(dict.fromkeys(operator_index.get('by_line', {}).get((line, capability), ())))
            factory_pool = list(dict.fromkeys(operator_index.get('by_factory', {}).get((factory, capability), ())))
            factory_allowed = {str(x) for x in factory_pool}
            worker_pool = list({str(x): x for x in [*line_pool, *factory_pool]
                                if str(x) in factory_allowed}.values())
            groups = {}
            for worker in worker_pool:
                groups.setdefault(_calendar_key(operator_index, worker), []).append(worker)
            worker_crews = []
            matching_attempted = 0
            if len(incumbent_workers) == k and set(incumbent_workers).issubset(worker_pool):
                worker_crews.append(incumbent_workers)
            worker_more = False
            for group_key in sorted(groups, key=str):
                ranked = sorted(groups[group_key], key=lambda e: str(e))
                crews = generate_matching_worker_crews(
                    [capability] * int(k), [ranked] * int(k),
                    max_solutions=1)
                matching_attempted += 4
                for crew in crews:
                    crew = tuple(sorted(str(x) for x in crew))
                    if crew not in worker_crews: worker_crews.append(crew)
                    if len(worker_crews) >= max_worker_crews: break
                if len(worker_crews) >= max_worker_crews: break
            # Distinct calendars are not a physical incompatibility: exact
            # fixed-crew execution decides whether their shifts intersect.
            # Include cross-calendar crews, then lazily fill toward the full
            # skill-eligible combination count when the cap expands.
            ranked_pool = sorted(worker_pool, key=lambda e: str(e))
            if len(worker_crews) < max_worker_crews:
                diverse, _ = _diverse_sets(ranked_pool, k, max_worker_crews)
                for crew in diverse:
                    canonical = tuple(sorted(str(x) for x in crew))
                    if canonical not in worker_crews: worker_crews.append(canonical)
                    if len(worker_crews) >= max_worker_crews: break
            if len(worker_crews) < max_worker_crews:
                for crew in combinations(ranked_pool, k):
                    canonical = tuple(sorted(str(x) for x in crew))
                    if canonical not in worker_crews: worker_crews.append(canonical)
                    if len(worker_crews) >= max_worker_crews: break
            worker_set_capacity = _legal_combination_count(len(worker_pool), k)
            worker_crews = worker_crews[:max_worker_crews]
            worker_more = worker_set_capacity > len(worker_crews)
            duration = authoritative_duration_contract(state['op_details'][key], k)
            setup = authoritative_setup_seconds(state['op_details'][key])
            for machines in machine_sets:
                for workers in worker_crews:
                    cfg = ResourceConfiguration(key, factory, line, k, machines, workers,
                                                int(setup), int(duration.productive_seconds),
                                                ('TIMING_INDEPENDENT_AUTHORITATIVE_DOMAIN',),
                                                float(duration.productive_minutes))
                    sig = (line, k, machines, workers)
                    if sig not in seen: seen.add(sig); configs.append(cfg)
            any_more = any_more or machine_more or worker_more
            line_rows.append({'line_id':line,'k':k,'machine_pool':list(machine_pool),
                              'candidate_machine_sets_before_pruning':'COMBINATORIAL',
                              'candidate_machine_sets_after_pruning':[list(x) for x in machine_sets],
                              'total_legal_machine_sets':machine_set_capacity,
                              'machine_pool_complete':True,
                              'machine_pool_limit_applied':False,
                              'machine_pruning_reasons':['candidate_limit'] if machine_more else [],
                              'worker_pool':[str(x) for x in worker_pool],
                              'worker_crews_after_pruning':[list(x) for x in worker_crews],
                              'total_legal_worker_crews':worker_set_capacity,
                              'worker_eligibility_scope':'SKILL_FACTORY_ONLY_CALENDAR_CHECKED_IN_REGIMES',
                              'worker_domain_mode':'MATCHING_GENERATED',
                              'calendar_groups':len(groups),
                              'cross_calendar_crews_eligible':len(groups)>1 and k>1,
                              'matching_attempted':matching_attempted,
                              'matching_unique_crews':len(worker_crews),
                              'worker_pool_complete':True,
                              'worker_pruning_reasons':['candidate_limit'] if worker_more else [],
                              'more_machine_sets_available':machine_more,
                              'more_worker_crews_available':worker_more})
        result[key] = tuple(configs)
        incumbent_present = any(c.line_id == int(row['line_id']) and c.machine_ids == incumbent_machines and c.worker_ids == incumbent_workers for c in configs)
        audit.append({'operation_key':key,'lifecycle_state':state.get('classified',{}).get(key,{}).get('state','UNKNOWN'),
                      'factory':factory,'eligible_lines':[int(x) for x in op.eligible_line_ids],
                      'legal_k_by_line':{str(x['line_id']):sorted({int(y['k']) for y in domains.get(key,()) if int(y['line_id'])==int(x['line_id'])}) for x in domains.get(key,())},
                      'domain_rows':line_rows,'total_resource_configurations':len(configs),
                      'incumbent_configuration_present':incumbent_present,
                      'additional_legal_configurations_available':any_more,'truncated':any_more})
    return result, audit, {'operations':len(keys),'configurations':sum(len(v) for v in result.values()),
                           'zero_domains':[k for k,v in result.items() if not v],
                           'generator':'TIMING_INDEPENDENT','joint_modes_consumed':False,
                           'joint_mode_generator_called':False,
                           'worker_domain_mode':'MATCHING_GENERATED'}


def expand_resource_configurations(state, current_configs, operation_keys, *,
                                   max_machine_sets=6, max_worker_crews=6,
                                   machine_pool_limit=30):
    """Targeted deterministic expansion; untouched operation domains persist."""
    additions, audit, summary = generate_resource_configurations(
        state, max_machine_sets=max_machine_sets,
        max_worker_crews=max_worker_crews,
        machine_pool_limit=machine_pool_limit,
        operation_keys=operation_keys)
    merged = {key: tuple(values) for key, values in current_configs.items()}
    for key, values in additions.items():
        seen={(x.line_id,x.k,x.machine_ids,x.worker_ids) for x in merged.get(key,())}
        merged[key]=tuple(list(merged.get(key,()))+[x for x in values if (x.line_id,x.k,x.machine_ids,x.worker_ids) not in seen])
    return merged, audit, summary


def serialize_configurations(configurations):
    return {key:[asdict(value) for value in values] for key,values in configurations.items()}
