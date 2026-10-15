"""
Receding-horizon (overlapping-lookahead) rolling scheduler.

WHY THIS EXISTS. The original rolling-horizon scheduler solved strict,
non-overlapping windows: optimise orders[i:i+C], commit all of it, slide by C,
repeat -- zero visibility into what demand is coming right after the window
closes. Measured on the full order book (see PROJECT_STATUS.txt): on-time
collapses across later windows, 100% -> 99.3% -> 80.7% -> 25.3% -> 6.3%. That
is not a capacity wall (the achievable ceiling was independently verified at
100% for this order book) -- it is each window committing machines with no
idea that the very next window's orders will want the same pools.

Standard fix, textbook receding/moving-horizon control: solve over a
LOOKAHEAD wider than what gets locked in, but only COMMIT the near-term
slice, then slide forward by the commit size (not the lookahead size) so the
overlap gets re-optimised with fresh information every round.

    commit_size (C):    orders actually locked into the schedule and carried
                        forward as committed machine state. This is what
                        "300-order horizon" meant before this module existed.
    lookahead_size (L): orders the GA can SEE while deciding, L >= C.
                        L == C (the default) recovers the original
                        non-overlapping behaviour exactly -- nothing else in
                        the pipeline has to change unless a wider L is
                        deliberately chosen.

Every iteration re-optimises the full lookahead from scratch (a fresh GA
population, same seed every time for reproducibility); only the carried
machine state persists across iterations. The overlap between one iteration's
lookahead and the next is the point: orders seen but not committed this round
are seen again next round, with a full generation budget, alongside whatever
newly-released orders have entered the lookahead by then.
"""

import time

from deap import creator
from ga.checkpointing import save as save_checkpoint, load as load_checkpoint, restore_rng_state, decode_evolution

from ga.scheduler import GAScheduler, make_key
from ga.fitness import evaluate_schedule
from ga.heuristics import baseline_chromosomes


def extract_committed_subschedule(chromosome, num_ops, committed_order_ids):
    """
    Filters a solved chromosome down to only the operations belonging to
    committed_order_ids, preserving their relative order.

    Precedence stays valid with no repair needed: deleting positions never
    reorders the ones that remain, so an order's own operations -- already in
    routing order from the GA's own precedence repair -- stay in routing
    order in the filtered sequence too.

    Line and machine-count decisions for committed operations are kept
    exactly as the GA chose them WITH THE FULL LOOKAHEAD VISIBLE -- that
    foresight is the entire purpose of this module. Only which operations
    get carried into the export and into next round's machine state changes.

    Returns (filtered_chromosome, filtered_num_ops). filtered_num_ops may be
    0 if none of the lookahead's operations belong to a committed order,
    which should not normally happen but is handled by the caller.
    """
    os_array = chromosome[:num_ops]
    la_array = chromosome[num_ops:2 * num_ops]
    mc_array = chromosome[2 * num_ops:]

    idxs = [i for i, op_key in enumerate(os_array)
            if op_key.split('_')[0] in committed_order_ids]

    new_os = [os_array[i] for i in idxs]
    new_la = [la_array[i] for i in idxs]
    new_mc = [mc_array[i] for i in idxs]
    return new_os + new_la + new_mc, len(new_os)


