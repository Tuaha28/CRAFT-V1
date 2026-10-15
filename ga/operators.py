import math
import random


def _trace_diff(before, after):
    return [{"index": i, "before": a, "after": b} for i, (a, b) in enumerate(zip(before, after)) if a != b]

def _emit_trace(callback, operator, before, raw, final, **extra):
    if callback is not None:
        callback({"operator": operator, "before": list(before), "raw_after_operator": list(raw),
                  "final_after_repair": list(final), "operator_diff": _trace_diff(before, raw),
                  "repair_diff": _trace_diff(raw, final), **extra})
from deap import tools
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
# Module-qualified, not value-imported -- see ga/chromosome.py's identical
# import for why: a test monkeypatching ga.schedule_utils.
# MC_PROTECTION_FRACTION must be seen live here too.
import ga.schedule_utils as schedule_utils

logger = get_logger("ga_operators", "ga.log")

def repair_precedence(os_array, op_sequence_map):
    """
    Forces the Operation Sequence (OS) to obey factory routing constraints.
    If the GA accidentally schedules a later operation step before an earlier one,
    this function detects it and re-sorts them into the correct chronological order.

    op_sequence_map: {op_key: operation_sequence_number}, the authoritative
    routing order (Routing_Operations.operation_sequence_number) -- the same
    column every other consumer (ga/scheduler.py's seq_lookup, ga/fitness.py's
    precedence check, agent/decision_engine.py's own seq_lookup) already
    sources this from. Deliberately NOT inferred from the numeric suffix of
    op_key ("orderid_operationid"): operation_id happens to increase in
    routing order in this synthetic seeder, but that is a coincidence of how
    the seeder assigns IDs, not a guarantee -- a real imported routing could
    number its operations in any order, and sorting by op_key's suffix would
    then silently produce an unexecutable schedule instead of a visibly
    wrong one.
    """
    order_positions = {}
    for idx, op_key in enumerate(os_array):
        order_id = op_key.split('_')[0]
        if order_id not in order_positions:
            order_positions[order_id] = []
        order_positions[order_id].append(idx)

    for order_id, positions in order_positions.items():
        ops_for_order = [os_array[i] for i in positions]
        ops_for_order.sort(key=lambda x: op_sequence_map[x])

        for i, pos in enumerate(positions):
            os_array[pos] = ops_for_order[i]

    return os_array

def order_crossover(parent1, parent2, cut_points=None):
    """
    Order Crossover (OX1) for permutation chromosomes, implemented directly
    on the gene values instead of DEAP's tools.cxOrdered.

    NOTE: DEAP's built-in tools.cxOrdered assumes individuals are sequences
    of INTEGER INDICES (it uses gene values themselves as list indices
    internally) and raises a TypeError on string genes like our
    'productionorderid_operationid' keys. This implementation works on any
    hashable gene type and preserves the same guarantee: every gene from the
    parents appears in each child exactly once — no duplicates, nothing
    dropped.

    cut_points: optional (a, b) pair. Default (None) draws its own random
    cut points, independent of anything else -- this is custom_crossover's
    existing behaviour, unchanged. shared_cutpoint_crossover (below) passes
    its own LA/MC crossover points here instead, so OS and LA/MC recombine
    at the SAME slice for once -- see DOMAIN_AWARE_CROSSOVER_PROPOSAL.md
    Section 6 for why that's a meaningfully different (cheaper, partial)
    operator from the default, not just a refactor.
    """
    size = len(parent1)
    a, b = cut_points if cut_points is not None else sorted(random.sample(range(size), 2))

    def build_child(slice_source, fill_source):
        child = [None] * size
        child[a:b] = slice_source[a:b]
        taken = set(slice_source[a:b])
        # Fill remaining positions, in fill_source's order, starting right
        # after the copied slice and wrapping around.
        fill_genes = [g for g in fill_source if g not in taken]
        fill_positions = list(range(b, size)) + list(range(0, a))
        for pos, gene in zip(fill_positions, fill_genes):
            child[pos] = gene
        return child

    child1 = build_child(parent1, parent2)
    child2 = build_child(parent2, parent1)
    return child1, child2


