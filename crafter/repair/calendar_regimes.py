"""Exact fixed-crew calendar regimes derived from ResourceConfigurations.

The cache in this module stores only exact authoritative calculations.  Its
keys include the relevant reservation/calendar signature, so an outage,
absence, commitment, or changed boundary can never receive a stale result.
"""
from datetime import timedelta
from dataclasses import dataclass, field
import os
import time
import hashlib

from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.lns_probe.transaction import _machine_accepts


def _freeze_worker_reservations(state):
    return tuple(sorted((str(worker), tuple((str(a), str(b)) for a, b in rows))
                        for worker, rows in state.get('_worker_res', {}).items()))


def _freeze_machine_reservations(state):
    return tuple(sorted((int(machine), tuple(tuple(row) for row in rows))
                        for machine, rows in state.get('_machine_res', {}).items()))


@dataclass
class CalendarRegimeCache:
    """Versioned, immutable-value memoization for exact regime preparation."""
    execution: dict = field(default_factory=dict)
    breakpoints: dict = field(default_factory=dict)
    machine_acceptance: dict = field(default_factory=dict)
    crew_availability: dict = field(default_factory=dict)
    crew_availability_signature: object = None
    statistics: dict = field(default_factory=lambda: {
        'execution_hits': 0, 'execution_misses': 0, 'execution_seconds': 0.0,
        'breakpoint_hits': 0, 'breakpoint_misses': 0, 'breakpoint_seconds': 0.0,
        'machine_acceptance_hits': 0, 'machine_acceptance_misses': 0,
        'machine_acceptance_seconds': 0.0, 'invalidations': 0,
        'evictions': 0,
    })

    def snapshot(self):
        return dict(self.statistics)

    def prune_to_configurations(self, configurations):
        """Evict memoized values that cannot serve the replacement domain.

        This is deliberately cache-only bookkeeping.  The authoritative
        configurations and generated regimes are untouched; evicted entries
        are recomputed on demand with the same exact primitive.
        """
        self.crew_availability.clear()
        configs = [config for rows in (configurations or {}).values()
                   for config in (rows or ())]
        crew_signatures = {
            (tuple(config.worker_ids), int(config.setup_seconds),
             float(getattr(config, 'productive_minutes', 0.0) or 0.0),
             int(config.productive_seconds))
            for config in configs
        }
        config_signatures = {
            (tuple(config.machine_ids), tuple(config.worker_ids),
             int(config.setup_seconds))
            for config in configs
        }
        machine_ids = {int(machine) for config in configs
                       for machine in config.machine_ids}
        before = len(self.execution) + len(self.breakpoints) + len(self.machine_acceptance)
        self.execution = {
            key: value for key, value in self.execution.items()
            if len(key) >= 5 and key[1:5] in crew_signatures
        }
        self.breakpoints = {
            key: value for key, value in self.breakpoints.items()
            if len(key) >= 5 and key[2:5] in config_signatures
        }
        self.machine_acceptance = {
            key: value for key, value in self.machine_acceptance.items()
            if len(key) >= 2 and int(key[1]) in machine_ids
        }
        after = len(self.execution) + len(self.breakpoints) + len(self.machine_acceptance)
        evicted = max(0, before - after)
        self.statistics['evictions'] += evicted
        self.statistics['invalidations'] += evicted
        return evicted


def _calendar_signature(state):
    """Exact state signature for calendar-dependent calculations.

    The operator index identity is deliberately included.  A caller that can
    prove calendar identity across windows may supply a stable explicit
    ``calendar_cache_version``; otherwise separate rebuilt states cannot
    share calendar answers by accident.
    """
    explicit = state.get('calendar_cache_version')
    base = explicit if explicit is not None else id(state['operator_index'])
    return (base, state['problem'].origin.isoformat(), int(state['problem'].horizon_seconds),
            _freeze_worker_reservations(state), _freeze_machine_reservations(state))


