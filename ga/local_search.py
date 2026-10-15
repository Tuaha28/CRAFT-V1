"""
Local search polishing for a finished schedule.

WHY THIS EXISTS. Measured across ten window runs, fifty generations of
crossover, mutation, elitism, random immigrants and island migration improved
on the initial population by 0.4% (single population) and 0.9% (islands), and
seven of those ten runs improved by exactly 0.0%. The GA's whole 7-10% margin
over dispatching rules comes from how the initial population is CONSTRUCTED,
not from the search.

The diagnosis is specific. Order Crossover reshuffles a large span of a
1,500-operation permutation and mutation makes ~6 random gene changes; neither
is likely to improve a schedule that was already built by a work-aware
heuristic. Those operators explore by large random jumps, and at this problem
size almost every large random jump is worse.

So this adds the missing operator class: SMALL, TARGETED, IMPROVING moves.
Rather than perturbing anywhere in the chromosome, every move is aimed at the
pool that is actually determining the schedule's length -- the (line, machine
type) pool carrying the most work per machine. Fixing anything else cannot
shorten the schedule.

Three move types, matching the three genes:
  * SEQUENCE  swap two operations competing for the critical pool, changing
              which gets first claim on its machines.
  * LINE      move an operation off the critical pool onto a less-loaded
              eligible line that can also run it.
  * MACHINES  adjust an operation's machine count on the critical pool --
              fewer frees machines for its neighbours, more shortens it.

Hill climbing with first-improvement acceptance: take the first candidate that
beats the incumbent rather than evaluating them all. Evaluation is the
expensive part (a 300-order window is ~0.4s per call), so the budget is spent
on more restarts rather than on exhaustive neighbourhoods.
"""

import random

from ga.operators import repair_precedence


def compute_pool_loads(chromosome, num_ops, factory, op_work):
    """
    Work per machine on each (line, machine type) pool, from the chromosome
    alone -- no simulation needed.

    This is the same quantity the schedule's length is bounded by: the busiest
    pool cannot finish sooner than its own work divided by its machines.
    """
    os_array = chromosome[:num_ops]
    la_array = chromosome[num_ops:2 * num_ops]
    loads, sizes = {}, {}
    for i, op_key in enumerate(os_array):
        line_id = la_array[i]
        key = (line_id, factory.optype_for_op.get(op_key))
        loads[key] = loads.get(key, 0.0) + op_work.get(op_key, 0.0)
        if key not in sizes:
            sizes[key] = max(1, len(factory.machine_ids_per_line_map.get(op_key, {}).get(line_id, [])))
    return {k: v / sizes.get(k, 1) for k, v in loads.items()}


def _ops_on_pool(chromosome, num_ops, factory, pool):
    """Indices of operations currently assigned to a given (line, type) pool."""
    os_array = chromosome[:num_ops]
    la_array = chromosome[num_ops:2 * num_ops]
    line_id, optype = pool
    return [i for i, op_key in enumerate(os_array)
            if la_array[i] == line_id and factory.optype_for_op.get(op_key) == optype]


def _move_swap_sequence(chromosome, num_ops, factory, pool, rng):
    """
    Swap two operations competing for the critical pool, then repair.

    Repair is mandatory: a raw positional swap can push an operation past a
    sibling from the same order and break routing precedence. repair_precedence
    re-sorts each order within the positions it occupies, so the result is
    always valid -- possibly a smaller change than the raw swap intended, but
    never an illegal one.
    """
    idxs = _ops_on_pool(chromosome, num_ops, factory, pool)
    if len(idxs) < 2:
        return None
    i, j = rng.sample(idxs, 2)
    cand = list(chromosome)
    cand[i], cand[j] = cand[j], cand[i]
    cand[num_ops + i], cand[num_ops + j] = cand[num_ops + j], cand[num_ops + i]
    cand[2 * num_ops + i], cand[2 * num_ops + j] = cand[2 * num_ops + j], cand[2 * num_ops + i]
    cand[:num_ops] = repair_precedence(cand[:num_ops], factory.op_sequence_map)
    return cand


def _move_reassign_line(chromosome, num_ops, factory, pool, pool_loads, rng):
    """
    Move one operation off the critical pool to the least-loaded eligible line
    that can also run it. MC is re-clamped to the new line's pool size, so the
    result respects the machine-count bound by construction.
    """
    idxs = _ops_on_pool(chromosome, num_ops, factory, pool)
    if not idxs:
        return None
    i = rng.choice(idxs)
    op_key = chromosome[i]
    optype = factory.optype_for_op.get(op_key)
    eligible = [ln for ln in factory.eligibility_map.get(op_key, [])
                if ln != chromosome[num_ops + i]]
    if not eligible:
        return None
    target = min(eligible, key=lambda ln: pool_loads.get((ln, optype), 0.0))
    cand = list(chromosome)
    cand[num_ops + i] = target
    cap = factory._max_machines_for(op_key, target)
    cand[2 * num_ops + i] = max(1, min(int(cand[2 * num_ops + i]), cap))
    return cand