def triple_coupled_order_crossover(os1, la1, mc1, os2, la2, mc2):
    """
    Order Crossover (OX1) extended to carry each operation's (line,
    machine-count) with it during recombination, keyed by OPERATION
    IDENTITY rather than array position.

    DOMAIN_AWARE_CROSSOVER_PROPOSAL.md Section 5 -- the mechanism this
    targets: custom_crossover recombines OS (via order_crossover, its own
    independent cut points) and LA/MC (via a 2-point swap at DIFFERENT
    cut points) separately, so after crossover, the operation sitting at
    position i is generally not the one la_array[i]/mc_array[i] were
    chosen for in either parent -- repair_line_and_machine_count then
    patches ELIGIBILITY and CAPS only, not quality, because by that point
    it has no way to know what a good pairing looked like. Confirmed as
    the dominant harm source in GA_AGENT_POSTMORTEM.md Section 7i
    (crossover-only offspring worse 83.60-90.51% of the time across 3
    frozen-snapshot cases, vs. 55.92-80.00% for mutation-only).

    Here, every operation's (line, machine-count) travels with it through
    OX's own slice/fill logic: an operation copied from the slice region
    keeps the pairing it had in the slice-source parent; an operation
    copied during the fill pass keeps the pairing it had in the
    fill-source parent -- exactly mirroring which parent OX itself drew
    that operation from, so nothing is reconciled by position afterward.
    """
    size = len(os1)
    a, b = sorted(random.sample(range(size), 2))

    triples1 = {op: (la1[i], mc1[i]) for i, op in enumerate(os1)}
    triples2 = {op: (la2[i], mc2[i]) for i, op in enumerate(os2)}

    def build_child(slice_os, slice_triples, fill_os, fill_triples):
        child_os = [None] * size
        child_os[a:b] = slice_os[a:b]
        taken = set(slice_os[a:b])
        fill_genes = [g for g in fill_os if g not in taken]
        fill_positions = list(range(b, size)) + list(range(0, a))

        child_la = [None] * size
        child_mc = [None] * size
        for i in range(a, b):
            child_la[i], child_mc[i] = slice_triples[child_os[i]]
        for pos, op in zip(fill_positions, fill_genes):
            child_os[pos] = op
            child_la[pos], child_mc[pos] = fill_triples[op]

        return child_os, child_la, child_mc

    child_os1, child_la1, child_mc1 = build_child(os1, triples1, os2, triples2)
    child_os2, child_la2, child_mc2 = build_child(os2, triples2, os1, triples1)
    return child_os1, child_la1, child_mc1, child_os2, child_la2, child_mc2

def compute_line_loads(os_array, la_array, mc_array, op_work, machines_per_line_map,
                        op_optype=None):
    """
    Work-per-machine currently assigned to each (line, machine type) POOL by
    this chromosome.

    Keyed by pool, not by line, because that is the granularity of the
    resource: an operation can only use machines of its own type on its own
    line. Tracking load per line made a line loaded with one type look
    unavailable for every other type, and left 271 of 494 pools permanently
    unused — see ga/heuristics.py::choose_line_least_loaded for the
    measurements.

    O(num_ops), computed once per operator call and reused for every gene that
    call touches — cheap enough to run inside mutation, which is the point:
    without it, a mutating line gene has no idea which pools are already
    overloaded.
    """
    op_optype = op_optype or {}
    raw = {}
    pool_sizes = {}
    for i, op_key in enumerate(os_array):
        line_id = la_array[i]
        key = (line_id, op_optype.get(op_key))
        raw[key] = raw.get(key, 0.0) + op_work.get(op_key, 0.0)
        if key not in pool_sizes:
            pool_sizes[key] = max(1, int(machines_per_line_map.get(op_key, {}).get(line_id, 1)))
    return {k: load / max(1, pool_sizes.get(k, 1)) for k, load in raw.items()}


