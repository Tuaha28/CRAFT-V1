"""Authoritative in-memory event effects used by deterministic repair."""
from dataclasses import replace
from datetime import timedelta

from crafter.lns_probe.anchored_model import (
    outside_machine_reservations, outside_worker_reservations,
)


def event_semantic(event):
    return str((getattr(event, 'metadata', {}) or {}).get('repair_semantics', ''))


def with_m2_capacity_contract(loaded, event):
    """Attach the benchmark's exact M2 legal-k contract to the event.

    M2 is not inferred from merely having a ``machine_ids`` field.  The
    benchmark defines a failed physical machine and expects same-line
    recovery at a screened smaller legal k.  This function derives that
    domain from the authoritative candidate-domain machinery and the
    remaining compatible same-line machine pool.
    """
    if event_semantic(event) != 'M2_CAPACITY_REDUCTION':
        return event
    problem, _operators, rows, _durations, _details, index, *_ = loaded
    target=str((event.metadata or {}).get('target_operation_key',''))
    row=next((x for x in rows if str(x['operation_key'])==target),None)
    if row is None:
        raise ValueError(f'M2 target operation is absent: {target}')
    from crafter.disruptions.benchmark import _candidate_domains
    from crafter.lns_probe.line_k_model import _available_machine_pool
    line=int(row['line_id']); incumbent_k=len(tuple(row.get('machine_ids',())))
    op=problem.operation_by_key[target]
    failed={int(x) for x in tuple(event.machine_ids or ())}
    remaining=tuple(int(x) for x in _available_machine_pool(problem,op,line)
                    if int(x) not in failed)
    domains=_candidate_domains(index,problem,(target,),event=event).get(target,())
    screened=sorted({int(x['k']) for x in domains
                     if int(x['line_id'])==line and 0<int(x['k'])<incumbent_k
                     and int(x['k'])<=len(remaining)})
    if not screened:
        raise ValueError(f'M2 has no screened smaller legal k for {target}')
    metadata={**(event.metadata or {}),
              'capacity_target_operation_key':target,
              'capacity_line_id':line,
              'capacity_incumbent_k':incumbent_k,
              'capacity_max_k':max(screened),
              'capacity_legal_k_values':screened,
              'unavailable_machine_ids':sorted(failed),
              'remaining_same_line_machine_ids':sorted(remaining),
              'capacity_contract_source':'AUTHORITATIVE_CANDIDATE_DOMAIN_AND_REMAINING_MACHINE_POOL'}
    return replace(event,metadata=metadata)


def refresh_event_reservations(state):
    """Rebuild outside reservations and overlay finite event availability.

    M3 and W2 belong to availability/reservation semantics.  They are not
    special CP constraints: regime generation and the canonical NoOverlap
    model consume these same authoritative reservation maps.
    """
    mutable=set(state['keys']); event=state.get('event'); semantic=event_semantic(event)
    machine=outside_machine_reservations(state['problem'],state['after_index'],mutable)
    worker=outside_worker_reservations(state['problem'],state['after_index'],mutable)
    start=int(getattr(event,'event_time_seconds',0) or 0)
    end=int(getattr(event,'end_time_seconds',start) or start)
    if semantic == 'M3_TEMPORARY_MACHINE_OUTAGE' and end > start:
        for mid in tuple(getattr(event,'machine_ids',()) or ()):
            machine.setdefault(int(mid),[]).append((start,end,'__EVENT__',f'event:{event.event_id}'))
            machine[int(mid)].sort(key=lambda x:(int(x[0]),int(x[1]),str(x[3])))
    if semantic == 'W2_WORKER_ABSENCE' and end > start:
        origin=state['problem'].origin
        left,right=origin+timedelta(seconds=start),origin+timedelta(seconds=end)
        for wid in tuple(getattr(event,'worker_ids',()) or ()):
            worker.setdefault(str(wid),[]).append((left,right))
            worker[str(wid)].sort()
    state['_machine_res']=machine; state['_worker_res']=worker
    return state
