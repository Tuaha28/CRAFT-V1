"""Canonical CP repair model for production and diagnostics."""
from collections import defaultdict
from ortools.sat.python import cp_model
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.disruptions.p2_compressed_timing_repair import _sec
from crafter.authoritative_semantics import authoritative_changeover_seconds, project_instant_seconds


def _boundary_seconds(value):
    """Project persisted floating boundary instants without moving work earlier."""
    return project_instant_seconds(float(value)).projected_seconds


def _committed_routing_end(state, key):
    """Exact end carried by an accepted prior rolling window, if present."""
    row = (state.get('committed_routing_completion') or {}).get(str(key))
    return None if row is None else int(row['authoritative_end_seconds'])


def _planning_fixed_outside(state, key):
    """Whether an external planning neighbor is an actual hard boundary.

    Repair states retain their established external-boundary behavior.  In an
    INITIAL/ROLLING planning state, however, a FUTURE_OUTSIDE_WINDOW operation
    is deliberately only context: its old benchmark placement cannot become a
    fake commitment.
    """
    if state.get('planning_context') is None:
        return True
    lifecycle = (state.get('classified', {}).get(str(key), {}).get('state'))
    return lifecycle in {'COMPLETED', 'IN_PROGRESS', 'COMMITTED_FUTURE', 'FROZEN_NEAR_TERM'}