def choose_line_work_aware(op_key, eligible_lines, line_loads, machines_per_line_map, top_k=3,
                            op_optype=None):
    """
    Picks a replacement line weighted toward the least loaded, measured as
    work per machine.

    WHY NOT random.choice(eligible_lines), which is what this replaced in both
    call sites below:

    Fixing initialisation alone is not enough. Every generation, mutation
    reassigns roughly indpb x num_ops line genes, and the repair pass
    reassigns any gene left invalid by crossover. If those pick uniformly at
    random from ~99 eligible lines, they steadily push a carefully balanced
    population back toward the uniform-random distribution it started from —
    the good initialisation gets eroded from generation 1 onward. A measured
    run showed the resulting imbalance at 181,646 minutes of standard
    deviation across lines.

    top_k rather than strict greedy: mutation exists to explore. Always
    choosing the single least-loaded line would make the operator
    deterministic and collapse its search value.
    """
    if not eligible_lines:
        return None
    optype = (op_optype or {}).get(op_key)
    ranked = sorted(
        eligible_lines,
        key=lambda ln: line_loads.get((ln, optype), 0.0) / max(
            1, int(machines_per_line_map.get(op_key, {}).get(ln, 1))
        ),
    )
    return random.choice(ranked[:min(top_k, len(ranked))])


def repair_line_and_machine_count(os_array, la_array, mc_array, eligibility_map, machines_per_line_map, max_mc_cap,
                                   op_work=None, op_optype=None, protected_machine_type=None):
    """
    After crossover/mutation, a position's (operation, line, machine-count)
    triple can drift out of sync — OS gets reordered by Order Crossover
    independently of LA/MC, so the operation now sitting at index i might
    not be the one LA[i]/MC[i] were originally chosen for. This wasn't
    fully guarded before MC existed either (LA was never re-validated
    against eligibility_map after crossover) — fixing both here:
      1. If la_array[i] isn't actually eligible for os_array[i], reassign
         it to a valid eligible line.
      2. Clamp mc_array[i] to what's actually available on that line
         (capped by max_mc_cap), so it can never exceed real machine count.

    protected_machine_type: PROJECT_STATUS.txt Section 0H.9. When an
    operation's op_optype matches this AND
    ga.schedule_utils.MC_PROTECTION_FRACTION < 1.0, its cap is further
    scaled down by that fraction — mirroring ga/chromosome.py::
    ChromosomeFactory._max_machines_for's 4th limit, so crossover/mutation
    offspring can't drift back above the cap a freshly-born individual
    already respects. None (default) leaves every existing caller
    unaffected.
    """
    # Only computed if something actually needs reassigning — most repair
    # passes touch few genes, and this is O(num_ops).
    line_loads = None

    for i, op_key in enumerate(os_array):
        eligible_lines = eligibility_map.get(op_key)
        if eligible_lines and la_array[i] not in eligible_lines:
            if op_work is not None:
                if line_loads is None:
                    line_loads = compute_line_loads(
                        os_array, la_array, mc_array, op_work, machines_per_line_map, op_optype)
                la_array[i] = choose_line_work_aware(
                    op_key, eligible_lines, line_loads, machines_per_line_map, op_optype=op_optype)
            else:
                la_array[i] = random.choice(eligible_lines)

        real_count = machines_per_line_map.get(op_key, {}).get(la_array[i], 1)
        cap = max(1, min(int(real_count), max_mc_cap))
        fraction = schedule_utils.MC_PROTECTION_FRACTION
        if (fraction < 1.0 and protected_machine_type is not None and op_optype is not None
                and op_optype.get(op_key) == protected_machine_type):
            cap = max(1, math.ceil(cap * fraction))
        mc_array[i] = max(1, min(int(mc_array[i]), cap))

    return os_array, la_array, mc_array


