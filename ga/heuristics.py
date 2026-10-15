"""
Classical dispatching heuristics for the garment job shop.

These serve TWO purposes at once, deliberately:

  1. SEEDING. The GA's initial population is built from a mix of these
     strategies instead of 50 draws of one random construction. A population
     of near-identical individuals gives crossover nothing to recombine; a
     population built from genuinely different good ideas does.

  2. BASELINES. Any results chapter comparing "our GA" against nothing is not
     a comparison. Running FIFO / EDF / critical-ratio standalone, scored on
     the same simulation and the same metrics as the GA, is the honest way to
     show what the GA is worth. Building them twice — once to seed, once to
     benchmark — would be wasted effort and would risk the two versions
     disagreeing.

Every constructor returns a chromosome laid out exactly as ga/chromosome.py
defines it: [operation sequence] + [line assignment] + [machine count], and
every one of them respects routing precedence.

A NOTE ON WHAT THESE RULES ACTUALLY CONTROL. The chromosome's operation
sequence is a PRIORITY ORDER, not a set of start times — ga/fitness.py places
each operation at the earliest moment its machines are free, in the order the
sequence lists them. So a dispatching rule here decides who gets first claim
on contested machines, which is precisely what a dispatching rule means on a
real floor.
"""

import random
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from ga.operators import repair_precedence

logger = get_logger("ga_heuristics", "ga.log")


# ---------------------------------------------------------------------------
# Line assignment
# ---------------------------------------------------------------------------

def choose_line_least_loaded(op_key, eligible_lines, line_load, factory, rng=random, top_k=1):
    """
    Picks the eligible line whose pool for THIS OPERATION'S MACHINE TYPE is
    carrying the least work per machine.

    Three things this deliberately does NOT do:

    * It does not balance by OPERATION COUNT. A real run balanced counts
      almost perfectly (35-95 operations per line) while the work behind them
      varied enormously — order quantities span 1,500 to 12,000 units — and
      the resulting load imbalance was 181,646 minutes of standard deviation,
      about 126 days of spread between lines. Counting operations measures the
      wrong thing.

    * It does not balance raw minutes either. Lines carry between 1 and 15
      machines of a given type, so a line with 15 machines should absorb
      fifteen times the work of a line with 1. Dividing accumulated work by
      the size of the pool that will actually run it makes the comparison
      fair.

    * It does not track load PER LINE. This is the fix that matters most.
      Machine capacity is per (line, machine type) — an operation can only use
      machines of its own type on its own line — but load was accumulated
      against the line as a whole. So a line that had absorbed heavy Body
      Assembly work looked "loaded" to a Flatlock Hemming operation, even
      though its Flatlock machines were completely untouched, and the
      scheduler avoided it. Measured consequence: only 223 of 494 (line, type)
      pools ever received work, each line ran just 2.25 of 5 types, and the
      busiest pool carried 92.5 days of work per machine while 271 pools sat
      entirely idle. Keying load by (line, type) puts the balancing on the
      same granularity as the resource it is balancing.

    top_k > 1 picks randomly among the k least-loaded lines. Strict greedy
    would make every individual in a population segment identical; a little
    randomness at the tail keeps them distinct while staying near-optimal.
    """
    if not eligible_lines:
        return None
    pools = factory.machine_ids_per_line_map.get(op_key, {})
    optype = factory.optype_for_op.get(op_key)

    def load_per_machine(line_id):
        pool_size = max(1, len(pools.get(line_id, [])))
        return line_load.get((line_id, optype), 0.0) / pool_size

    ranked = sorted(eligible_lines, key=load_per_machine)
    if top_k <= 1:
        return ranked[0]
    return rng.choice(ranked[:min(top_k, len(ranked))])


def _work_of(factory, op_details, op_key):
    """Total single-machine work content, the quantity we balance lines on."""
    return float(op_details.get(op_key, {}).get('serial_processing_mins', 30.0))


# ---------------------------------------------------------------------------
# Sequencing rules
# ---------------------------------------------------------------------------