def _move_adjust_machines(chromosome, num_ops, factory, pool, rng):
    """
    Nudge an operation's machine count on the critical pool, up or down.

    Both directions are worth trying and they trade against each other: more
    machines shortens this operation but ties up more of the pool its
    neighbours are queuing for; fewer releases capacity at the cost of running
    longer.
    """
    idxs = _ops_on_pool(chromosome, num_ops, factory, pool)
    if not idxs:
        return None
    i = rng.choice(idxs)
    op_key = chromosome[i]
    line_id = chromosome[num_ops + i]
    cap = factory._max_machines_for(op_key, line_id)
    current = int(chromosome[2 * num_ops + i])
    options = [v for v in (current - 1, current + 1) if 1 <= v <= cap and v != current]
    if not options:
        return None
    cand = list(chromosome)
    cand[2 * num_ops + i] = rng.choice(options)
    return cand


MOVE_GENERATORS = ('swap', 'line', 'machines')


def polish(chromosome, num_ops, factory, op_work, evaluate, max_evals=250,
           rng=None, log_fn=None):
    """
    Hill-climb a finished schedule with targeted moves.

    chromosome: the GA's output, as a plain list of genes.
    evaluate:   callable taking a gene list and returning DEAP's (fitness,)
                tuple -- the same objective the GA optimised, so an
                improvement here is an improvement in the same terms.
    max_evals:  evaluation budget. The cost is entirely in `evaluate`.

    Returns (best_chromosome, best_fitness, stats).
    """
    rng = rng or random
    best = list(chromosome)
    best_fit = evaluate(best)[0]
    start_fit = best_fit
    evals = 1
    accepted = {m: 0 for m in MOVE_GENERATORS}
    rejected = 0

    # Stop only when the budget runs out or `patience` consecutive candidates
    # all fail.
    #
    # BUG FIX: the first version tried exactly one candidate per move type and
    # exited the outer loop as soon as a pass produced no improvement -- at
    # most three evaluations before giving up. Measured, it used 4 of a
    # 400-evaluation budget and finished in under a second, which looked like
    # "local search does not help" when it was really "local search barely
    # ran". A single move touches one of ~1,500 operations, so the great
    # majority of candidates are duds by construction and the climber has to
    # be willing to keep drawing.
    patience = max(20, max_evals // 6)
    consecutive_failures = 0
    # Sampling among the top few loaded pools rather than only the single
    # busiest one: once the critical pool is locally optimal, restricting to
    # it means every further draw is a guaranteed miss, and the second- and
    # third-busiest pools are usually within a few percent of it anyway.
    top_k_pools = 5

    while evals < max_evals and consecutive_failures < patience:
        pool_loads = compute_pool_loads(best, num_ops, factory, op_work)
        if not pool_loads:
            break
        ranked = sorted(pool_loads, key=pool_loads.get, reverse=True)[:top_k_pools]
        pool = rng.choice(ranked)
        move = rng.choice(MOVE_GENERATORS)

        if move == 'swap':
            cand = _move_swap_sequence(best, num_ops, factory, pool, rng)
        elif move == 'line':
            cand = _move_reassign_line(best, num_ops, factory, pool, pool_loads, rng)
        else:
            cand = _move_adjust_machines(best, num_ops, factory, pool, rng)
        if cand is None:
            consecutive_failures += 1
            continue

        fit = evaluate(cand)[0]
        evals += 1
        # Strict improvement only: accepting equal-scoring moves would let the
        # climber wander a plateau indefinitely without converging.
        if fit < best_fit - 1e-9:
            best, best_fit = cand, fit
            accepted[move] += 1
            consecutive_failures = 0
        else:
            rejected += 1
            consecutive_failures += 1

    stats = {
        'evaluations': evals,
        'start_fitness': start_fit,
        'final_fitness': best_fit,
        'improvement_pct': 100.0 * (start_fit - best_fit) / start_fit if start_fit else 0.0,
        'accepted': accepted,
        'rejected': rejected,
    }
    if log_fn:
        log_fn(
            f"Local search: {start_fit:.1f} -> {best_fit:.1f} "
            f"({stats['improvement_pct']:+.2f}%) in {evals} evaluations; "
            f"accepted {accepted}, rejected {rejected}."
        )
    return best, best_fit, stats