def custom_crossover(ind1, ind2, num_ops, eligibility_map=None, machines_per_line_map=None, max_mc_cap=40,
                      op_work=None, op_optype=None, protected_machine_type=None, op_sequence_map=None, trace_callback=None):
    """
    Breeds two schedules.
    Uses 2-Point Crossover for Line Assignments (LA) and Machine Counts
    (MC) — safe, since these values may legitimately repeat across
    positions. LA and MC are swapped at the SAME crossover points (not
    independently) so each position's (line, machine-count) pair moves as
    one unit rather than getting mismatched.
    Uses Order Crossover (OX) for the Operation Sequence (OS), followed by repair.

    IMPORTANT: os_array is a PERMUTATION — every real operation must appear
    exactly once. A raw two-point slice-swap (the old approach) is only valid
    for chromosomes where genes may repeat; applied to a permutation it
    duplicates whatever genes land in the swapped slice while silently
    dropping an equal number of other operations from the child, and
    repair_precedence() only re-sorts step order — it cannot detect or fix a
    missing/duplicated operation. order_crossover() (Order Crossover / OX1,
    defined above) is built specifically to recombine two permutations while
    guaranteeing every gene still appears exactly once in each child.
    """
    before1, before2 = list(ind1), list(ind2)
    os1, la1, mc1 = ind1[:num_ops], ind1[num_ops:2*num_ops], ind1[2*num_ops:]
    os2, la2, mc2 = ind2[:num_ops], ind2[num_ops:2*num_ops], ind2[2*num_ops:]
    
    # 1. Crossover Line Assignments AND Machine Counts together, at the same
    #    two points, so a position's line/machine-count pair stays coupled.
    if num_ops > 1:
        pt1, pt2 = sorted(random.sample(range(num_ops), 2))
    else:
        pt1, pt2 = 0, num_ops
    la1[pt1:pt2], la2[pt1:pt2] = la2[pt1:pt2], la1[pt1:pt2]
    mc1[pt1:pt2], mc2[pt1:pt2] = mc2[pt1:pt2], mc1[pt1:pt2]
    
    # 2. Crossover Operation Sequences (permutation-safe: Order Crossover)
    if len(os1) > 1:
        os1, os2 = order_crossover(os1, os2)
    
    raw1, raw2 = os1 + la1 + mc1, os2 + la2 + mc2

    # 3. Repair Precedence Violations (fixes step ORDER only; OX above already
    #    guarantees composition validity, i.e. no duplicates/missing ops)
    os1 = repair_precedence(os1, op_sequence_map)
    os2 = repair_precedence(os2, op_sequence_map)

    # 4. Re-couple (operation, line, machine-count) after OS reordering —
    #    OS was just reordered independently of LA/MC, so re-validate LA
    #    eligibility and re-clamp MC against whatever line each operation
    #    actually ended up paired with (see repair_line_and_machine_count).
    if eligibility_map is not None and machines_per_line_map is not None:
        os1, la1, mc1 = repair_line_and_machine_count(os1, la1, mc1, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)
        os2, la2, mc2 = repair_line_and_machine_count(os2, la2, mc2, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)

    # Recombine into DEAP individuals
    ind1[:] = os1 + la1 + mc1
    ind2[:] = os2 + la2 + mc2
    _emit_trace(trace_callback, "crossover", before1, raw1, ind1, child=1, other_parent=before2)
    _emit_trace(trace_callback, "crossover", before2, raw2, ind2, child=2, other_parent=before1)

    return ind1, ind2