def _order_keys(op_keys, seq_lookup):
    """{order_id: [op_key, ...] in routing sequence}."""
    grouped = {}
    for key in op_keys:
        order_id = key.split('_')[0]
        grouped.setdefault(order_id, []).append(key)
    for order_id in grouped:
        grouped[order_id].sort(key=lambda k: seq_lookup.get(k, int(k.split('_')[1])))
    return grouped


def _emit_by_order_priority(grouped, priority_of_order, rng=random, jitter=0.0):
    """
    Emits every order's operations in routing sequence, with orders ranked by
    a priority rule. The result is precedence-valid by construction.

    jitter multiplies each order's key by a random factor in
    [1-jitter, 1+jitter], so a segment of the population seeded with the same
    rule still produces distinct individuals rather than N identical clones.
    """
    order_ids = list(grouped.keys())

    def key_of(order_id):
        base = priority_of_order(order_id)
        if jitter:
            return base * rng.uniform(1.0 - jitter, 1.0 + jitter)
        return base

    order_ids.sort(key=key_of)
    sequence = []
    for order_id in order_ids:
        sequence.extend(grouped[order_id])
    return sequence


def sequence_fifo(grouped, rng=random, jitter=0.0):
    """
    First-in-first-out: orders in arrival order.

    production_order_id is used as the arrival proxy — this schema has no
    explicit release timestamp, and the seeder allocates ids in creation
    order, so a lower id is an earlier order. Documented rather than hidden,
    because FIFO's whole meaning depends on what "first" is measured by.
    """
    return _emit_by_order_priority(grouped, lambda o: int(o), rng, jitter)


def sequence_edf(grouped, order_deadlines, rng=random, jitter=0.0):
    """
    Earliest deadline first. Orders with no known deadline sort last rather
    than first — an unknown date is not an urgent one.
    """
    def key(order_id):
        d = order_deadlines.get(order_id)
        return d if d is not None else float('inf')
    return _emit_by_order_priority(grouped, key, rng, jitter)


def _critical_ratio(order_id, grouped, order_deadlines, op_details):
    """
    THE critical-ratio formula: time remaining until due, divided by work
    still to do. Below 1.0 means the order cannot make its date at current
    pace. Factored out so sequence_critical_ratio and
    sequence_urgency_tiered (PROJECT_STATUS.txt Section 0H.11, which
    buckets orders by this SAME ratio into tiers) can never drift apart --
    a formula written out twice is exactly the kind of duplication this
    project has already paid for once (calculate_operation_work's
    docstring in ga/schedule_utils.py tells that story).

    Returns float('inf') when the order has no deadline or no work left
    (both already the original inline version's behaviour) -- "no evidence
    of urgency" must sort/tier as LEAST urgent, never crash or silently
    rank first.
    """
    deadline = order_deadlines.get(order_id)
    work = sum(_work_of(None, op_details, k) for k in grouped[order_id])
    if deadline is None or work <= 0:
        return float('inf')
    return deadline / work


def sequence_critical_ratio(grouped, order_deadlines, op_details, rng=random, jitter=0.0):
    """
    Critical ratio: time remaining until due, divided by work still to do.
    Below 1.0 means the order cannot make its date at current pace. Lower is
    more urgent, so ascending order is the dispatch order.

    Unlike EDF this accounts for how big the job is: an order due in ten days
    with two days of work left is comfortable, while one due in ten days with
    twenty days of work is already lost. That distinction is exactly what
    matters on a floor where order quantities vary by 8x.
    """
    return _emit_by_order_priority(
        grouped, lambda o: _critical_ratio(o, grouped, order_deadlines, op_details), rng, jitter)


# Minimum relative IQR (interquartile range / median) a window's finite
# critical ratios must show before sequence_urgency_tiered will bucket them
# into hard tiers at all. PROJECT_STATUS.txt Section 0H.11: percentile
# ranking guarantees every tier gets populated -- it does NOT guarantee the
# orders in "tier 0" are actually more urgent than the ones in "tier 3" by
# any meaningful margin. On this dataset every order has 21-75 days of
# release-to-deadline budget (Section 0H.10), so a window can easily contain
# no genuinely urgent orders at all; without this guard, percentile would
# still confidently label its bottom quartile "CRITICAL". Below this
# threshold, sequence_urgency_tiered degrades to the plain continuous
# sequence_critical_ratio sort instead of manufacturing artificial priority.
# A starting default, like every other business-policy threshold in this
# codebase (agent/fuzzy_logic.py's own anchors, RISK_BUFFER_AT_CERTAINTY,
# MC_PROTECTION_FRACTION) -- measured, and reported, against the real
# dataset rather than assumed correct; see Section 0H.11 for the measured
# fallback rate.
MIN_RELATIVE_IQR_FOR_TIERING = 0.10