def run_rolling_horizon(routings, df_machines, df_lines, all_preds, capacity, origin,
                        commit_size=300, lookahead_size=None,
                        pop_size=50, generations=50, polish_evals=400, seed=42,
                        export=False, decision_engine=None, operator_index=None,
                        log_fn=print,
                        release_gate=None, manager_prioritized_orders=None, ownership_snapshot=None, legacy_mode=None, initial_operator_reservations=None,
                        committed_machine_state=None,
                        committed_machine_last_order=None,
                        export_label_prefix=None, checkpoint_dir=None, checkpoint_identity=None, resume_checkpoint=None):
    """
    Runs the full order book through consecutive lookahead windows, sliding
    by commit_size each round, and returns aggregate + per-window results.

    export=True additionally writes an operational schedule JSON for EVERY
    committed window (not only the first), via decision_engine's exporter,
    and rolls up the labour-by-shift / labour-by-sourcing breakdowns those
    exports compute -- this is what the standing evaluation report reads.
    export=False skips JSON serialization only. Whenever operator_index is
    supplied, worker assignment and segmented elapsed execution remain inside
    authoritative fitness; omitting export cannot switch the problem back to
    worker-blind timing.

    release_gate: optional, a pre-constructed ga.release_gate.ReleaseGate
        (WLC_RELEASE_GATE_PROPOSAL.md). None (the default) reproduces the
        original behaviour EXACTLY -- pure release-date-order positional
        slicing through `all_orders`, unchanged. When provided, each
        round's commit batch is instead decided by the gate's workload-norm
        admission (feasibility/starvation/manager-bypass all handled
        inside the gate itself -- see ga/release_gate.py). Gated mode
        forces lookahead_size == commit_size (L==C) -- combining an active
        gate with an independently wider lookahead is untested and
        deliberately out of scope for now (WLC_RELEASE_GATE_PROPOSAL.md
        Section 5); `lookahead_size` is ignored if passed alongside a gate.
    manager_prioritized_orders: optional list/set of order_ids, threaded
        straight through to every window's GAScheduler (the existing
        in-window fuzzy-urgency sequencing boost, ga/scheduler.py) exactly
        as agent/decision_engine.py's single-pass path already does.
        PREVIOUSLY MISSING FROM THIS FUNCTION ENTIRELY -- the rolling-
        horizon path (what every multi-window measurement in this project
        has used) silently ignored manager priority before this parameter
        existed. Independent of release_gate's own manager bypass (which
        only affects ADMISSION timing); both should normally be given the
        same order_id set.
    """
    operator_reservations = initial_operator_reservations
    if operator_index is not None and operator_reservations is None:
        from database.queries import fetch_committed_operator_reservations
        operator_reservations = fetch_committed_operator_reservations(origin)
    lookahead_size = lookahead_size or commit_size
    if release_gate is not None:
        lookahead_size = commit_size
    elif lookahead_size < commit_size:
        raise ValueError(
            f"lookahead_size ({lookahead_size}) must be >= commit_size ({commit_size})"
        )

    from utils.factory_ownership import resolve_ownership
    ownership_snapshot = resolve_ownership(routings['production_order_id'], ownership_snapshot, legacy_mode=legacy_mode)
    order_release = (
        routings.groupby('production_order_id')['release_date'].min()
        .reset_index().sort_values(['release_date', 'production_order_id'])
    )
    all_orders = order_release['production_order_id'].tolist()

    # CONSISTENCY WITH THE WORKER SIDE (P1). This was {} -- machines started
    # completely idle while workers started fully booked for the OLD plan, so
    # the same operation was simultaneously free on its machine, reserved on
    # its worker, and pending as demand. Under policy (a) both resources hold
    # work already under way and release work that is merely planned.
    machine_free, machine_last_order = {}, {}
    if committed_machine_state is None:
        from database.queries import fetch_committed_machine_state
        committed_machine_state, loaded_last_order = fetch_committed_machine_state(origin)
        if committed_machine_last_order is None:
            committed_machine_last_order = loaded_last_order
    machine_free.update(committed_machine_state)
    machine_last_order.update(committed_machine_last_order or {})
    lateness = {}
    # Same accumulation pattern as `lateness` above: each window's
    # GAScheduler only knows about that window's own orders, so these merge
    # across windows into one whole-book, one-origin-relative view. Lets a
    # caller compute real in-flight-order-count-over-time (release <= t <=
    # completion) from actual GA output, not a re-derivation -- see
    # GA_AUDIT.md Section 5/6.
    releases = {}
    completions = {}
    windows = []
    export_summaries = []
    margins = []
    representative_logbook = None
    all_logbooks = []
    total_labour_minutes = 0.0

    pos, w = 0, 0
    # Gated mode tracks TWO separate clocks, deliberately not one --
    # conflating them was a real bug caught by
    # tests/test_release_gate.py::test_accumulation_still_respects_the_norm_across_the_whole_batch
    # before it reached real data:
    #   real_now_mins: how far real simulated time has actually
    #     progressed (max(machine_free.values())). STARVATION is always
    #     measured against this, and it only advances between OUTER
    #     rounds (once a real GA round has actually run) OR when the
    #     accumulation below is genuinely stuck (see the inner loop).
    #   scan_time_mins: how far ahead the accumulation below is currently
    #     looking for MORE release-eligible orders to gather into this
    #     round's batch. Freely advances during accumulation -- if this
    #     were also used for starvation, pure lookahead scanning would
    #     manufacture false starvation and let one force-admitted order
    #     cascade into force-admitting the rest of the norm-blocked batch
    #     too, defeating the norm for the whole round.
    real_now_mins = 0.0
    run_t0 = time.time()
    while (release_gate.has_pending() if release_gate is not None else pos < len(all_orders)):
        if release_gate is not None:
            real_now_mins = max(real_now_mins, max(machine_free.values()) if machine_free else 0.0)
            # ACCUMULATE across several admit_next_batch calls up to
            # commit_size orders, rather than handing GAScheduler whatever
            # a single call returns. A single window/round rebuilds a
            # fresh ChromosomeFactory (fixed cost regardless of batch
            # size) and runs a full GA pass -- with real release dates
            # this sparse (GA_AUDIT.md Section 8), a single
            # admit_next_batch call can return as few as 5-10 orders once
            # the day-0 backlog clears, which would mean hundreds of GA
            # rounds to clear the book. This does NOT change which orders
            # get admitted or the guarantees ReleaseGate itself provides
            # (feasibility/starvation/manager-bypass, still fully
            # enforced per call, including norm-checking against
            # everything accumulated so far THIS round, since already-
            # admitted orders are already counted outstanding) -- it only
            # changes how many admission decisions get batched into one
            # GA-scheduling round. A safety cap on inner iterations
            # guarantees this cannot spin forever even if norm/starvation
            # interact pathologically.
            admitted_ids = []
            scan_time_mins = real_now_mins
            inner_rounds = 0
            while len(admitted_ids) < commit_size and release_gate.has_pending():
                inner_rounds += 1
                if inner_rounds > 10_000:
                    break  # matches the outer loop's own termination proof; belt-and-suspenders
                batch = release_gate.admit_next_batch(
                    max_batch=commit_size - len(admitted_ids),
                    current_time_mins=scan_time_mins,
                    starvation_reference_mins=real_now_mins)
                if batch:
                    admitted_ids.extend(batch)
                    continue
                next_release = release_gate.next_release_mins()
                if next_release is not None and next_release > scan_time_mins:
                    # A genuine future order to look ahead to -- fine to
                    # keep accumulating, real_now_mins is untouched.
                    scan_time_mins = next_release
                    continue
                # Nothing new becomes visible by scanning further, and
                # nothing currently eligible fits the norm or has REALLY
                # starved yet -- genuinely stuck at real_now_mins. Force
                # real progress (same fallback the original, non-
                # accumulating design used), which also raises the
                # starvation reference for anything already gathered
                # this round to be re-checked against on the next
                # inner iteration.
                real_now_mins += release_gate.starvation_mins + 1.0
                scan_time_mins = max(scan_time_mins, real_now_mins)
            if not admitted_ids:
                continue
            commit_ids = set(admitted_ids)
            lookahead_ids = commit_ids  # L==C enforced above when release_gate is provided
            sub = routings[routings['production_order_id'].astype(str).isin(commit_ids)]
        else:
            # lookahead_ids stays in the routings column's native dtype (whatever
            # pandas gave production_order_id) for the .isin() filter below.
            # commit_ids is stringified separately: extract_committed_subschedule
            # compares against op_key.split('_')[0], which is ALWAYS a string
            # (op_key is built by make_key as an f-string) regardless of the
            # source column's dtype. Comparing native ints against those strings
            # silently matched nothing — every window read "nothing to commit"
            # until this was caught by a smoke test before the real sweep ran.
            lookahead_ids = set(all_orders[pos:pos + lookahead_size])
            commit_ids = {str(x) for x in all_orders[pos:pos + commit_size]}
            sub = routings[routings['production_order_id'].isin(lookahead_ids)]

        w += 1
        keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
        preds = {k: v for k, v in all_preds.items() if k in keys}

        t0 = time.time()
        sched = GAScheduler(sub, df_machines, df_lines, preds,
                            ownership_snapshot=ownership_snapshot, legacy_mode=legacy_mode,
                            current_time=origin, present_operator_count=capacity,
                            initial_machine_free=machine_free,
                            initial_machine_last_order=machine_last_order,
                            manager_prioritized_orders=manager_prioritized_orders,
                            # The commit-weighted objective fix: score only
                            # the orders this window will actually commit,
                            # not the whole lookahead used for realistic
                            # machine contention. See scored_order_ids in
                            # ga/fitness.py and PROJECT_STATUS.txt Section 0B.
                            scored_order_ids=commit_ids,
                            operator_index=operator_index,
                            initial_operator_reservations=operator_reservations)
        # EVERY window's logbook, not only the first: 'representative_logbook'
        # below keeps its original meaning (window 1) for existing callers,
        # but a convergence claim made from one window is a claim about one
        # window. Kept as plain dicts so the result stays JSON-serialisable.
        expected_identity = dict(checkpoint_identity or {})
        expected_identity.update({'window': w, 'origin': origin.isoformat() if hasattr(origin, 'isoformat') else str(origin),
                                  'commit_size': commit_size, 'lookahead_size': lookahead_size, 'seed': seed})
        resumed = load_checkpoint(resume_checkpoint, expected_identity) if resume_checkpoint else None
        if resumed:
            restore_rng_state(resumed['rng_state'])
            if resumed['ga_state'].get('evolution_state') is not None:
                sched.restored_evolution_state = decode_evolution(resumed['ga_state']['evolution_state'])
        if resumed and resumed['phase'] == 'post_polish':
            genes = resumed['ga_state']['polished_chromosome']
            polished = creator.Individual(genes)
            polished.fitness.values = (resumed['ga_state']['polished_fitness'],)
            sols, logbook, pstats = [(resumed['ga_state']['selected_chromosome'], resumed['ga_state']['best_fitness'])], [], resumed['ga_state'].get('polish_stats', {})
        else:
            if resumed and resumed['phase'] == 'pre_polish':
                genes = resumed['ga_state']['selected_chromosome']
                sols, logbook = [(genes, resumed['ga_state']['best_fitness'])], []
            else:
                sols, logbook = sched.optimize(pop_size=pop_size, generations=generations,
                                               n_solutions=1, random_seed=seed)
            pre_fitness, pre_details = evaluate_schedule(sols[0][0], sched.num_ops, sched.op_details, sched.gnn_predictions,
                all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes,
                machine_pools=sched.factory.machine_ids_per_line_map, machine_line_map=sched.machine_line_map,
                usable_machine_count=sched.usable_machine_count, present_operator_count=sched.present_operator_count,
                op_sequence_map=sched.seq_lookup, initial_machine_free=machine_free,
                initial_machine_last_order=machine_last_order, op_optype=sched.factory.optype_for_op,
                protected_machine_type=sched.protected_machine_type, base_time=sched.now,
                worker_availability=sched.worker_availability, operator_index=operator_index,
                initial_operator_reservations=operator_reservations, return_details=True)
            if checkpoint_dir and not resumed:
                save_checkpoint(checkpoint_dir, f'window_{w:03d}_pre_polish_checkpoint', {
                    'phase':'pre_polish','identity':expected_identity,'rolling_state':{'position':pos,'window':w,'commit_ids':sorted(commit_ids),'remaining_orders':[str(x) for x in all_orders[pos:]]},
                    'ga_state':{'selected_chromosome':list(sols[0][0]),'best_fitness':float(pre_fitness[0]),'phenotype':pre_details['operations'],'logbook':[dict(x) for x in logbook],'evaluations':sum(int(x.get('nevals',0)) for x in logbook),'evolution_state':getattr(sched, 'last_evolution_state', None)},
                    'resource_state':{'machine_free':machine_free,'machine_last_order':machine_last_order,'operator_reservations':operator_reservations}})
            polished, pstats = sched.polish(sols[0][0], max_evals=polish_evals, random_seed=seed)
            if checkpoint_dir:
                post_f, post_det = evaluate_schedule(polished, sched.num_ops, sched.op_details, sched.gnn_predictions,
                    all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes, machine_pools=sched.factory.machine_ids_per_line_map, machine_line_map=sched.machine_line_map,
                    usable_machine_count=sched.usable_machine_count, present_operator_count=sched.present_operator_count, op_sequence_map=sched.seq_lookup,
                    initial_machine_free=machine_free, initial_machine_last_order=machine_last_order, op_optype=sched.factory.optype_for_op, protected_machine_type=sched.protected_machine_type,
                    base_time=sched.now, worker_availability=sched.worker_availability, operator_index=operator_index, initial_operator_reservations=operator_reservations, return_details=True)
                save_checkpoint(checkpoint_dir, f'window_{w:03d}_post_polish_checkpoint', {
                    'phase':'post_polish','identity':expected_identity,'rolling_state':{'position':pos,'window':w,'commit_ids':sorted(commit_ids),'remaining_orders':[str(x) for x in all_orders[pos:]]},
                    'ga_state':{'selected_chromosome':list(sols[0][0]),'best_fitness':float(pre_fitness[0]),'phenotype':pre_details['operations'],'polished_chromosome':list(polished),'polished_fitness':float(post_f[0]),'polish_stats':dict(pstats),'polished_phenotype':post_det['operations'],'logbook':[dict(x) for x in logbook],'evaluations':sum(int(x.get('nevals',0)) for x in logbook),'evolution_state':getattr(sched, 'last_evolution_state', None)},
                    'resource_state':{'machine_free':machine_free,'machine_last_order':machine_last_order,'operator_reservations':operator_reservations}})
        if representative_logbook is None:
            representative_logbook = list(logbook)
        all_logbooks.append({'window': w, 'records': [dict(r) for r in logbook]})

        # optimize() already computes and stores this (to seed the population
        # with the best heuristic) — read it rather than calling
        # sched.run_baselines() again, which would just re-evaluate the same
        # four dispatching rules a second time for nothing.
        baselines = getattr(sched, 'baseline_results', None) or {}

        polished, pstats = sched.polish(sols[0][0], max_evals=polish_evals, random_seed=seed)

        committed_chromo, committed_num_ops = extract_committed_subschedule(
            polished, sched.num_ops, commit_ids)
        if committed_num_ops == 0:
            log_fn(f"  window {w}: nothing to commit (empty intersection) — advancing.")
            if release_gate is None:
                pos += commit_size
            else:
                # Shouldn't happen (sub is built directly from admitted_ids
                # in gated mode) -- if it ever does, these orders are
                # already marked admitted in the gate with no completion,
                # so they'd sit as permanent phantom outstanding load.
                # Explicit so this can never fail silently.
                log_fn(f"  WARNING: release-gate-admitted orders produced zero "
                       f"committed operations: {sorted(commit_ids)}")
            continue

        # Captured BEFORE evaluate_schedule()'s own carried-forward params
        # get overwritten below by this window's END state -- the exporter
        # needs the same START-of-window state evaluate_schedule() was just
        # given, not what it produced, or it would double-count this
        # window's own placements as pre-existing busy time.
        window_start_machine_free = machine_free
        window_start_machine_last_order = machine_last_order
        window_start_operator_reservations = operator_reservations

        _f, det = evaluate_schedule(
            committed_chromo, committed_num_ops, sched.op_details, sched.gnn_predictions,
            all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes,
            order_release_minutes=sched.order_release_minutes,
            machine_pools=sched.factory.machine_ids_per_line_map,
            machine_line_map=sched.machine_line_map,
            usable_machine_count=sched.usable_machine_count,
            present_operator_count=sched.present_operator_count,
            op_sequence_map=sched.seq_lookup,
            initial_machine_free=window_start_machine_free,
            initial_machine_last_order=window_start_machine_last_order, return_details=True,
            # Same protection this window's GA search actually scored under
            # (sched.protected_machine_type is fixed for the whole window,
            # computed once at GAScheduler construction) -- omitting it here
            # would measure the committed subschedule under different
            # machine-selection rules than the ones that won the search.
            op_optype=sched.factory.optype_for_op,
            protected_machine_type=sched.protected_machine_type,
            # Same origin and availability predicate the scheduler scores
            # with, so the committed span equals the scored span. Omitting
            # these would score the window on one calendar and commit it on
            # another -- the "exported plan is not the plan that was scored"
            # failure this codebase has already paid for once.
            base_time=sched.now,
            worker_availability=sched.worker_availability,
            operator_index=operator_index,
            initial_operator_reservations=window_start_operator_reservations)

        machine_free = det['final_machine_free']
        machine_last_order = det['final_machine_last_order']
        operator_reservations = det['final_operator_reservations']
        # Durable post-polish/commit boundary: exact next-window resource state.
        if checkpoint_dir:
            save_checkpoint(checkpoint_dir, f'window_{w:03d}_post_commit_checkpoint', {
                'phase':'post_polish','identity':expected_identity,
                'rolling_state':{'position':pos, 'next_position':pos + (commit_size if release_gate is None else 0), 'window':w,
                                 'commit_ids':sorted(commit_ids),'remaining_orders':[str(x) for x in all_orders[pos + commit_size:] ]},
                'ga_state':{'selected_chromosome':list(sols[0][0]), 'best_fitness':float(sols[0][1]),
                            'polished_chromosome':list(polished), 'polished_fitness':float(_f[0]),
                            'phenotype':det['operations'], 'polish_stats':dict(pstats),
                            'evolution_state':getattr(sched, 'last_evolution_state', None), 'logbook':[dict(x) for x in logbook]},
                'resource_state':{'machine_free':machine_free,'machine_last_order':machine_last_order,
                                  'operator_reservations':operator_reservations}})
        lateness.update(det['order_lateness_hours'])
        completions.update(det['order_completion_minutes'])
        releases.update(sched.order_release_minutes)
        total_labour_minutes += det['labor_used_minutes']
        if release_gate is not None:
            release_gate.mark_completions(det['order_completion_minutes'])
        elapsed = time.time() - t0

        late_here = sum(1 for v in det['order_lateness_hours'].values() if v > 0)
        # The number of DISTINCT orders that actually landed an operation in
        # the committed subschedule -- not len(commit_ids), which is just the
        # slice size and would overcount if an order in the slice had none of
        # its operations in this lookahead for some reason.
        n_committed_orders = len({k.split('_')[0] for k in committed_chromo[:committed_num_ops]})

        # Margin over the best dispatching baseline, on the FULL lookahead
        # problem the GA actually solved (baselines are evaluated the same
        # way — see GAScheduler.optimize). NOTE this is not perfectly
        # apples-to-apples across different lookahead sizes: a wider L is a
        # bigger problem for both the GA and the baselines, so margin sweeps
        # across L should be read alongside on-time/makespan/throughput, not
        # in isolation.
        best_name, margin_pct = None, None
        if baselines:
            best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
            best_fit = baselines[best_name]['fitness']
            margin_pct = 100.0 * (best_fit - polished.fitness.values[0]) / best_fit
            margins.append(margin_pct)

        window_late_vals = [v for v in det['order_lateness_hours'].values() if v > 0]
        entry = {
            'window': w,
            'lookahead_orders': len(lookahead_ids),
            'committed_orders': n_committed_orders,
            'committed_operations': committed_num_ops,
            'late': late_here,
            'on_time_pct': 100.0 * (1 - late_here / max(1, len(det['order_lateness_hours']))),
            'mean_tardiness_h': sum(window_late_vals) / len(window_late_vals) if window_late_vals else 0.0,
            'labor_minutes': det['labor_used_minutes'],
            'local_search_gain_pct': pstats['improvement_pct'],
            'margin_pct': margin_pct,
            'best_baseline': best_name,
            'end_day': max(machine_free.values()) / 1440.0 if machine_free else 0.0,
            'runtime_s': elapsed,
            # Reported per window so a results table can show WHICH term the
            # search actually moved, rather than only the scalarised total.
            # Straight from evaluate_schedule's own breakdown -- never
            # recomputed here (see ga/fitness.py's note on duplication).
            'fitness': det['fitness'],
            'fitness_components': det['fitness_components'],
            'fitness_lateness_split': det['fitness_lateness_split'],
            'max_tardiness_h': det['max_tardiness_hours'],
            'pool_short_operations': det['pool_short_operations'],
        }

        if export and decision_engine is not None:
            # export_label_prefix makes the filenames arm-specific. Without
            # it both arms of a paired run write proposed_schedule[_w{n}].json,
            # so ANY later run overwrites them -- exactly how a paired staffing
            # replay ended up comparing a GA export from one run against an
            # EDF export from another. Default None preserves the historical
            # filenames for existing single-arm callers.
            label = (f"{export_label_prefix}_w{w}" if export_label_prefix
                     else (None if w == 1 else f"w{w}"))
            summary = decision_engine._export_proposed_schedule(
                committed_chromo, committed_num_ops, sched.op_details, preds, df_lines,
                sched.factory.eligibility_map, sched.order_deadline_abs,
                sub, df_machines, label=label,
                machine_pools=sched.factory.machine_ids_per_line_map,
                operator_index=operator_index, present_operator_count=capacity,
                usable_machine_count=sched.usable_machine_count,
                op_optype=sched.factory.optype_for_op,
                protected_machine_type=sched.protected_machine_type,
                # The SAME start-of-window carried-forward state
                # evaluate_schedule() was just scored against above -- NOT
                # the module-level machine_free/machine_last_order, which by
                # this point already hold this window's END state (reassigned
                # from det['final_machine_*'] a few lines up). Omitting this
                # entirely (the pre-fix behaviour) meant every window after
                # the first exported a schedule decoded against an idle
                # factory instead of the one it was actually scored against;
                # passing the post-window state instead would have
                # double-counted this window's own placements as pre-existing
                # busy time. FULL_CODEBASE_AUDIT.md Section 1.
                initial_machine_free=window_start_machine_free,
                initial_machine_last_order=window_start_machine_last_order,
                initial_operator_reservations=window_start_operator_reservations, base_time=origin,
                phenotype_details=det, scored_fitness=_f[0])
            if summary.get('phenotype_hash') != det.get('phenotype_hash'):
                raise RuntimeError(
                    f"Score/export phenotype mismatch in window {w}: "
                    f"{det.get('phenotype_hash')} != {summary.get('phenotype_hash')}"
                )
            if summary['operator_reservations_out'] != operator_reservations:
                raise RuntimeError(f"Score/export worker reservation mismatch in window {w}")
            export_summaries.append(summary)

        windows.append(entry)
        margin_str = f"{margin_pct:+.1f}% vs {best_name}" if margin_pct is not None else "n/a"
        log_fn(f"  window {w}: lookahead {len(lookahead_ids)} orders, committing "
              f"{n_committed_orders} — on-time {entry['on_time_pct']:.1f}%, "
              f"margin {margin_str}, local search {pstats['improvement_pct']:+.2f}%, "
              f"{elapsed:.0f}s")

        if release_gate is None:
            pos += commit_size

    total_runtime = time.time() - run_t0
    horizon_min = max(machine_free.values()) if machine_free else 0.0
    late = [v for v in lateness.values() if v > 0]

    aggregate = {
        'commit_size': commit_size,
        'lookahead_size': lookahead_size,
        'orders': len(lateness),
        'on_time_pct': 100.0 * (1 - len(late) / max(1, len(lateness))),
        'orders_late': len(late),
        # DENOMINATOR IS EXPLICIT IN THE NAME. This one averages over LATE
        # orders only; *_all_orders averages over every order with a
        # deadline, on-time ones contributing 0. They differ by the late
        # fraction and are routinely confused, so both are emitted and
        # neither is called simply 'mean tardiness'.
        #
        # BOTH are tardiness against the EFFECTIVE deadline: ga/fitness.py
        # clamps a deadline that already passed before the scheduling
        # origin up to the origin (effective_deadline = max(deadline, 0)),
        # so these measure delay THIS schedule adds and exclude any
        # pre-existing overdue time against the original customer date.
        'mean_tardiness_h_late_orders_only': sum(late) / len(late) if late else 0.0,
        'mean_tardiness_h_all_orders': sum(late) / max(1, len(lateness)),
        # Retained under its original name for existing consumers; it is
        # the late-orders-only figure.
        'mean_tardiness_h': sum(late) / len(late) if late else 0.0,
        'max_tardiness_h': max(late) if late else 0.0,
        'makespan_days': horizon_min / 1440.0,
        'throughput_orders_per_day': len(lateness) / (horizon_min / 1440.0) if horizon_min else 0.0,
        # Whole-book aggregate, never a mean of per-window rates.
        'labour_utilisation_pct': (
            100.0 * total_labour_minutes / (capacity * horizon_min) if horizon_min else 0.0
        ),
        'order_release_minutes': releases,
        'order_completion_minutes': completions,
        'total_runtime_s': total_runtime,
        'n_windows': len(windows),
        'mean_margin_over_baseline_pct': sum(margins) / len(margins) if margins else None,
        'mean_local_search_gain_pct': (
            sum(w['local_search_gain_pct'] for w in windows) / len(windows) if windows else 0.0
        ),
        'concurrent_operators': capacity,
    }

    # Roll up labour-by-shift / labour-by-sourcing across every exported
    # window, if export was requested. Kept separate from `aggregate` above
    # since it's only available when export=True.
    if export and export_summaries:
        shift_totals, sourcing_totals = {}, {}
        for s in export_summaries:
            for k, v in s.get('labor_minutes_by_shift', {}).items():
                shift_totals[k] = shift_totals.get(k, 0.0) + v
            for k, v in s.get('labor_minutes_by_sourcing_tier', {}).items():
                sourcing_totals[k] = sourcing_totals.get(k, 0.0) + v
        aggregate['labour_minutes_by_shift'] = shift_totals
        aggregate['labour_minutes_by_sourcing_tier'] = sourcing_totals

    return {
        'windows': windows,
        'aggregate': aggregate,
        'representative_logbook': representative_logbook,
        'logbooks': all_logbooks,
        'export_summaries': export_summaries,
    }