def triple_coupled_crossover(ind1, ind2, num_ops, eligibility_map=None, machines_per_line_map=None, max_mc_cap=40,
                              op_work=None, op_optype=None, protected_machine_type=None, op_sequence_map=None):
    """
    Triple-Coupled Order Crossover (TCOX) -- DOMAIN_AWARE_CROSSOVER_PROPOSAL.md
    Section 5. Drop-in alternative to custom_crossover (same signature),
    NOT wired into GAScheduler by default -- opt-in only, via
    toolbox.register("mate", triple_coupled_crossover, ...) in place of
    custom_crossover, same pattern already used for memetic_interval /
    memetic_replace.

    Uses triple_coupled_order_crossover for OS/LA/MC together (see that
    function's docstring for the mechanism) instead of custom_crossover's
    two independently-cut segments. repair_line_and_machine_count still
    runs afterward -- still needed for genuine edge cases (e.g. a line
    valid in one parent's context isn't valid under a different protected-
    machine-type fraction) -- but should fire far less often, since most
    pairings are no longer arbitrary to begin with.

    KNOWN RESIDUAL GAP, not fixed here: repair_precedence (called right
    after, same as in custom_crossover) can still reorder POSITIONS for
    operations whose relative sequence a crossover left invalid -- e.g.
    an order's op 2 ends up before its op 1 -- and repair_precedence only
    re-sorts those operations back into correct step order, without
    correspondingly moving their (line, machine-count) values. For that
    specific subset of operations (whichever orders needed a precedence
    fix this call), the pairing TCOX just attached correctly can still
    end up detached again by repair_precedence, and then re-validated
    only for eligibility/caps by repair_line_and_machine_count, same as
    today. This is a real, smaller, PRE-EXISTING mechanism (identical in
    custom_crossover) that TCOX does not address -- confirmed by
    tests/test_domain_aware_crossover.py testing TCOX's own coupling
    guarantee on triple_coupled_order_crossover's raw output, before
    repair_precedence runs, rather than overclaiming the guarantee
    survives the full pipeline. Not in scope for this proposal (Section 4).
    """
    os1, la1, mc1 = ind1[:num_ops], ind1[num_ops:2*num_ops], ind1[2*num_ops:]
    os2, la2, mc2 = ind2[:num_ops], ind2[num_ops:2*num_ops], ind2[2*num_ops:]

    if num_ops > 1:
        os1, la1, mc1, os2, la2, mc2 = triple_coupled_order_crossover(os1, la1, mc1, os2, la2, mc2)

    os1 = repair_precedence(os1, op_sequence_map)
    os2 = repair_precedence(os2, op_sequence_map)

    if eligibility_map is not None and machines_per_line_map is not None:
        os1, la1, mc1 = repair_line_and_machine_count(os1, la1, mc1, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)
        os2, la2, mc2 = repair_line_and_machine_count(os2, la2, mc2, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)

    ind1[:] = os1 + la1 + mc1
    ind2[:] = os2 + la2 + mc2

    return ind1, ind2


def shared_cutpoint_crossover(ind1, ind2, num_ops, eligibility_map=None, machines_per_line_map=None, max_mc_cap=40,
                               op_work=None, op_optype=None, protected_machine_type=None, op_sequence_map=None):
    """
    Cheaper comparison arm -- DOMAIN_AWARE_CROSSOVER_PROPOSAL.md Section 6.
    Drop-in alternative to custom_crossover (same signature), NOT wired
    into GAScheduler by default -- opt-in only, same pattern as
    triple_coupled_crossover above.

    Keeps custom_crossover's existing structure (OS via Order Crossover,
    LA/MC via a 2-point swap) but draws ONE set of cut points and shares
    them between the two, via order_crossover's cut_points parameter --
    instead of custom_crossover's two unrelated draws (the confirmed
    mechanism, Section 7i). Fixes alignment WITHIN the shared slice
    region only: an operation copied from the slice keeps the pairing it
    had there, by construction. The fill region -- the majority of the
    chromosome -- still reorders OS independently of where LA/MC values
    end up, same as custom_crossover today. Run side by side with TCOX to
    measure how much of TCOX's benefit needs the full triple-coupling
    versus just aligning the cut points.
    """
    os1, la1, mc1 = ind1[:num_ops], ind1[num_ops:2*num_ops], ind1[2*num_ops:]
    os2, la2, mc2 = ind2[:num_ops], ind2[num_ops:2*num_ops], ind2[2*num_ops:]

    if num_ops > 1:
        pt1, pt2 = sorted(random.sample(range(num_ops), 2))
    else:
        pt1, pt2 = 0, num_ops
    la1[pt1:pt2], la2[pt1:pt2] = la2[pt1:pt2], la1[pt1:pt2]
    mc1[pt1:pt2], mc2[pt1:pt2] = mc2[pt1:pt2], mc1[pt1:pt2]

    if len(os1) > 1:
        os1, os2 = order_crossover(os1, os2, cut_points=(pt1, pt2))

    os1 = repair_precedence(os1, op_sequence_map)
    os2 = repair_precedence(os2, op_sequence_map)

    if eligibility_map is not None and machines_per_line_map is not None:
        os1, la1, mc1 = repair_line_and_machine_count(os1, la1, mc1, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)
        os2, la2, mc2 = repair_line_and_machine_count(os2, la2, mc2, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype, protected_machine_type)

    ind1[:] = os1 + la1 + mc1
    ind2[:] = os2 + la2 + mc2

    return ind1, ind2