def _tier_orders_by_critical_ratio(grouped, order_deadlines, op_details, num_tiers):
    """
    Buckets orders into `num_tiers` discrete urgency tiers by PERCENTILE
    RANK of _critical_ratio within THIS window -- not fixed ratio
    thresholds. Same reasoning as graph/labels.py's Head 3 fix (Section
    0H.10): a fixed threshold picked before seeing real data has already
    silently mismatched this dataset's actual scale twice (the delay
    label's constant-feature reversal, and urgency_score's day-scale
    anchors) -- a third hardcoded threshold here would repeat a failure
    mode this project has measured twice, not guard against a hypothetical
    one.

    Returns (tier_by_order, ratio_by_order, fell_back):
      tier_by_order: {order_id: int}, 0 = most urgent. Only populated when
          fell_back is False.
      ratio_by_order: {order_id: float}, every order in `grouped`,
          including float('inf') entries (no deadline/no work) -- always
          returned so a caller never has to recompute _critical_ratio.
      fell_back: True when there were fewer than 2 orders with a real
          (finite) ratio, or the finite ratios' spread was too narrow to
          tier meaningfully (see MIN_RELATIVE_IQR_FOR_TIERING) -- percentile
          ranking alone cannot detect "everyone is nearly equally urgent",
          since it always produces a populated bottom/top tier regardless
          of the underlying values.
    """
    ratio_by_order = {
        oid: _critical_ratio(oid, grouped, order_deadlines, op_details) for oid in grouped
    }
    finite = {oid: r for oid, r in ratio_by_order.items() if r != float('inf')}
    if len(finite) < 2:
        return {}, ratio_by_order, True

    values = sorted(finite.values())
    n = len(values)

    def _percentile_value(p):
        idx = min(n - 1, int(round(p * (n - 1))))
        return values[idx]

    median = _percentile_value(0.5)
    iqr = _percentile_value(0.75) - _percentile_value(0.25)
    relative_spread = (iqr / median) if median > 0 else float('inf')
    if relative_spread < MIN_RELATIVE_IQR_FOR_TIERING:
        return {}, ratio_by_order, True

    # (rank - 1) / (n - 1): the same full-[0,1]-span percentile-rank
    # construction as graph/labels.py::_percentile_rank_priority, so the
    # least-urgent and most-urgent orders in THIS window always anchor
    # tier 0 and tier num_tiers-1 exactly, regardless of the ratio's
    # absolute scale.
    ordered_ids = sorted(finite, key=lambda oid: finite[oid])
    tier_by_order = {}
    for rank, oid in enumerate(ordered_ids):
        percentile = rank / max(1, n - 1)
        tier_by_order[oid] = min(num_tiers - 1, int(percentile * num_tiers))
    return tier_by_order, ratio_by_order, False