def _worker_calendar_signature(state):
    explicit = state.get('worker_calendar_cache_version')
    base = explicit if explicit is not None else id(state['operator_index'])
    # Actual inputs of operator_available_at; in-place roster edits must also
    # invalidate answers even if a caller reused the index object identity.
    calendar_rows = tuple((name, tuple(sorted((repr(k), tuple(v)) for k, v in
                         state['operator_index'].get(name, {}).items())))
                         for name in ('crew_of', 'shift_of'))
    calendar_digest = hashlib.sha256(repr(calendar_rows).encode()).hexdigest()
    return (base, state['problem'].origin.isoformat(), _freeze_worker_reservations(state), calendar_digest)


def _sec(problem, value):
    return int(round((value-problem.origin).total_seconds()))


def _execution(state, config, start, cache=None, worker_signature=None):
    # Uncached replay needs no factory-wide cache signature. Exact execution
    # still receives the complete authoritative reservation map below.
    cache_key = None
    if cache is not None:
        worker_signature = worker_signature if worker_signature is not None else _worker_calendar_signature(state)
        cache_key=(worker_signature, tuple(config.worker_ids),
                   int(config.setup_seconds), float(getattr(config, 'productive_minutes', 0.0) or 0.0),
                   int(config.productive_seconds), int(start))
    if cache is not None and cache_key in cache.execution:
        cache.statistics['execution_hits'] += 1
        return cache.execution[cache_key]
    if cache is not None: cache.statistics['execution_misses'] += 1
    mark = time.perf_counter()
    ready = state['problem'].origin + timedelta(seconds=int(start + config.setup_seconds))
    productive_minutes = float(getattr(config, 'productive_minutes', 0.0) or 0.0)
    if productive_minutes <= 0:
        productive_minutes = float(config.productive_seconds) / 60.0
    availability_cache = None
    if cache is not None:
        if cache.crew_availability_signature != worker_signature:
            cache.crew_availability.clear()
            cache.crew_availability_signature = worker_signature
        availability_cache = cache.crew_availability
    end, segments = _fixed_crew_execution(state['problem'], state['operator_index'],
                                           config.worker_ids, ready,
                                           productive_minutes,
                                           state['_worker_res'], availability_cache=availability_cache)
    value=(_sec(state['problem'], end), tuple((_sec(state['problem'],a),_sec(state['problem'],b)) for a,b in segments))
    if cache is not None:
        cache.statistics['execution_seconds'] += time.perf_counter() - mark
        cache.execution[cache_key]=value
    return value


def _affine(samples):
    x0,v0=samples[0]; x1,v1=samples[1]; out=[]
    # A transition that changes the number of productive segments cannot be
    # represented by one CP regime. zip() would otherwise silently ignore the
    # extra segment and leave its worker occupancy unmodeled.
    if any(len(v) != len(v0) for _, v in samples): return None
    for a,b in zip(v0,v1):
        d=x1-x0; slope=(b-a)/d if d else 0
        if abs(slope-round(slope))>1e-9 or int(round(slope)) not in (0,1): return None
        s=int(round(slope)); out.append((s,int(a-s*x0)))
    if any(any(s*x+c != int(y) for (s,c),y in zip(out,v)) for x,v in samples[2:]): return None
    return tuple(out)


def _coalesce_regimes(regimes):
    """Merge only contiguous regimes with identical physical/time meaning."""
    merged=[]
    for r in sorted(regimes, key=lambda x: (repr(x['config']), int(x['lo']), int(x['hi']), repr(x['formula']))):
        if (merged and merged[-1]['config'] == r['config']
                and merged[-1]['formula'] == r['formula']
                and int(merged[-1]['hi']) == int(r['lo'])):
            merged[-1] = {**merged[-1], 'hi': int(r['hi'])}
        else:
            merged.append(r)
    return merged


