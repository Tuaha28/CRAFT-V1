"""
Sequential ALNS (Adaptive Large Neighbourhood Search) refinement --
ALNS_REFINEMENT_PROPOSAL.md. Alternative to ga/local_search.py::polish()
for the end-of-run refinement stage, NOT wired into GAScheduler.optimize()
by default -- opt-in only, via GAScheduler.alns_refine(), same pattern as
polish() and memetic_interval.

WHY THIS EXISTS. GA_AGENT_POSTMORTEM.md Section 7a: evolution contributes
0.035% over the initial population, local-search polish contributes
+0.615% (~17.5x more) -- the refinement stage is where the real gains
live. polish() is a single-gene-move hill-climb with uniform-random
operator choice and strict-improvement-only acceptance (see its own
docstring). This adds the missing structure: multi-operation destroy/
repair cycles, ADAPTIVE operator weighting (an operator that is actually
paying off on this run's landscape gets selected more often), and a
permissive-but-tracked acceptance rule that can escape local optima
without losing the "never finishes worse than it started" guarantee for
the stage's OUTPUT.

DESTROY: pick a subset of operation INDICES and tear out their (line,
machine-count) only -- position in os_array is never touched, so
repair_precedence is never needed here (unlike custom_crossover/
custom_mutate, which do reorder OS). Three operators
(ALNS_REFINEMENT_PROPOSAL.md Section 3a):
  high_delay_risk  -- the operations the GNN itself scores most likely
                       to run late (gnn_predictions[op]['delay']).
  bottleneck_pool  -- operations on the busiest (line, type) pool(s),
                       generalizing local_search.py's own single-move
                       "critical pool" targeting to a whole batch.
  random           -- the standard ALNS baseline operator (Ropke &
                       Pisinger), included so the two targeted operators
                       are judged against a neutral comparison point.

REPAIR: reassigns each torn-out operation to the least-loaded eligible
line -- the SAME deterministic argmin rule local_search.py::
_move_reassign_line already uses for exactly this purpose, reused
verbatim rather than choose_line_work_aware/choose_line_least_loaded
(which draw from the global `random` module internally, not an
injectable rng -- using them here would leave part of this module's
randomness outside the caller's seed control, breaking the
reproducibility this codebase tests everywhere else). Machine-count is
redrawn upper-half-biased within the new line's cap, mirroring
custom_mutate's own MC step exactly.

ACCEPTANCE (Section 3d): TWO solutions tracked through the search, not
one. `current` explores under a simulated-annealing-style rule (accepts
a worse candidate with probability exp(-delta/temperature), temperature
cooling geometrically over the run) -- this is what gives the search room
to escape a local optimum, the property that actually differentiates
ALNS from a bigger-neighbourhood hill-climb. `best` only ever updates on
a genuine improvement, exactly like polish()'s own strict rule, and is
what this function returns -- so the STAGE's output can never be worse
than what went in, even though the search internally may wander.

ADAPTIVE WEIGHTS: roulette-wheel operator selection, weights updated by
exponential smoothing over fixed-length segments (Ropke & Pisinger),
reward tiers: new best > improved current > accepted-but-worse > 0 for
rejected. weight_history is returned in stats so a caller can inspect
whether the mechanism actually differentiated the operators on a given
run, not just infer it from the final fitness.
"""
import math
import random
import statistics

from ga.local_search import compute_pool_loads, _ops_on_pool

DESTROY_OPERATORS = ('high_delay_risk', 'bottleneck_pool', 'random')


def _removal_count(num_ops, removal_frac):
    return max(1, min(num_ops, round(removal_frac * num_ops)))


def _destroy_high_delay_risk(chromosome, num_ops, gnn_predictions, removal_frac):
    os_array = chromosome[:num_ops]
    count = _removal_count(num_ops, removal_frac)
    ranked = sorted(
        range(num_ops),
        key=lambda i: gnn_predictions.get(os_array[i], {}).get('delay', 0.0),
        reverse=True,
    )
    return ranked[:count]


def _destroy_bottleneck_pool(chromosome, num_ops, factory, op_work, removal_frac, rng, top_k_pools=5):
    count = _removal_count(num_ops, removal_frac)
    pool_loads = compute_pool_loads(chromosome, num_ops, factory, op_work)
    if not pool_loads:
        return []
    ranked_pools = sorted(pool_loads, key=pool_loads.get, reverse=True)[:top_k_pools]
    idxs = []
    for pool in ranked_pools:
        idxs.extend(_ops_on_pool(chromosome, num_ops, factory, pool))
        if len(idxs) >= count:
            break
    rng.shuffle(idxs)
    return idxs[:count]