def sequence_urgency_tiered(grouped, order_deadlines, op_details, rng=random, jitter=0.0, num_tiers=4):
    """
    Staged-release sequencing (PROJECT_STATUS.txt Section 0H.11): orders are
    bucketed into `num_tiers` discrete urgency tiers by percentile rank of
    _critical_ratio (_tier_orders_by_critical_ratio), then emitted as HARD
    WAVES -- every tier-0 (most urgent) order's operations placed before ANY
    tier-1 operation, and so on -- rather than the fine-grained continuous
    blend sequence_critical_ratio already produces.

    THE difference from sequence_critical_ratio, spelled out precisely:
    that function already ranks by this exact ratio (shared via
    _critical_ratio, so the two can never silently drift apart) but lets
    urgent and non-urgent orders freely interleave in the final sequence. A
    tier-1 order can still land ahead of a tier-0 order there, as long as
    its ratio happens to be close. Tiering forbids that: a tier-0 order's
    every operation reaches select_machines_and_start's machine-selection
    simulation before a single tier-1 operation is even considered. That is
    a different scheduling POLICY, not just a finer sort -- which is why
    the validation for this must be a head-to-head against
    sequence_critical_ratio specifically, not the general baseline table
    (see Section 0H.11).

    Falls back to sequence_critical_ratio's plain continuous sort when this
    window's critical-ratio spread is too narrow to tier meaningfully (see
    MIN_RELATIVE_IQR_FOR_TIERING and _tier_orders_by_critical_ratio) --
    logged, not silently swallowed, so the fallback rate is measurable.

    jitter applies to the RATIO component ONLY, within a tier. Jittering
    tier BOUNDARIES would let a merely-slightly-less-urgent order jump
    ahead of a more urgent one purely from population-diversity noise,
    defeating the entire point of a hard wave.
    """
    tier_by_order, ratio_by_order, fell_back = _tier_orders_by_critical_ratio(
        grouped, order_deadlines, op_details, num_tiers)

    if fell_back:
        logger.info(
            "sequence_urgency_tiered: this window's critical-ratio spread was too narrow "
            "to tier meaningfully (fewer than 2 orders with a real deadline+work, or "
            f"relative IQR below {MIN_RELATIVE_IQR_FOR_TIERING}) -- falling back to the "
            "continuous critical-ratio sort instead of forcing artificial tiers."
        )
        return sequence_critical_ratio(grouped, order_deadlines, op_details, rng, jitter)

    def key_of(order_id):
        ratio = ratio_by_order[order_id]
        if jitter and ratio != float('inf'):
            ratio = ratio * rng.uniform(1.0 - jitter, 1.0 + jitter)
        # no-deadline/no-work orders (absent from tier_by_order, since they
        # were excluded from the finite ratio population) sort into a tier
        # past every real one, i.e. dead last -- matching sequence_edf/
        # sequence_critical_ratio's existing "unknown is not urgent" rule.
        tier = tier_by_order.get(order_id, num_tiers)
        return (tier, ratio)

    # jitter=0.0 here deliberately -- key_of already applies jitter itself,
    # to the ratio component only (see the docstring above for why tier
    # boundaries themselves must stay un-jittered).
    return _emit_by_order_priority(grouped, key_of, rng, jitter=0.0)


def sequence_random(op_keys, op_sequence_map, rng=random):
    """Shuffled, then repaired back into routing order within each order."""
    shuffled = rng.sample(list(op_keys), len(op_keys))
    return repair_precedence(shuffled, op_sequence_map)


# ---------------------------------------------------------------------------
# Whole-chromosome construction
# ---------------------------------------------------------------------------