def _breakpoints(state, key, config, lower, cache=None, calendar_signature=None,
                 additional_breakpoints=()):
    p=state['problem']; horizon=int(p.horizon_seconds)
    calendar_signature = calendar_signature if calendar_signature is not None else _calendar_signature(state)
    extra = tuple(sorted({int(value) for value in (additional_breakpoints or ())}))
    cache_key=(calendar_signature, str(key), tuple(config.machine_ids),
               tuple(config.worker_ids), int(config.setup_seconds), int(lower), extra,
               int(config.productive_seconds))
    if cache is not None and cache_key in cache.breakpoints:
        cache.statistics['breakpoint_hits'] += 1
        return cache.breakpoints[cache_key]
    if cache is not None: cache.statistics['breakpoint_misses'] += 1
    mark=time.perf_counter()
    upper=horizon-int(config.setup_seconds+config.productive_seconds)
    row=state['after_index'].by_key[key]
    values={int(lower),max(int(lower),upper),int(round(float(row.get('start_minutes',0))*60))}
    # Adaptive domain pricing can prove a specific route-ready start through
    # exact fixed-crew execution.  Add a one-second bracket around that point
    # so the canonical affine verifier can represent the legal slice instead
    # of rejecting a much wider interval because one distant endpoint is
    # blocked.  This adds no availability and changes no calendar semantics.
    for value in extra:
        values.update((int(value)-1, int(value), int(value)+1))
    for mid in config.machine_ids:
        for a,b,_order,_owner in state['_machine_res'].get(int(mid),()): values.update((int(a),int(b)))
    for wid in config.worker_ids:
        for a,b in state['_worker_res'].get(str(wid),()): values.update((_sec(p,a),_sec(p,b)))
    for x in list(values):
        dt=p.origin+timedelta(seconds=int(x)); day=dt.replace(hour=0,minute=0,second=0,microsecond=0)
        for offset in (-1,0,1):
            d=day+timedelta(days=offset)
            for hour in (6,14,22):
                q=_sec(p,d+timedelta(hours=hour))-int(config.setup_seconds)
                values.update((q-1,q,q+1))
                # Execution changes shape when productive work ends exactly
                # at a shift boundary, not only when it starts there. Without
                # this probe a wide mixed-shape interval can be discarded and
                # hide an otherwise legal successor start. These are probes,
                # not availability: exact fixed-crew verification still gates
                # every emitted regime (including pauses/reservations).
                finish_transition = q - int(config.productive_seconds)
                values.update((finish_transition-1, finish_transition, finish_transition+1))
    result=tuple(sorted(x for x in values if int(lower)<=x<=upper))
    if cache is not None:
        cache.statistics['breakpoint_seconds'] += time.perf_counter()-mark
        cache.breakpoints[cache_key]=result
    return result


def _machine_accepts_cached(state, machine_id, start, end, order_id, cache, calendar_signature=None):
    reservations=state['_machine_res'].get(int(machine_id), ())
    calendar_signature = calendar_signature if calendar_signature is not None else _calendar_signature(state)
    cache_key=(calendar_signature, int(machine_id), int(start), int(end), str(order_id))
    if cache_key in cache.machine_acceptance:
        cache.statistics['machine_acceptance_hits'] += 1
        return cache.machine_acceptance[cache_key]
    cache.statistics['machine_acceptance_misses'] += 1
    mark=time.perf_counter()
    value=_machine_accepts(reservations, start, end, order_id)
    cache.statistics['machine_acceptance_seconds'] += time.perf_counter()-mark
    cache.machine_acceptance[cache_key]=value
    return value