def _destroy_random(chromosome, num_ops, removal_frac, rng):
    count = _removal_count(num_ops, removal_frac)
    return rng.sample(range(num_ops), min(count, num_ops))


def _pool_size(factory, op_key, ln):
    return max(1, len(factory.machine_ids_per_line_map.get(op_key, {}).get(ln, [])))


def _impact_counts(chromosome, num_ops, factory, destroyed_idxs, optype):
    """
    Count of operations OUTSIDE this destroy batch eligible for each
    candidate line, for this optype -- proxy for how contested the pool
    gets AFTER this placement (ALNS_REPAIR_SCORING_PROPOSAL.md Section 3).
    """
    os_array = chromosome[:num_ops]
    destroyed_ops = {os_array[i] for i in destroyed_idxs}
    counts = {}
    for i, op_key in enumerate(os_array):
        if op_key in destroyed_ops:
            continue
        if factory.optype_for_op.get(op_key) != optype:
            continue
        for ln in factory.eligibility_map.get(op_key, []):
            counts[ln] = counts.get(ln, 0) + 1
    return counts


def _richer_score_line(eligible, pool_loads, optype, op_key, delay_prob, impact_counts,
                        op_work, factory, weights):
    """
    ALNS_REPAIR_SCORING_PROPOSAL.md Section 3/11 richer repair-line score:
    load + risk-weighted MARGINAL load this operation would add to each
    candidate (not the candidate's pre-existing load -- Section 7 proved
    that version is structurally inert, always degenerating to a rescale
    of the load term alone) + a downstream-contention proxy.
    """
    w_load, w_risk, w_impact = weights
    loads_for_type = [pool_loads.get((ln, optype), 0.0) for ln in eligible]
    max_load = max(loads_for_type) if loads_for_type else 1.0
    max_impact = max(impact_counts.values()) if impact_counts else 1.0
    marginals = {ln: op_work.get(op_key, 0.0) / _pool_size(factory, op_key, ln) for ln in eligible}
    max_marginal = max(marginals.values()) if marginals else 1.0

    def score(ln):
        norm_load = pool_loads.get((ln, optype), 0.0) / max_load if max_load else 0.0
        norm_marginal = marginals[ln] / max_marginal if max_marginal else 0.0
        norm_impact = impact_counts.get(ln, 0) / max_impact if max_impact else 0.0
        return (w_load * norm_load) + (w_risk * delay_prob * norm_marginal) + (w_impact * norm_impact)
    return min(eligible, key=score)