def build_chromosome(factory, op_details, sequence, mc_strategy='max',
                     line_top_k=1, rng=random):
    """
    Turns an operation sequence into a full chromosome by assigning lines
    work-aware and then machine counts.

    Lines are chosen in sequence order, so the load an operation sees is the
    load its predecessors actually created — the same order the simulation
    will later place them in.

    mc_strategy:
      'max'    - take the whole bounded pool. More machines shortens the
                 operation and raises labour utilisation. Measured effect on
                 fitness is modest (~5%) because total work is conserved, but
                 it is the right default given the business objective.
      'random' - uniform within the bound, for population diversity.
      'high'   - biased to the upper half; a compromise between the two.
    """
    line_load = {}
    la_array, mc_array = [], []

    for op_key in sequence:
        eligible = factory.eligibility_map.get(op_key, [])
        line_id = choose_line_least_loaded(op_key, eligible, line_load, factory, rng, line_top_k)
        if line_id is None:
            line_id = eligible[0] if eligible else 1
        cap = factory._max_machines_for(op_key, line_id)

        if mc_strategy == 'max':
            mc = cap
        elif mc_strategy == 'high':
            mc = rng.randint(max(1, (cap + 1) // 2), cap)
        else:
            mc = rng.randint(1, cap)

        la_array.append(line_id)
        mc_array.append(mc)
        # Charge the specific (line, machine type) POOL this operation will
        # occupy — not the line as a whole. See choose_line_least_loaded.
        pool_key = (line_id, factory.optype_for_op.get(op_key))
        line_load[pool_key] = line_load.get(pool_key, 0.0) + _work_of(
            factory, op_details, op_key
        ) + mc * float(op_details.get(op_key, {}).get('setup_mins', 15.0))

    return list(sequence) + la_array + mc_array


def build_population_seeds(factory, op_details, order_deadlines, seq_lookup,
                           n_individuals, rng=random):
    """
    Builds a diverse initial population.

    The point is that these are different IDEAS, not different random draws of
    one idea. Each strategy below is a defensible way to run a garment floor,
    and they disagree with each other about what matters — deadlines, job
    size, arrival order, or nothing at all. Crossover between individuals
    built on different principles has something real to recombine; crossover
    between fifty runs of the same greedy rule has almost nothing.

    Roughly equal shares, with the randomized segment kept deliberately
    unguided so the population retains genuine exploration capacity and does
    not converge on the heuristics' shared blind spots.

    Returns a list of raw gene lists (the caller wraps them as DEAP
    Individuals).
    """
    op_keys = list(factory.op_keys)
    grouped = _order_keys(op_keys, seq_lookup)

    strategies = [
        ('workload-balanced', lambda r: build_chromosome(
            factory, op_details, sequence_random(op_keys, seq_lookup, r), 'max', 2, r)),
        ('edf', lambda r: build_chromosome(
            factory, op_details, sequence_edf(grouped, order_deadlines, r, jitter=0.15),
            'max', 2, r)),
        ('critical-ratio', lambda r: build_chromosome(
            factory, op_details,
            sequence_critical_ratio(grouped, order_deadlines, op_details, r, jitter=0.15),
            'max', 2, r)),
        ('fifo', lambda r: build_chromosome(
            factory, op_details, sequence_fifo(grouped, r, jitter=0.15), 'high', 2, r)),
        ('randomized', lambda r: build_chromosome(
            factory, op_details, sequence_random(op_keys, seq_lookup, r), 'random', 99, r)),
    ]

    population, counts = [], {}
    for i in range(n_individuals):
        name, make = strategies[i % len(strategies)]
        population.append(make(rng))
        counts[name] = counts.get(name, 0) + 1

    logger.info(f"Seeded initial population from {len(strategies)} strategies: {counts}")
    return population


# ---------------------------------------------------------------------------
# Standalone baselines
# ---------------------------------------------------------------------------

def baseline_chromosomes(factory, op_details, order_deadlines, seq_lookup):
    """
    The pure, un-jittered heuristics, for benchmarking rather than seeding.
    Each is fully deterministic so the numbers in a results chapter are
    reproducible.

    All five use the same work-aware line assignment and the same machine-count
    policy, so the comparison isolates the SEQUENCING rule — which is the thing
    a dispatching heuristic actually decides. Comparing rules that also differ
    in how they assign lines would confound the two.

    UrgencyTiered (PROJECT_STATUS.txt Section 0H.11) is here specifically so
    it lands in this same table CriticalRatio already does, unmodified —
    that head-to-head, not the whole-table comparison, is the decisive
    validation for whether hard tier-blocking beats CriticalRatio's
    continuous blend of the identical underlying ratio. Not yet added to
    build_population_seeds above: promotion to an actual GA-seeding
    strategy is conditional on this comparison, per the scoped-and-approved
    validation plan.
    """
    op_keys = list(factory.op_keys)
    grouped = _order_keys(op_keys, seq_lookup)
    fixed = random.Random(42)

    return {
        'FIFO': build_chromosome(factory, op_details, sequence_fifo(grouped), 'max', 1, fixed),
        'EDF': build_chromosome(
            factory, op_details, sequence_edf(grouped, order_deadlines), 'max', 1, fixed),
        'CriticalRatio': build_chromosome(
            factory, op_details,
            sequence_critical_ratio(grouped, order_deadlines, op_details), 'max', 1, fixed),
        'UrgencyTiered': build_chromosome(
            factory, op_details,
            sequence_urgency_tiered(grouped, order_deadlines, op_details), 'max', 1, fixed),
        'RandomRepaired': build_chromosome(
            factory, op_details, sequence_random(op_keys, seq_lookup, fixed), 'max', 1, fixed),
    }