def run_rolling_horizon_baseline(strategy_name, routings, df_machines, df_lines, all_preds,
                                  capacity, origin, commit_size=300,
                                  manager_prioritized_orders=None, log_fn=print, ownership_snapshot=None, legacy_mode=None,
                                  export=False, decision_engine=None, operator_index=None,
                                  initial_operator_reservations=None,
                                  committed_machine_state=None,
                                  committed_machine_last_order=None,
                                  export_label_prefix=None, checkpoint_dir=None, checkpoint_identity=None, resume_checkpoint=None):
    """
    The same whole-book, non-overlapping rolling-horizon loop as
    run_rolling_horizon, but commits each window's pure-dispatching-
    heuristic chromosome (strategy_name -- one of the names
    ga.heuristics.baseline_chromosomes returns, e.g. 'FIFO', 'EDF',
    'CriticalRatio') instead of the GA's optimize()+polish() output.

    WHY THIS EXISTS: run_rolling_horizon's own per-window `baselines` dict
    (ga.scheduler.GAScheduler.run_baselines(), read at line ~257 above)
    scores each classical rule against the SAME machine state the GA's
    OWN carried-forward trajectory left behind -- useful for a per-window
    margin number, but not a genuine "what if this heuristic alone had
    scheduled the entire book" comparison, since a heuristic's own choices
    earlier in the book would have left machines in a different state than
    the GA's did. This function gives each baseline its own independent,
    self-consistent machine-state trajectory across the whole order book,
    the apples-to-apples comparison whole-book makespan/throughput/
    labour-utilisation figures actually need.

    Non-overlapping only (lookahead == commit) -- matches this project's
    actual current default (ga/scheduler.py callers leave LOOKAHEAD_ORDERS
    at None). The overlapping-lookahead machinery above exists for GA
    experimentation and is out of scope for a baseline comparison arm.

    Returns the same {'windows': [...], 'aggregate': {...}} shape
    run_rolling_horizon returns (minus the GA-only fields:
    local_search_gain_pct, margin_pct, best_baseline,
    representative_logbook), so a caller can report GA and baseline arms
    through one shared table.

    export=True runs the SAME decision_engine._export_proposed_schedule()
    worker-assignment path the GA arm uses, and returns its summaries --
    which is the only way to obtain understaffed_operations /
    execution_feasible for a baseline. Without it, a baseline's staffing
    feasibility is UNMEASURED, not zero: a report that shows the GA's
    shortage counts beside baselines that were never assessed invites
    exactly the cross-arm reading the data cannot support.

    Pass initial_operator_reservations explicitly when pairing arms, so both
    start from identical committed reservations; each call then evolves its
    OWN reservation state from there (they must not share a mutable one, or
    whichever arm runs second inherits the first's bookings).
    """
    from utils.factory_ownership import resolve_ownership
    ownership_snapshot = resolve_ownership(routings['production_order_id'], ownership_snapshot, legacy_mode=legacy_mode)
    order_release = (
        routings.groupby('production_order_id')['release_date'].min()
        .reset_index().sort_values(['release_date', 'production_order_id'])
    )
    all_orders = order_release['production_order_id'].tolist()

    machine_free, machine_last_order = {}, {}
    if committed_machine_state is None:
        from database.queries import fetch_committed_machine_state
        committed_machine_state, loaded_last_order = fetch_committed_machine_state(origin)
        if committed_machine_last_order is None:
            committed_machine_last_order = loaded_last_order
    machine_free.update(committed_machine_state)
    machine_last_order.update(committed_machine_last_order or {})
    lateness, releases, completions = {}, {}, {}
    windows = []
    export_summaries = []
    total_labour_minutes = 0.0
    # Same reservation bootstrap the GA path uses, so a paired GA-vs-baseline
    # staffing run starts both arms from the identical committed state.
    operator_reservations = initial_operator_reservations
    if operator_index is not None and operator_reservations is None:
        from database.queries import fetch_committed_operator_reservations
        operator_reservations = fetch_committed_operator_reservations(origin)

    pos, w = 0, 0
    run_t0 = time.time()
    while pos < len(all_orders):
        lookahead_ids = set(all_orders[pos:pos + commit_size])
        commit_ids = {str(x) for x in all_orders[pos:pos + commit_size]}
        sub = routings[routings['production_order_id'].isin(lookahead_ids)]

        w += 1
        keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
        preds = {k: v for k, v in all_preds.items() if k in keys}

        t0 = time.time()
        sched = GAScheduler(sub, df_machines, df_lines, preds,
                            ownership_snapshot=ownership_snapshot, legacy_mode=legacy_mode,
                            current_time=origin, present_operator_count=capacity,
                            initial_machine_free=machine_free,
                            initial_machine_last_order=machine_last_order,
                            manager_prioritized_orders=manager_prioritized_orders,
                            scored_order_ids=commit_ids,
                            operator_index=operator_index,
                            initial_operator_reservations=operator_reservations)
        chromo = baseline_chromosomes(
            sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup
        )[strategy_name]

        committed_chromo, committed_num_ops = extract_committed_subschedule(
            chromo, sched.num_ops, commit_ids)
        if committed_num_ops == 0:
            log_fn(f"  [{strategy_name}] window {w}: nothing to commit (empty intersection) — advancing.")
            pos += commit_size
            continue

        # Captured BEFORE evaluate_schedule reassigns machine_free below --
        # an export must decode against the state the schedule was SCORED
        # against, not this window's end state, which would double-count this
        # window's own placements as pre-existing busy time. Same requirement
        # the GA path above already meets.
        window_start_machine_free = machine_free
        window_start_machine_last_order = machine_last_order
        window_start_operator_reservations = operator_reservations

        _f, det = evaluate_schedule(
            committed_chromo, committed_num_ops, sched.op_details, sched.gnn_predictions,
            all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes,
            order_release_minutes=sched.order_release_minutes,
            machine_pools=sched.factory.machine_ids_per_line_map,
            machine_line_map=sched.machine_line_map,
            usable_machine_count=sched.usable_machine_count,
            present_operator_count=sched.present_operator_count,
            op_sequence_map=sched.seq_lookup,
            initial_machine_free=machine_free,
            initial_machine_last_order=machine_last_order, return_details=True,
            op_optype=sched.factory.optype_for_op,
            protected_machine_type=sched.protected_machine_type,
            # Same origin and availability predicate the scheduler scores
            # with, so the committed span equals the scored span. Omitting
            # these would score the window on one calendar and commit it on
            # another -- the "exported plan is not the plan that was scored"
            # failure this codebase has already paid for once.
            base_time=sched.now,
            worker_availability=sched.worker_availability,
            operator_index=operator_index,
            initial_operator_reservations=window_start_operator_reservations)

        machine_free = det['final_machine_free']
        machine_last_order = det['final_machine_last_order']
        operator_reservations = det['final_operator_reservations']
        lateness.update(det['order_lateness_hours'])
        completions.update(det['order_completion_minutes'])
        releases.update(sched.order_release_minutes)
        total_labour_minutes += det['labor_used_minutes']
        elapsed = time.time() - t0

        late_here = sum(1 for v in det['order_lateness_hours'].values() if v > 0)
        n_committed_orders = len({k.split('_')[0] for k in committed_chromo[:committed_num_ops]})
        window_late_vals = [v for v in det['order_lateness_hours'].values() if v > 0]

        entry = {
            'window': w,
            'lookahead_orders': len(lookahead_ids),
            'committed_orders': n_committed_orders,
            'committed_operations': committed_num_ops,
            'late': late_here,
            'on_time_pct': 100.0 * (1 - late_here / max(1, len(det['order_lateness_hours']))),
            'mean_tardiness_h': sum(window_late_vals) / len(window_late_vals) if window_late_vals else 0.0,
            'labor_minutes': det['labor_used_minutes'],
            'end_day': max(machine_free.values()) / 1440.0 if machine_free else 0.0,
            'runtime_s': elapsed,
            # Same breakdown the GA arm records, from the same scorer, so the
            # two arms' component tables are comparable term by term.
            'fitness': det['fitness'],
            'fitness_components': det['fitness_components'],
            'fitness_lateness_split': det['fitness_lateness_split'],
            'max_tardiness_h': det['max_tardiness_hours'],
            'pool_short_operations': det['pool_short_operations'],
        }
        if export and decision_engine is not None:
            summary = decision_engine._export_proposed_schedule(
                committed_chromo, committed_num_ops, sched.op_details, preds, df_lines,
                sched.factory.eligibility_map, sched.order_deadline_abs,
                sub, df_machines,
                label=f"{export_label_prefix or strategy_name.lower()}_w{w}",
                machine_pools=sched.factory.machine_ids_per_line_map,
                operator_index=operator_index, present_operator_count=capacity,
                usable_machine_count=sched.usable_machine_count,
                op_optype=sched.factory.optype_for_op,
                protected_machine_type=sched.protected_machine_type,
                initial_machine_free=window_start_machine_free,
                initial_machine_last_order=window_start_machine_last_order,
                initial_operator_reservations=window_start_operator_reservations, base_time=origin,
                phenotype_details=det, scored_fitness=_f[0])
            if summary.get('phenotype_hash') != det.get('phenotype_hash'):
                raise RuntimeError(
                    f"Score/export phenotype mismatch in window {w}: "
                    f"{det.get('phenotype_hash')} != {summary.get('phenotype_hash')}"
                )
            if summary['operator_reservations_out'] != operator_reservations:
                raise RuntimeError(f"Score/export worker reservation mismatch in window {w}")
            export_summaries.append(summary)

        windows.append(entry)
        log_fn(f"  [{strategy_name}] window {w}: lookahead {len(lookahead_ids)} orders, committing "
              f"{n_committed_orders} — on-time {entry['on_time_pct']:.1f}%, {elapsed:.0f}s")
        pos += commit_size

    total_runtime = time.time() - run_t0
    horizon_min = max(machine_free.values()) if machine_free else 0.0
    late = [v for v in lateness.values() if v > 0]

    aggregate = {
        'strategy': strategy_name,
        'commit_size': commit_size,
        'orders': len(lateness),
        'on_time_pct': 100.0 * (1 - len(late) / max(1, len(lateness))),
        'orders_late': len(late),
        # DENOMINATOR IS EXPLICIT IN THE NAME. This one averages over LATE
        # orders only; *_all_orders averages over every order with a
        # deadline, on-time ones contributing 0. They differ by the late
        # fraction and are routinely confused, so both are emitted and
        # neither is called simply 'mean tardiness'.
        #
        # BOTH are tardiness against the EFFECTIVE deadline: ga/fitness.py
        # clamps a deadline that already passed before the scheduling
        # origin up to the origin (effective_deadline = max(deadline, 0)),
        # so these measure delay THIS schedule adds and exclude any
        # pre-existing overdue time against the original customer date.
        'mean_tardiness_h_late_orders_only': sum(late) / len(late) if late else 0.0,
        'mean_tardiness_h_all_orders': sum(late) / max(1, len(lateness)),
        # Retained under its original name for existing consumers; it is
        # the late-orders-only figure.
        'mean_tardiness_h': sum(late) / len(late) if late else 0.0,
        'max_tardiness_h': max(late) if late else 0.0,
        'makespan_days': horizon_min / 1440.0,
        'throughput_orders_per_day': len(lateness) / (horizon_min / 1440.0) if horizon_min else 0.0,
        'labour_utilisation_pct': (
            100.0 * total_labour_minutes / (capacity * horizon_min) if horizon_min else 0.0
        ),
        'order_release_minutes': releases,
        'order_completion_minutes': completions,
        'total_runtime_s': total_runtime,
        'n_windows': len(windows),
        'concurrent_operators': capacity,
    }

    return {'windows': windows, 'aggregate': aggregate,
            'export_summaries': export_summaries}