def _repair_reassign(chromosome, num_ops, factory, op_work, destroyed_idxs, rng,
                      gnn_predictions=None, repair_score_weights=None):
    """
    Reassign each torn-out operation to a target line. Loads are computed
    ONCE for the whole batch and reused, same accepted staleness tradeoff
    custom_mutate's own docstring documents for the identical reason
    (O(num_ops) once vs. O(num_ops^2) if recomputed per gene).

    repair_score_weights: optional (w_load, w_risk, w_impact) tuple. None
    (default): unchanged plain least-loaded-line argmin, the rule
    local_search.py::_move_reassign_line also uses. When given (together
    with gnn_predictions), uses the richer score from
    ALNS_REPAIR_SCORING_PROPOSAL.md instead -- opt-in only, same pattern
    as memetic_interval/TCOX, never the default.
    """
    cand = list(chromosome)
    la_array = cand[num_ops:2 * num_ops]
    mc_array = cand[2 * num_ops:]
    pool_loads = compute_pool_loads(cand, num_ops, factory, op_work)

    use_richer = repair_score_weights is not None and gnn_predictions is not None
    impact_cache = {}

    for i in destroyed_idxs:
        op_key = cand[i]
        optype = factory.optype_for_op.get(op_key)
        eligible = factory.eligibility_map.get(op_key, [])
        if not eligible:
            continue
        if use_richer:
            if optype not in impact_cache:
                impact_cache[optype] = _impact_counts(cand, num_ops, factory, destroyed_idxs, optype)
            delay_prob = gnn_predictions.get(op_key, {}).get('delay', 0.0)
            target = _richer_score_line(eligible, pool_loads, optype, op_key, delay_prob,
                                         impact_cache[optype], op_work, factory,
                                         repair_score_weights)
        else:
            target = min(eligible, key=lambda ln: pool_loads.get((ln, optype), 0.0))
        la_array[i] = target
        cap = factory._max_machines_for(op_key, target)
        mc_array[i] = rng.randint(max(1, (cap + 1) // 2), cap)

    cand[num_ops:2 * num_ops] = la_array
    cand[2 * num_ops:] = mc_array
    return cand


def _draw_operator(weights, rng):
    total_w = sum(weights.values())
    draw = rng.random() * total_w
    cum = 0.0
    chosen = DESTROY_OPERATORS[-1]
    for op in DESTROY_OPERATORS:
        cum += weights[op]
        if draw <= cum:
            chosen = op
            break
    return chosen


def _destroy_and_repair(chosen_op, current, num_ops, factory, op_work, gnn_predictions, removal_frac, rng,
                         repair_score_weights=None):
    if chosen_op == 'high_delay_risk':
        idxs = _destroy_high_delay_risk(current, num_ops, gnn_predictions, removal_frac)
    elif chosen_op == 'bottleneck_pool':
        idxs = _destroy_bottleneck_pool(current, num_ops, factory, op_work, removal_frac, rng)
    else:
        idxs = _destroy_random(current, num_ops, removal_frac, rng)
    if not idxs:
        return None
    return _repair_reassign(current, num_ops, factory, op_work, idxs, rng,
                             gnn_predictions=gnn_predictions, repair_score_weights=repair_score_weights)


def alns_refine(chromosome, num_ops, factory, op_work, evaluate, gnn_predictions,
                 max_evals=400, removal_frac=0.035, rng=None, log_fn=None,
                 segment_length=20, reaction_factor=0.3,
                 reward_new_best=3.0, reward_improved_current=2.0, reward_accepted_worse=1.0,
                 calibration_evals=None, repair_score_weights=None):
    """
    chromosome: the starting individual (this project's convention: the
                best heuristic-seeded construction, same starting point
                polish() is normally applied to).
    evaluate:   same (fitness,)-returning callable the GA/polish() use.
    removal_frac: fraction of num_ops torn out per destroy call.
    repair_score_weights: optional (w_load, w_risk, w_impact) tuple, passed
                through to _repair_reassign. None (default): unchanged
                plain least-loaded-line repair. See _repair_reassign's own
                docstring -- ALNS_REPAIR_SCORING_PROPOSAL.md Section 13.

    Returns (best_chromosome, best_fitness, stats) -- same shape as
    ga/local_search.py::polish(), plus weight_history/final_weights.

    TEMPERATURE CALIBRATION (ALNS_REFINEMENT_PROPOSAL.md Section 9
    follow-up): the first `calibration_evals` evaluations (default
    max(10, max_evals // 20), i.e. ~5% of budget) are a real, measured
    warmup, not the earlier borrowed single-gene-scale guess
    (t0 = 0.02 * start_fitness, which scratch/alns_delta_calibration.py
    measured as roughly 10-20x too small: real median worsening deltas
    from an actual multi-operation destroy-repair move ranged ~35-345
    depending on window and removal_frac, versus the old formula's
    17-20). A fixed external constant does not generalize well across
    that range, so this samples the ACTUAL instance being solved --
    same window, same removal_frac, same starting point -- and derives
    t0 from the median WORSENING delta actually observed, calibrated so
    a typical worsening move has ~50% acceptance probability at the
    start of the search (accept_prob = exp(-delta/t0) = 0.5 =>
    t0 = median_worsening_delta / ln(2)). Calibration itself only ever
    accepts strict improvements (safe, matches polish()'s own rule) --
    it never risks a bad acceptance before the temperature that would
    make such acceptance meaningful even exists.
    """
    rng = rng or random
    current = list(chromosome)
    current_fit = evaluate(current)[0]
    best = list(current)
    best_fit = current_fit
    start_fit = current_fit
    evals = 1

    weights = {op: 1.0 for op in DESTROY_OPERATORS}
    segment_scores = {op: 0.0 for op in DESTROY_OPERATORS}
    segment_uses = {op: 0 for op in DESTROY_OPERATORS}
    weight_history = []

    accepted_new_best = 0
    accepted_improved = 0
    accepted_worse = 0
    rejected = 0
    no_op_destroys = 0

    # --- Calibration phase: measure, don't assume. ---
    calibration_evals = calibration_evals if calibration_evals is not None else max(10, max_evals // 20)
    calibration_evals = min(calibration_evals, max(0, max_evals - 1))
    calibration_deltas = []
    for _ in range(calibration_evals):
        chosen_op = _draw_operator(weights, rng)
        candidate = _destroy_and_repair(chosen_op, current, num_ops, factory, op_work,
                                         gnn_predictions, removal_frac, rng,
                                         repair_score_weights=repair_score_weights)
        segment_uses[chosen_op] += 1
        if candidate is None:
            no_op_destroys += 1
            continue
        cand_fit = evaluate(candidate)[0]
        evals += 1
        calibration_deltas.append(cand_fit - current_fit)
        if cand_fit < best_fit - 1e-9:
            best, best_fit = list(candidate), cand_fit
            current, current_fit = candidate, cand_fit
            segment_scores[chosen_op] += reward_new_best
            accepted_new_best += 1
        else:
            rejected += 1

    worsening = [d for d in calibration_deltas if d > 1e-9]
    if worsening:
        t0 = statistics.median(worsening) / math.log(2)
    else:
        # Degenerate fallback (no worsening sample observed during
        # calibration -- unlikely on real data, but must not crash or
        # silently divide by zero): the old single-gene-scale guess,
        # clearly worse than a real measurement but never hit in the
        # real validation run that motivated this fix.
        t0 = max(1e-6, 0.02 * start_fit)
    t_end = t0 * 0.01
    calibration_t0 = t0
    if log_fn and calibration_deltas:
        log_fn(
            f"  ALNS calibration: {len(calibration_deltas)} samples, "
            f"{len(worsening)} worsening, median worsening delta="
            f"{statistics.median(worsening) if worsening else float('nan'):.2f}, t0={t0:.2f}"
        )

    remaining_evals = max(1, max_evals - evals)
    main_phase_evals = 0

    while evals < max_evals:
        # Cooling progresses over the MAIN phase's own budget, not the
        # total -- calibration used a different (strict-improvement-only)
        # acceptance rule, so the temperature schedule should start fresh
        # at t0 when the main phase begins, not partway through its decay.
        progress = main_phase_evals / remaining_evals
        temperature = t0 * ((t_end / t0) ** min(1.0, progress))

        chosen_op = _draw_operator(weights, rng)
        candidate = _destroy_and_repair(chosen_op, current, num_ops, factory, op_work,
                                         gnn_predictions, removal_frac, rng,
                                         repair_score_weights=repair_score_weights)

        if candidate is None:
            no_op_destroys += 1
            segment_uses[chosen_op] += 1
            if no_op_destroys > max_evals:
                break  # no destroy operator can find anything to remove -- stop, not spin forever
            continue

        cand_fit = evaluate(candidate)[0]
        main_phase_evals += 1
        evals += 1
        segment_uses[chosen_op] += 1

        if cand_fit < best_fit - 1e-9:
            best, best_fit = list(candidate), cand_fit
            current, current_fit = candidate, cand_fit
            segment_scores[chosen_op] += reward_new_best
            accepted_new_best += 1
        elif cand_fit < current_fit - 1e-9:
            current, current_fit = candidate, cand_fit
            segment_scores[chosen_op] += reward_improved_current
            accepted_improved += 1
        else:
            delta = cand_fit - current_fit
            accept_prob = math.exp(-delta / max(temperature, 1e-9)) if delta > 0 else 1.0
            if rng.random() < accept_prob:
                current, current_fit = candidate, cand_fit
                segment_scores[chosen_op] += reward_accepted_worse
                accepted_worse += 1
            else:
                rejected += 1

        if evals % segment_length == 0:
            for op in DESTROY_OPERATORS:
                avg_score = segment_scores[op] / segment_uses[op] if segment_uses[op] else 0.0
                weights[op] = weights[op] * (1 - reaction_factor) + reaction_factor * avg_score
                if weights[op] <= 0:
                    weights[op] = 0.01  # floor: roulette selection never fully excludes an operator
            weight_history.append((evals, dict(weights)))
            segment_scores = {op: 0.0 for op in DESTROY_OPERATORS}
            segment_uses = {op: 0 for op in DESTROY_OPERATORS}

    stats = {
        'evaluations': evals,
        'start_fitness': start_fit,
        'final_fitness': best_fit,
        'improvement_pct': 100.0 * (start_fit - best_fit) / start_fit if start_fit else 0.0,
        'accepted_new_best': accepted_new_best,
        'accepted_improved': accepted_improved,
        'accepted_worse': accepted_worse,
        'rejected': rejected,
        'weight_history': weight_history,
        'final_weights': dict(weights),
        'calibration_t0': calibration_t0,
        'calibration_samples': len(calibration_deltas),
        'calibration_worsening_samples': len(worsening),
    }
    if log_fn:
        log_fn(
            f"ALNS refine: {start_fit:.1f} -> {best_fit:.1f} ({stats['improvement_pct']:+.2f}%) "
            f"in {evals} evaluations (t0={calibration_t0:.1f} from {len(worsening)} measured "
            f"worsening samples); new_best={accepted_new_best} improved={accepted_improved} "
            f"worse_accepted={accepted_worse} rejected={rejected}; final weights={weights}"
        )
    return best, best_fit, stats