def build_repair_model(state, regimes, *, include_internal_routing=True,
                       include_outside_routing=True,
                       include_internal_machine_no_overlap=True,
                       include_outside_machine_reservations=True,
                       include_internal_worker_no_overlap=True,
                       include_outside_worker_reservations=True,
                       include_changeover=True,
                       include_release_bounds=True,
                       include_event_bounds=True,
                       force_regime_indices=None,
                       exclude_routing_edges=None,
                       guard_outside_routing=False,
                       guard_internal_routing=False):
    """Build every repair/ablation variant from one authoritative builder."""
    p, idx = state['problem'], state['after_index']
    keys, horizon = sorted(state['keys']), int(state['problem'].horizon_seconds)
    model = cp_model.CpModel(); starts = {}; ends = {}; reg_lit = {}
    reg_info = {}; seg_info = {}; cfg_lit = {}; machine_int = defaultdict(list)
    worker_int = defaultdict(list); optional = 0
    routing_assumptions = []
    routing_assumption_map = {}
    routing_constraint_records = []
    event = state.get('event'); md = getattr(event, 'metadata', {}) or {}
    excluded_edges = set(exclude_routing_edges or ())
    delayed_pred = md.get('predecessor_operation_key')
    delayed_end = int(md.get('delayed_end_seconds', 0) or 0)
    for key in keys:
        rs = list(regimes.get(key, ()))
        if not rs: raise ValueError(f'zero regime domain {key}')
        starts[key] = model.NewIntVar(0, horizon, f'cr_start_{key}')
        ends[key] = model.NewIntVar(0, horizon, f'cr_end_{key}')
        lits, cfgs = [], defaultdict(list)
        for ri, r in enumerate(rs):
            lit = model.NewBoolVar(f'cr_reg_{key}_{ri}'); lits.append(lit)
            reg_info[(key, ri)] = r; cfgs[r['config']].append(lit)
            lo = int(r.get('lo', 0))
            if not include_release_bounds: lo = int(r.get('base_lo', 0))
            if not include_event_bounds and idx.route_predecessor.get(key) == delayed_pred:
                lo = int(r.get('base_lo', 0))
            model.Add(starts[key] >= lo).OnlyEnforceIf(lit)
            model.Add(starts[key] <= int(r.get('hi', horizon))).OnlyEnforceIf(lit)
            f = r['formula']; model.Add(ends[key] == int(f[0][0])*starts[key] + int(f[0][1])).OnlyEnforceIf(lit)
            base = r['base']; segs = []
            for si in range(int(r.get('segments', 0))):
                fs, fe = f[1+2*si], f[1+2*si+1]
                # These are exact affine expressions of the operation start.
                # Extra segment variables and a duplicate presence literal per
                # regime add no semantics, but previously multiplied the model
                # by every worker/segment alternative.  CP-SAT interval fields
                # accept one-variable affine expressions directly.
                st = int(fs[0])*starts[key] + int(fs[1])
                en = int(fe[0])*starts[key] + int(fe[1])
                sz = (int(fe[0])-int(fs[0]))*starts[key] + (int(fe[1])-int(fs[1]))
                for wid in base.worker_ids:
                    worker_int[str(wid)].append(model.NewOptionalIntervalVar(st, sz, en, lit, f'cr_w_{key}_{ri}_{si}_{wid}')); optional += 1
                segs.append({'start': st, 'end': en, 'lit': lit})
            seg_info[(key, ri)] = segs
            for mid in base.machine_ids:
                ef = f[0]
                machine_end = int(ef[0])*starts[key] + int(ef[1])
                machine_size = (int(ef[0])-1)*starts[key] + int(ef[1])
                machine_int[int(mid)].append(model.NewOptionalIntervalVar(starts[key], machine_size, machine_end, lit, f'cr_m_{key}_{ri}_{mid}')); optional += 1
        model.AddExactlyOne(lits); reg_lit[key] = lits; cfg_lit[key] = {}
        for cfg, ls in cfgs.items():
            c = model.NewBoolVar(f'cr_cfg_{key}_{len(cfg_lit[key])}'); model.Add(sum(ls) == c); cfg_lit[key][cfg] = c
        if force_regime_indices and key in force_regime_indices:
            model.Add(lits[int(force_regime_indices[key])] == 1)
    event_constraint_records = []
    if include_event_bounds and delayed_pred in ends and delayed_end:
        model.Add(ends[delayed_pred] >= delayed_end)
        event_constraint_records.append({'category':'EVENT_DELAYED_PREDECESSOR_END',
                                         'operation_key':str(delayed_pred),
                                         'lower_bound_seconds':delayed_end})
    for key in keys:
        pred, succ = idx.route_predecessor.get(key), idx.route_successor.get(key)
        # Membership dispatch is independent of family enablement.  An inside
        # successor must never fall through and be encoded as a fixed outside
        # boundary merely because internal routing is disabled diagnostically.
        if succ in keys:
            if include_internal_routing and (key, succ) not in excluded_edges:
                ct = model.Add(ends[key] <= starts[succ])
                routing_constraint_records.append({'category':'INTERNAL_ROUTING','edge':(key,succ),'direction':'INSIDE_TO_INSIDE'})
                if guard_internal_routing:
                    lit = model.NewBoolVar(f'cr_assume_route_internal_{key}_{succ}')
                    ct.OnlyEnforceIf(lit); model.AddAssumption(lit)
                    routing_assumptions.append(lit)
                    routing_assumption_map[lit.Index()] = {'category':'INTERNAL_ROUTING','predecessor':key,'successor':succ,'direction':'INSIDE_TO_INSIDE'}
        elif (succ is not None and include_outside_routing
              and _planning_fixed_outside(state, succ)
              and (key, succ) not in excluded_edges):
            a = next((x.assignment for x in state['baseline'].operations if x.assignment.operation_key == succ), None)
            if a is not None:
                boundary = _boundary_seconds(a.start_seconds)
                ct = model.Add(ends[key] <= boundary)
                routing_constraint_records.append({'category':'OUTSIDE_ROUTING','edge':(key,succ),'direction':'INSIDE_TO_OUTSIDE','fixed_boundary_seconds':boundary})
                if guard_outside_routing:
                    lit = model.NewBoolVar(f'cr_assume_route_out_successor_{key}_{succ}')
                    ct.OnlyEnforceIf(lit); model.AddAssumption(lit)
                    routing_assumptions.append(lit)
                    routing_assumption_map[lit.Index()] = {'category':'OUTSIDE_ROUTING','inside_operation':key,'outside_operation':succ,'direction':'INSIDE_TO_OUTSIDE','fixed_boundary_seconds':boundary}
        if (pred not in keys and pred is not None and include_outside_routing
                and _planning_fixed_outside(state, pred)
                and (pred, key) not in excluded_edges):
            a = next((x.assignment for x in state['baseline'].operations if x.assignment.operation_key == pred), None)
            carried = _committed_routing_end(state, pred)
            if carried is not None or a is not None:
                boundary = carried if carried is not None else _boundary_seconds(a.end_seconds)
                ct = model.Add(boundary <= starts[key])
                routing_constraint_records.append({'category':'OUTSIDE_ROUTING','edge':(pred,key),'direction':'OUTSIDE_TO_INSIDE','fixed_boundary_seconds':boundary})
                if guard_outside_routing:
                    lit = model.NewBoolVar(f'cr_assume_route_out_predecessor_{pred}_{key}')
                    ct.OnlyEnforceIf(lit); model.AddAssumption(lit)
                    routing_assumptions.append(lit)
                    routing_assumption_map[lit.Index()] = {'category':'OUTSIDE_ROUTING','inside_operation':key,'outside_operation':pred,'direction':'OUTSIDE_TO_INSIDE','fixed_boundary_seconds':boundary}
    mutable = set(keys); mr = state.get('_machine_res') or outside_machine_reservations(p, idx, mutable)
    wr = state.get('_worker_res') or outside_worker_reservations(p, idx, mutable)
    if include_outside_machine_reservations:
        for mid, rows in mr.items():
            for i, (a,b,_o,_owner) in enumerate(rows):
                if int(b)>int(a): machine_int[int(mid)].append(model.NewFixedSizeIntervalVar(int(a), int(b-a), f'cr_fm_{mid}_{i}'))
    if include_outside_worker_reservations:
        for wid, rows in wr.items():
            for i,(a,b) in enumerate(rows):
                s,e = _sec(p,a),_sec(p,b)
                if e>s: worker_int[str(wid)].append(model.NewFixedSizeIntervalVar(s,e-s,f'cr_fw_{wid}_{i}'))
    if include_internal_machine_no_overlap:
        for vals in machine_int.values(): model.AddNoOverlap(vals)
    if include_internal_worker_no_overlap:
        for vals in worker_int.values(): model.AddNoOverlap(vals)
    if include_changeover:
        for ai,left in enumerate(keys):
            for right in keys[ai+1:]:
                for lc,ll in cfg_lit[left].items():
                    for rc,rr in cfg_lit[right].items():
                        left_machines = lc.machine_ids if hasattr(lc, 'machine_ids') else lc[2]
                        right_machines = rc.machine_ids if hasattr(rc, 'machine_ids') else rc[2]
                        shared=set(left_machines) & set(right_machines)
                        if not shared: continue
                        bef=model.NewBoolVar(f'cr_b_{left}_{right}_{len(shared)}'); aft=model.NewBoolVar(f'cr_a_{left}_{right}_{len(shared)}')
                        model.Add(bef+aft >= ll+rr-1); model.Add(bef<=ll); model.Add(bef<=rr); model.Add(aft<=ll); model.Add(aft<=rr)
                        ch=int(authoritative_changeover_seconds(left.split('_')[0], right.split('_')[0]))
                        model.Add(ends[left]+ch<=starts[right]).OnlyEnforceIf(bef); model.Add(ends[right]+ch<=starts[left]).OnlyEnforceIf(aft)
    if ends:
        mx=model.NewIntVar(0,horizon,'cr_max_end'); model.AddMaxEquality(mx,list(ends.values()))
    model.ClearObjective()
    return {'model':model,'starts':starts,'ends':ends,'reg_lit':reg_lit,'reg_info':reg_info,'seg_info':seg_info,'cfg_lit':cfg_lit,
            'routing_assumptions':routing_assumptions,
            'routing_assumption_map':routing_assumption_map,
            'routing_constraint_records':routing_constraint_records,
            'event_constraint_records':event_constraint_records,
            'size':{'operations':len(keys),'regimes':sum(len(v) for v in regimes.values()),'variables':len(model.Proto().variables),'constraints':len(model.Proto().constraints),'optional_intervals':optional}}