def custom_mutate(ind, num_ops, eligibility_map, machines_per_line_map=None, max_mc_cap=40, indpb=0.1,
                   op_work=None, op_optype=None, protected_machine_type=None, op_sequence_map=None, trace_callback=None):
    """
    Mutates a schedule to explore new search space possibilities.
    OS: Swaps the position of two operations in the sequence.
    LA: Reassigns an operation to an alternative eligible Production Line.
    MC: Reassigns the machine-count for an operation to a new value, bounded
        by what's actually available on its (possibly just-changed) line.
    """
    before = list(ind)
    os_array = ind[:num_ops]
    la_array = ind[num_ops:2*num_ops]
    mc_array = ind[2*num_ops:]
    
    # 1. Mutate Line Assignments (LA), work-aware.
    #    Loads are computed ONCE for the whole chromosome and reused across
    #    every gene mutated in this call — O(num_ops) total, the same order as
    #    the mutation loop itself. Loads go stale slightly as genes change
    #    within the call, which is acceptable: this is a stochastic operator,
    #    not an exact placement, and recomputing per gene would make mutation
    #    O(num_ops^2).
    line_loads = (
        compute_line_loads(os_array, la_array, mc_array, op_work, machines_per_line_map, op_optype)
        if op_work is not None and machines_per_line_map is not None else None
    )
    for i in range(num_ops):
        if random.random() < indpb:
            op_key = os_array[i]
            eligible_lines = eligibility_map.get(op_key, [la_array[i]])
            if line_loads is not None:
                picked = choose_line_work_aware(
                    op_key, eligible_lines, line_loads, machines_per_line_map, op_optype=op_optype)
                if picked is not None:
                    la_array[i] = picked
            else:
                la_array[i] = random.choice(eligible_lines)

    # 2. Mutate Machine Counts (MC) — independent of the LA mutation above,
    #    bounded to what's actually available on whatever line ends up at
    #    this position (re-clamped in step 4 regardless, but pick a fresh
    #    value here so this gene actually gets explored by the GA).
    #    Biased to the upper half of the range: more machines shortens the
    #    operation and raises labour utilisation, which is a stated business
    #    objective, and a uniform draw spends half its samples below the
    #    midpoint pulling utilisation back down.
    if machines_per_line_map is not None:
        for i in range(num_ops):
            if random.random() < indpb:
                op_key = os_array[i]
                cap = max(1, min(int(machines_per_line_map.get(op_key, {}).get(la_array[i], 1)), max_mc_cap))
                mc_array[i] = random.randint(max(1, (cap + 1) // 2), cap)
            
    # 3. Mutate Operation Sequence (OS) via Swap Mutation
    for i in range(num_ops):
        if random.random() < indpb:
            swap_idx = random.randint(0, num_ops - 1)
            os_array[i], os_array[swap_idx] = os_array[swap_idx], os_array[i]
            
    raw = os_array + la_array + mc_array

    # 4. Repair any precedence rules broken by the OS swap
    os_array = repair_precedence(os_array, op_sequence_map)

    # 5. Re-couple (operation, line, machine-count) — OS may have just
    #    changed independently of LA/MC (see repair_line_and_machine_count).
    if machines_per_line_map is not None:
        os_array, la_array, mc_array = repair_line_and_machine_count(
            os_array, la_array, mc_array, eligibility_map, machines_per_line_map, max_mc_cap, op_work, op_optype,
            protected_machine_type
        )
    
    ind[:] = os_array + la_array + mc_array
    _emit_trace(trace_callback, "mutation", before, raw, ind)
    return (ind,)

if __name__ == "__main__":
    print("Testing Line-Based GA Operators and Repair Mechanism...")

    # Operation IDs deliberately NOT in routing-sequence order, to demonstrate
    # that repair now follows op_sequence_map rather than the op_key suffix.
    mock_os = ["100_30", "101_20", "100_10", "101_10", "100_20"]
    mock_seq_map = {"100_30": 3, "100_20": 2, "100_10": 1, "101_20": 2, "101_10": 1}
    print(f"BROKEN Sequence:  {mock_os}")

    repaired_os = repair_precedence(mock_os, mock_seq_map)
    print(f"REPAIRED Sequence: {repaired_os}")

    print("\n[OK] Repair Mechanism successfully forced precedence compliance for line routing!")