def generate_calendar_regimes(state, configurations, *, cache=None,
                              additional_breakpoints_by_operation=None,
                              operation_keys=None):
    regimes, summary, total, checks = {}, [], 0, []
    cache = cache or CalendarRegimeCache()
    calendar_signature = _calendar_signature(state)
    worker_signature = _worker_calendar_signature(state)
    event=state.get('event'); md=getattr(event,'metadata',{}) or {}
    delayed=str(md.get('predecessor_operation_key','')); delayed_end=int(md.get('delayed_end_seconds',0) or 0)
    keys = tuple(sorted(str(key) for key in (
        state['keys'] if operation_keys is None else operation_keys)))
    # SchedulingProblem lookup properties construct dictionaries. Keep them
    # local to this immutable generation call, not inside every sample/machine
    # check. No cross-state cache or resource-domain filtering is introduced.
    operations = state['problem'].operation_by_key
    orders = state['problem'].order_by_id
    for key in keys:
        operation_started=time.perf_counter()
        operation_stats=cache.snapshot()
        order_id = operations[key].order_id
        lower=max(0,int(orders[order_id].release_seconds))
        if state['after_index'].route_predecessor.get(key)==delayed: lower=max(lower,delayed_end)
        rows=[]
        extra = (additional_breakpoints_by_operation or {}).get(str(key), ())
        for config in configurations.get(key,()):
            bp=_breakpoints(state,key,config,lower,cache,calendar_signature,
                            additional_breakpoints=extra)
            for a,b in zip(bp,bp[1:]):
                if b<a: continue
                xs=sorted(set((a,b,(a+b)//2,a+1 if a<b else a,b-1 if b>a else b)))
                vals=[]; exact=[]
                try:
                    for x in xs:
                        end,segs=_execution(state,config,x,cache,worker_signature)
                        if end>state['problem'].horizon_seconds: raise ValueError('horizon')
                        if not all(_machine_accepts_cached(state,mid,x,end,order_id,cache,calendar_signature) for mid in config.machine_ids): raise ValueError('machine boundary')
                        vector=(end,)+tuple(v for pair in segs for v in pair); vals.append((x,vector)); exact.append((x,end,segs))
                except Exception:
                    continue
                formula=_affine(vals)
                if formula is None:
                    # Before the shape guard, only transitions whose common
                    # vector prefix was affine could enter the CP domain.
                    # Split precisely those previously exposed transitions;
                    # do not widen the domain to every other non-affine
                    # one-second sample in this performance-sensitive path.
                    vector_lengths = ({len(vector) for _, vector in vals}
                                      if b-a == 1 else set())
                    prefix_affine = (_affine([(x, vector[:min(vector_lengths)])
                                               for x, vector in vals])
                                     if len(vector_lengths) > 1 else None)
                    if prefix_affine is not None:
                        # Both integer starts are legal and exactly evaluated,
                        # but they cross a calendar-shape transition. Preserve
                        # each as a point regime instead of approximating or
                        # silently deleting an otherwise legal alternative.
                        for x,end,segs in exact:
                            vector=(end,)+tuple(v for pair in segs for v in pair)
                            point_formula=tuple((0,int(value)) for value in vector)
                            rows.append({'key':key,'config':config,'base':config,
                                         'lo':x,'base_lo':lower,'hi':x,
                                         'formula':point_formula,'segments':len(segs)})
                            checks.append({'operation_key':key,'start_points':[x],
                                           'equivalent':True})
                    continue
                nseg=(len(vals[0][1])-1)//2
                regime={'key':key,'config':config,'base':config,'lo':a,'base_lo':lower,'hi':b,'formula':formula,'segments':nseg}
                rows.append(regime)
                checks.append({'operation_key':key,'start_points':xs,'equivalent':all(formula[0][0]*x+formula[0][1]==end and [tuple((formula[1+2*i][0]*x+formula[1+2*i][1],formula[2+2*i][0]*x+formula[2+2*i][1])) for i in range(nseg)]==list(segs) for x,end,segs in exact)})
        seen=set(); clean=[]
        for r in rows:
            sig=(r['config'],r['lo'],r['hi'],r['formula'])
            if sig not in seen: seen.add(sig); clean.append(r)
        # Breakpoints are deliberately conservative: reservation and shift
        # boundaries are sampled even when they do not change this particular
        # configuration's execution mapping.  Keeping every resulting slice
        # as a separate CP alternative creates a large amount of pure
        # symmetry.  Adjacent closed intervals with the same configuration and
        # the exact same affine execution formula are one regime semantically,
        # so coalesce them.  Never bridge a gap (a rejected interval), and
        # never merge different formulas/resources.
        before_coalescing=time.perf_counter()
        merged=_coalesce_regimes(clean)
        after_coalescing=time.perf_counter()
        regimes[key]=merged; total+=len(merged)
        now_stats=cache.snapshot()
        operation_profile={name: now_stats[name]-operation_stats[name] for name in now_stats}
        summary.append({'operation_key':key,
                        'resource_configurations':len(configurations.get(key,())),
                        'raw_regime_count':len(clean),
                        'regime_count':len(merged),
                        'coalesced_regimes':len(clean)-len(merged),
                        'raw_generation_seconds':before_coalescing-operation_started,
                        'coalescing_seconds':after_coalescing-before_coalescing,
                        'total_generation_seconds':after_coalescing-operation_started,
                        'calendar_profile':operation_profile,
                        'status':'OK' if merged else 'ZERO_REGIME_DOMAIN'})
    return regimes, summary, total, {
        'checks':checks,'mismatches':sum(1 for x in checks if not x['equivalent']),
        'all_equivalent':all(x['equivalent'] for x in checks),
        'cache_profile':cache.snapshot(),
    }


def generate_calendar_regimes_selected(state, configurations, **kwargs):
    """Experimental dispatch; V1 remains the default/control engine."""
    if str(os.environ.get('CRAFT_CALENDAR_ENGINE_V2', '0')).strip().lower() in {
            '1', 'true', 'yes', 'on'}:
        from .calendar_engine_v2 import generate_calendar_regimes_v2
        return generate_calendar_regimes_v2(state, configurations, **kwargs)
    return generate_calendar_regimes(state, configurations, **kwargs)


class IncrementalCalendarRegimeBuilder:
    """Operation-granular exact regime reuse within one immutable state.

    The builder is intentionally scoped to one planning state.  An operation
    is reused only when its complete ordered ResourceConfiguration tuple and
    authoritative additional-breakpoint tuple are identical.  Any changed
    crew, machine set, readiness probe, boundary, calendar, or horizon creates
    a fresh builder/signature and therefore forces canonical regeneration.
    """

    def __init__(self, state, *, cache=None):
        self.state = state
        self.cache = cache or CalendarRegimeCache()
        self._entries = {}
        self.statistics = {
            'operation_hits': 0,
            'operation_misses': 0,
            'regimes_reused': 0,
            'regimes_generated_new': 0,
            'build_seconds': 0.0,
        }

    @staticmethod
    def _signature(configurations, probes):
        # ResourceConfiguration is frozen/hashable.  Retaining the full value
        # avoids a lossy cache signature and makes semantic equality explicit.
        return (tuple(configurations), tuple(sorted({int(x) for x in probes})))

    def seed(self, configurations, regimes, summary=(), equivalence=None,
             additional_breakpoints_by_operation=None):
        summary_by_key = {str(row.get('operation_key')): dict(row)
                          for row in (summary or ())}
        checks_by_key = {}
        for row in ((equivalence or {}).get('checks') or ()):
            checks_by_key.setdefault(str(row.get('operation_key')), []).append(dict(row))
        probes = additional_breakpoints_by_operation or {}
        for key in sorted(str(x) for x in self.state['keys']):
            values = tuple(configurations.get(key, ()))
            signature = self._signature(values, probes.get(key, ()))
            rows = tuple(regimes.get(key, ()))
            self._entries[key] = {
                'signature': signature,
                'regimes': rows,
                'summary': summary_by_key.get(key, {
                    'operation_key': key,
                    'resource_configurations': len(values),
                    'raw_regime_count': len(rows),
                    'regime_count': len(rows),
                    'coalesced_regimes': 0,
                    'raw_generation_seconds': 0.0,
                    'coalescing_seconds': 0.0,
                    'total_generation_seconds': 0.0,
                    'status': 'OK' if rows else 'ZERO_REGIME_DOMAIN',
                }),
                'checks': tuple(checks_by_key.get(key, ())),
            }

    def build(self, configurations, *, additional_breakpoints_by_operation=None):
        started = time.perf_counter()
        probes = additional_breakpoints_by_operation or {}
        hits = []
        misses = []
        signatures = {}
        for key in sorted(str(x) for x in self.state['keys']):
            signature = self._signature(
                tuple(configurations.get(key, ())), probes.get(key, ()))
            signatures[key] = signature
            entry = self._entries.get(key)
            if entry is not None and entry['signature'] == signature:
                hits.append(key)
            else:
                misses.append(key)

        # Operation-granular reuse is valuable for a small priced addition.
        # When most operations changed, one canonical bulk pass is materially
        # cheaper than many partial bookkeeping paths and produces the exact
        # same domain.  It still uses the shared exact primitive cache.
        bulk_rebuild = bool(misses and len(misses) * 2 > max(1, len(signatures)))
        generation_keys = list(signatures) if bulk_rebuild else misses
        if generation_keys:
            partial_configs = {key: tuple(configurations.get(key, ())) for key in generation_keys}
            partial_regimes, partial_summary, _total, partial_equivalence = generate_calendar_regimes_selected(
                self.state, partial_configs, cache=self.cache,
                additional_breakpoints_by_operation=probes,
                operation_keys=generation_keys)
            summary_by_key = {str(row['operation_key']): dict(row)
                              for row in partial_summary}
            checks_by_key = {}
            for row in partial_equivalence.get('checks', ()):
                checks_by_key.setdefault(str(row.get('operation_key')), []).append(dict(row))
            for key in generation_keys:
                rows = tuple(partial_regimes.get(key, ()))
                self._entries[key] = {
                    'signature': signatures[key],
                    'regimes': rows,
                    'summary': summary_by_key[key],
                    'checks': tuple(checks_by_key.get(key, ())),
                }

        regimes = {}
        summary = []
        checks = []
        for key in sorted(str(x) for x in self.state['keys']):
            entry = self._entries[key]
            regimes[key] = entry['regimes']
            summary.append(dict(entry['summary']))
            checks.extend(dict(row) for row in entry['checks'])
        reused = sum(len(regimes[key]) for key in hits)
        effective_hits = [] if bulk_rebuild else hits
        effective_misses = generation_keys
        reused = sum(len(regimes[key]) for key in effective_hits)
        generated = sum(len(regimes[key]) for key in effective_misses)
        self.statistics['operation_hits'] += len(effective_hits)
        self.statistics['operation_misses'] += len(effective_misses)
        self.statistics['regimes_reused'] += reused
        self.statistics['regimes_generated_new'] += generated
        elapsed = time.perf_counter() - started
        self.statistics['build_seconds'] += elapsed
        equivalence = {
            'checks': checks,
            'mismatches': sum(1 for row in checks if not row.get('equivalent')),
            'all_equivalent': all(row.get('equivalent') for row in checks),
            'cache_profile': self.cache.snapshot(),
            'incremental_profile': {
                'operation_hits': len(effective_hits),
                'operation_misses': len(effective_misses),
                'regimes_reused': reused,
                'regimes_generated_new': generated,
                'build_seconds': elapsed,
            },
        }
        if bulk_rebuild:
            equivalence['incremental_profile']['bulk_rebuild'] = True
        return regimes, summary, sum(len(rows) for rows in regimes.values()), equivalence
