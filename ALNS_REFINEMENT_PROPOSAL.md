# GA + ALNS Hybrid — Scoping Proposal (no code changes)

Same discipline as `WLC_RELEASE_GATE_PROPOSAL.md`, `DOMAIN_AWARE_CROSSOVER_
PROPOSAL.md`, and `CP_SAT_PROPOSAL.md`: scope first, no code changes yet.
Every claim about this project's current refinement stage is tied to a
line in `ga/local_search.py` (read in full for this document), not a
generic description of ALNS.

## 1. Why this, why now

`GA_AGENT_POSTMORTEM.md` Section 7a's own headline finding: across 50
generations, crossover/mutation/elitism/immigrants improve the initial
population by 0.035% — the GA's evolutionary loop contributes almost
nothing. The one-shot local-search polish that runs once at the end
contributes +0.615% on the same run, ~17.5x more. Two consecutive,
careful attempts to improve the EVOLUTIONARY side of this system —
memetic integration and TCOX — both landed on real-win/real-loss/wash
records with no predictive rule for which case benefits
(`GA_AGENT_POSTMORTEM.md` Sections 7f, 7j; `GA validation snapshot tool`
memory). CP-SAT, evaluated as a from-scratch alternative, could not find
a feasible schedule for this problem size under the simplification
tested (`CP_SAT_PROPOSAL.md` Section 10).

None of that work touched the actual lever `local_search.py`'s own
docstring already identifies as where the real gains come from: the
REFINEMENT stage. And that stage, as it stands today, is a fairly simple
single-move hill-climber (Section 2) — never redesigned itself, only
ever the target of a one-shot budget. This is a different, well-motivated
next lever: not another evolutionary-operator tweak, not a from-scratch
replacement, but a proper iterative search applied to the part of this
system already shown to matter most.

## 2. The current one-shot polish, exactly

`ga/local_search.py::polish()` — hill-climbing, ONE gene-level move per
evaluation, strict-improvement-only acceptance:

- **Targeting**: every move is aimed at one of the top-5 most-loaded
  `(line, operation-type)` pools (`compute_pool_loads`, `top_k_pools = 5`)
  — the pool actually bounding the schedule's length, re-ranked fresh
  each iteration as the chromosome changes.
- **Three move types** (`MOVE_GENERATORS = ('swap', 'line', 'machines')`),
  one operation touched per move: `_move_swap_sequence` (swap two
  operations competing for the critical pool), `_move_reassign_line`
  (move one operation off the critical pool to the least-loaded eligible
  alternative), `_move_adjust_machines` (nudge one operation's
  machine-count up or down by exactly 1).
- **Acceptance**: strict improvement only (`fit < best_fit - 1e-9`) —
  never accepts a worse or equal candidate, so the returned result can
  never be worse than the input. This guarantee is real and tested
  (mirrors the elitism-never-regresses guarantee tested elsewhere in this
  project) and is explicitly worth preserving, not an accident.
- **Budget**: `max_evals` (400 in every full comparison run this
  session), with an early-stop `patience` of consecutive failures.
- **What it is NOT**: there is no destroy/repair structure — each
  candidate touches exactly one operation. There is no operator
  adaptivity — `move = rng.choice(MOVE_GENERATORS)`, uniform every time,
  regardless of which move type has actually been paying off. There is
  no way to escape a local optimum the way a permissive-acceptance search
  can — it is a pure hill-climb.

## 3. Scope: the sequential ALNS refinement stage

Per instruction, this proposal is scoped narrowly: **replace ONLY the
refinement stage** (`polish()`), applied to the SAME construction output
this project already uses. It does NOT touch:

- **Construction**: `ChromosomeFactory.generate_random_individual` /
  `ga/heuristics.py::build_population_seeds` / `baseline_chromosomes` —
  unchanged.
- **The evolutionary population loop**: `GAScheduler.optimize()`'s
  generations of crossover/mutation/selection/elitism — unchanged,
  untouched by this proposal. (Section 6 states explicitly why the
  validation plan does not even exercise this loop for the comparison.)
- **Employee resolution**: same downstream deterministic step it already
  is, same boundary `CP_SAT_PROPOSAL.md` drew.

### 3a. Destroy operators

Multi-operation removal, not single-gene perturbation — pick a SUBSET of
operation indices and mark their `(line, machine-count)` as torn out
(position in `os_array` stays fixed; only their resource assignment is
undone), for the repair step to reconstruct. Three operators, matching
what was specifically requested plus the standard ALNS baseline:

- **High-delay-risk removal**: remove the operations with the highest
  GNN-predicted `delay` probability (`gnn_predictions[op]['delay']`,
  already computed, already used for the risk-buffer cushion) — the
  operations the model itself flags as most likely to run late are worth
  re-placing, not just re-cushioning.
- **Bottleneck-pool removal**: remove operations currently on the
  busiest `(line, type)` pool — the exact concept `local_search.py`
  already targets (`compute_pool_loads`), generalized from "nudge one
  operation" to "tear out a batch and let repair rebuild the pool's
  assignment properly."
- **Random removal**: a uniform-random subset, unrelated to any signal —
  the standard ALNS baseline operator (Ropke & Pisinger), included so
  the two targeted operators are judged against a neutral comparison
  point, not just against each other.

Removal size (how many operations per destroy call) is itself a real
design parameter, not resolved here — a small, fixed fraction of `num_ops`
(e.g., 2-5%) is the conventional ALNS starting point, worth confirming
empirically rather than assuming, per Section 6.

### 3b. Repair mechanism — existing construction heuristics, per
     instruction

For each torn-out operation, re-assign a line via
`ga/heuristics.py::choose_line_least_loaded` or
`ga/operators.py::choose_line_work_aware` (both already exist, already
tested, already used for exactly this purpose elsewhere in this
codebase) — reusing established, trusted logic rather than inventing a
new placement heuristic. Machine-count is redrawn the same way
`custom_mutate`'s own MC step already does (upper-half-biased within the
new line's cap). No new construction logic — the explicit point of using
"existing construction heuristics... initially."

### 3c. Adaptive operator weighting

Standard ALNS mechanism (Ropke & Pisinger), applied to the three destroy
operators above: each operator carries a weight, initialized equal;
selection each iteration is roulette-wheel, proportional to current
weight. After each destroy-repair-accept/reject cycle, the operator used
earns a score (large if it produced a new best-ever, smaller if it
improved the current working solution without beating the best, smallest
if accepted anyway under Section 3d's acceptance rule, zero if rejected).
Weights update by exponential smoothing over fixed-length segments
(`weight = weight * (1 - reaction_factor) + reaction_factor * segment_score`),
so an operator that is actually paying off on THIS run's landscape gets
selected more often as the search progresses — directly answering the
brief's own framing: "adaptively weighting which destroy operators
actually produce improvements over time," not assumed in advance.

### 3d. Acceptance criterion — a real design choice, stated explicitly

This is where ALNS differs most from the existing hill-climb, and where
this project's own history argues for caution. The existing `polish()`'s
strict-improvement-only rule is a genuine, tested safety guarantee (never
returns worse than it started). A destroy-repair search that ONLY ever
accepts strict improvements is closer to today's hill-climb with bigger
moves than to real ALNS — the escaping-local-optima character that makes
ALNS worth trying at all comes from a more permissive acceptance rule
(simulated-annealing-style: accept a worse candidate with a probability
that decays over the run).

**Recommendation**: track TWO solutions through the search, not one —
the `current` working solution (which a permissive SA-style rule may
let wander to something temporarily worse) and `best-ever` (which only
ever updates on a genuine improvement, exactly like today's `polish()`).
Return `best-ever`. This preserves the existing "never finishes worse
than what went in" guarantee for the STAGE's OUTPUT — the same property
this project's tests already check for the current polish and for
elitism — while still giving the search itself room to explore. A
strict-improvement-only variant (no SA acceptance, `current == best`
always) is a legitimate, simpler fallback to test alongside this if the
permissive version does not pay off, but is not expected to look
meaningfully different from a bigger-neighbourhood hill-climb — the
point of this proposal is to test the version that could plausibly
behave differently, not the version most likely to just replicate
Section 2's existing result under a new name.

### 3e. Stopping criterion — matched evaluation budget, not cycle count

Each `evaluate_schedule` call costs the same wall-clock time regardless
of how many genes a candidate changed (it re-simulates the whole
chromosome) — an ALNS destroy-repair cycle is NOT cheaper than one of
today's single-gene hill-climb moves per evaluation, it just explores a
bigger neighbourhood per evaluation. A fair first comparison should match
TOTAL EVALUATION COUNT to today's `max_evals=400` convention (Section 6),
not cycle count or wall-clock time directly — consistent with how every
other comparison in this investigation (memetic, TCOX) was scoped on
evaluation/generation counts, not raw seconds.

## 4. CP-SAT-as-repair — documented future option, not built now

Per instruction, noted and left for later: instead of a heuristic repair
step, a SMALL CP-SAT model could optimally re-insert just the torn-out
subset (a handful to a few dozen operations, not the full 2,541-operation
window that `CP_SAT_PROPOSAL.md` Sections 8-9 could not solve). This
plausibly sidesteps the scale problem that closed the CP-SAT investigation
— a torn-out subset is a dramatically smaller instance — but is a real,
separate piece of engineering (a second solver integration, a new
per-cycle model-build cost to measure) and is explicitly NOT part of this
sequential proposal. Worth revisiting only if the heuristic-repair version
below is validated and a further quality gain is still wanted.

## 5. What "sequential" means here, and what comes after it

"Sequential" (per instruction): one destroy-repair cycle at a time,
single working solution, no parallelism — the simplest real ALNS loop,
not the full framework. A natural later extension (not scoped here, not
implied as committed) would be running several independent ALNS chains
in parallel from different starting points, analogous to this project's
existing island-migration mechanism for the evolutionary loop
(`GAScheduler.optimize_islands`) — flagged as a possible future direction
only, since the sequential version has not yet been validated at all.

## 6. Validation methodology, before any of this is trusted

**The real comparison, per instruction**: construction + one-shot polish
(today's actual best pipeline, MINUS the evolutionary generations loop)
versus construction + ALNS refinement — NOT "GA-with-evolution vs. ALNS,"
which would conflate two different questions Section 7a already answered
separately (evolution contributes 0.035%; refinement contributes the
rest). Isolating this cleanly:

- **Baseline arm**: `run_baselines()` / `baseline_chromosomes()`'s best
  heuristic-seeded individual (the same starting point `optimize()`
  itself seeds into generation 0 today), then today's existing
  `polish(max_evals=400)` — zero new code, reuses exactly what exists.
- **ALNS arm**: the SAME heuristic-seeded starting individual, then the
  new ALNS refinement loop instead, evaluation-budget-matched to 400.
- Neither arm runs the GA's generational loop at all — both start from
  identical construction, so any difference measured is attributable
  ONLY to the refinement stage, the one variable this proposal changes.

**Same 3-frozen-snapshot standard as everything else this session**:
window 1/seed 42, window 1/seed 123, window 2/seed 42, snapshot
`"20260825"` (`database/ga_snapshot.py`). "Seed" here controls the ALNS
search's own random draws (destroy-operator tie-breaks, repair
randomness, SA acceptance draws) — analogous to how `polish(...,
random_seed=seed)` already works today — not which heuristic gets
seeded, which is deterministic.

**Decision rule, proposed explicitly rather than judged after the fact**,
same discipline as TCOX/CP-SAT: ALNS counts as worth adopting if it beats
the one-shot-polish baseline's final fitness on a clear MAJORITY of the 3
cases (at minimum 2 of 3), with no case showing a real regression beyond
noise — mirroring the bar memetic integration and TCOX were actually
held to, not a lower one. A result matching their own pattern (real win
on some cases, real loss or wash on others) should be reported with the
same honesty Section 9 of `GA_AGENT_POSTMORTEM.md` established as
standard practice — a mixed result is a legitimate, valuable outcome
here too, not a failure of the investigation.

**Also worth measuring, not just final fitness**: operator weight
trajectories over the run (did the adaptive mechanism actually
differentiate the three destroy operators, or did they end up converging
to roughly equal weight — itself an informative result either way) and
evaluations-to-first-improvement compared to today's polish, since the
brief's own motivating question ("does adaptive weighting actually
identify which operators help over time") deserves a direct answer, not
just an indirect one inferred from the final fitness number.

## 7. Explicitly out of scope for this document

No code, no implementation. This document exists to decide whether and
how to build the sequential ALNS refinement stage, not to build it — same
sequencing as WLC, TCOX, and CP-SAT (scope → review → build the smallest
justified next piece → report real numbers → decide the next step from
there, not before). CP-SAT-as-repair (Section 4) and parallel/multi-chain
ALNS (Section 5) are explicitly named as future ideas, not committed
work.

## 8. Implementation — IMPLEMENTED, LEGALITY VERIFIED (2026-08-26)

Built exactly as designed: `ga/alns.py` (destroy operators, repair via
`local_search.py::_move_reassign_line`'s own least-loaded-line argmin
rule — reused verbatim rather than `choose_line_work_aware`/
`choose_line_least_loaded`, both of which draw from the global `random`
module internally rather than an injectable rng, which would have left
part of this module's randomness outside the caller's seed control;
dual-solution SA acceptance; segment-based adaptive weighting) and
`GAScheduler.alns_refine()` (thin wrapper mirroring `polish()` exactly).
Not wired into `optimize()` — opt-in only, same as `polish()` and
`memetic_interval`.

`tests/test_alns_refinement.py`, 11 tests, all passing; full suite
479 → 490/490: legality across many trials and all three removal
fractions, the never-worse-than-start guarantee held directly, seed
reproducibility, evaluation-budget respected, weight-history structure,
single-operation edge case, and the `GAScheduler.alns_refine()` wrapper
itself.

## 9. Validation — RESULT: fails the pre-committed decision rule, and
    why, mechanistically (2026-08-26)

`scratch/alns_validation.py`: baseline (heuristic-seeded construction +
today's existing `polish(max_evals=400)`, zero new code) vs. ALNS
refinement at three removal fractions (2%, 3.5%, 5%), same starting
individual, same 3 frozen-snapshot cases, snapshot `"20260825"`. Neither
arm runs the GA's evolutionary generations — per instruction, this
isolates the refinement stage cleanly, but it also means these fitness
values are NOT directly comparable to any other full-pipeline number
elsewhere in this project (every other reported run in this investigation
includes 50 generations of evolution before refinement; this validation
deliberately skips that, so its baseline numbers — 854.72, 855.85,
1006.25 — are a different, rougher starting point than e.g. Section 11's
800.27).

| case | baseline final | ALNS final (all 3 fracs identical) | delta |
|---|---|---|---|
| window 1, seed 42 | 854.72 | 856.26 | **-0.181% (regression)** |
| window 1, seed 123 | 855.85 | 856.26 | **-0.049% (regression)** |
| window 2, seed 42 | 1006.25 | **917.29** | **+8.841% (large win)** |

**Against the pre-committed decision rule (≥2 of 3 cases beating
baseline, no real regression on the third): FAILS.** 1 win of 3, with
real regressions (not noise) on the other two, at every removal fraction
tested. Per the same standard memetic integration and TCOX were held to,
this does not clear the bar for adoption as tested.

**A real, honest mechanistic red flag, not glossed over**: the final
result is IDENTICAL to two decimal places across all three removal
fractions, in every case. A parameter that was supposed to meaningfully
change search behaviour (Section 6, the specific reason this run tested
three values instead of assuming one) produced no detectable difference
at all. The `accepted_new_best`/`accepted_improved`/`accepted_worse`
counts explain why: on window 1 (both seeds), ALNS found ZERO improving
moves across the full 400-evaluation budget, on ALL three removal
fractions (`new_best=0` in all 6 window-1 runs) — worse than the existing
one-shot polish, which found small real gains on the same starting point
in under 8 seconds. On window 2, exactly ONE improving move was found,
very early, identically across all three fractions, after which every
run goes almost completely quiet (350-399 of 400 candidates rejected).
Operator weights tell the same story: `high_delay_risk` and
`bottleneck_pool` decay to ~0.0008 (their weight's asymptotic floor under
20-eval segments and a 0.3 reaction factor, `0.7^20`) within the first
few segments in EVERY run, `random` ends up dominating what little
selection remains — the adaptive mechanism never demonstrated a
persistent edge for either targeted destroy operator over the baseline
random one.

**The most likely cause, stated as a hypothesis this document does not
claim to have confirmed further**: the temperature schedule
(`t0 = 0.02 * start_fitness`) was calibrated without measuring the actual
scale of a multi-operation destroy-repair delta first. A single-gene move
(what `polish()` and the Step-1 offspring-harm diagnostic both measured,
`GA_AGENT_POSTMORTEM.md` Section 7h: mean damage 178-565 points) is a
very different scale of perturbation from reassigning 2-5% of a
~2,500-operation window at once (51-127 operations, from ONE stale
`pool_loads` snapshot per Section 3b's own documented, accepted
staleness tradeoff — accepted for `custom_mutate`'s ~6 mutations per
individual, not obviously valid at 10-20x that batch size). If a
realistic multi-operation move's typical delta is far larger than
`t0 ≈ 17` (2% of a ~856-1009 starting fitness), the SA acceptance
mechanism's `exp(-delta/temperature)` collapses to effectively zero for
nearly every non-improving candidate almost immediately — meaning the
"permissive exploration" Section 3d's whole design was built around may
never have actually been exercised in this run. This would explain both
the near-total flatlining AND the removal-fraction insensitivity (if
almost nothing is ever accepted regardless of batch size, batch size
stops mattering) without requiring the destroy/repair design itself to
be unsound.

**Honest bottom line, precise rather than overstated in either
direction**: AS VALIDATED — this specific temperature calibration, this
specific staleness tradeoff at this batch scale — sequential ALNS does
NOT meet the pre-committed bar and should not be adopted on this
evidence. That is a real, decisive result for what was actually tested.
It should NOT be read as a settled verdict on the destroy/repair design
or on ALNS as an idea for this problem — the calibration hypothesis above
was not tested here (per the standing discipline this project has
applied to every other closed investigation: report what was tested,
name what wasn't). The window 2 result (+8.84%, the largest single
effect size measured anywhere in this entire investigation, memetic and
TCOX included) is real and striking enough that a recalibrated attempt
is not obviously a waste — but that is a judgement call for the next
decision, not resolved by this document.

## 10. Recalibration attempt — RESULT: same outcome under a properly
    measured temperature, the real, final verdict (2026-08-26)

Per instruction, one tightly-scoped attempt: measure the real delta
scale directly, derive `t0` from that measurement, re-run the same
validation once.

**Measurement** (`scratch/alns_delta_calibration.py`, 30 real destroy-
repair-evaluate cycles per removal fraction per window, no acceptance
logic, sampled fresh from the heuristic-seeded starting point): the old
formula (`t0 = 0.02 * start_fitness ≈ 17-20`) was roughly 10-20x too
small. Real median WORSENING deltas measured 181-216 (window 1, all
three fractions) and 34-343 (window 2, scaling with removal fraction).
A genuinely useful second finding from this same measurement, worth
keeping on its own terms: on window 2, `high_delay_risk` removal
improved the schedule in 10 of 10 real samples, at every removal
fraction — a clean, reliable signal, and very likely the actual
mechanism behind the +8.84% result. On window 1, zero of 90 real samples
improved, across all three destroy operators and all three fractions —
a real property of that landscape under this repair mechanism, observed
directly, not inferred.

**Fix implemented**: `ga/alns.py::alns_refine` now runs a real
calibration phase first (~5% of the evaluation budget, strict-
improvement-only, safe by construction), measures the ACTUAL delta
distribution for the specific window/fraction being solved, and sets
`t0 = median(worsening_deltas) / ln(2)` (50% acceptance probability for
a typical worsening move at the start of the search) — replacing the
external, unmeasured single-gene-scale guess entirely, and adapting
automatically per instance rather than needing a hand-tuned constant per
window/fraction. 13 tests now (2 new, covering the calibration output
directly), full suite 490 → 492/492.

**Re-ran the identical validation** (`scratch/alns_validation.py`,
unchanged) under this corrected calibration:

| case | baseline | ALNS, recalibrated (all 3 fracs) | delta | search activity (frac=0.035) |
|---|---|---|---|---|
| window 1, seed 42 | 854.72 | 856.26 (unchanged) | -0.181% | improved=76, worse_accepted=75, rejected=248 |
| window 1, seed 123 | 855.85 | 856.26 (unchanged) | -0.049% | improved=54, worse_accepted=56, rejected=289 |
| window 2, seed 42 | 1006.25 | 917.29 (unchanged) | +8.841% | improved=43, worse_accepted=46, rejected=309 |

**Identical final fitness to the broken-calibration run, in every single
case — but this time reached through genuine, extensive exploration**,
not a search that gave up almost immediately. Before recalibration,
window 1 saw 0-19 non-rejected moves out of 400 per run; now it sees
150-180+. The search is demonstrably no longer inert — it explored
hundreds of real accepted moves per run, including many that moved
`current` to a worse point than where it started, exactly the escaping-
local-optima behaviour Section 3d's design was built around — and STILL
never found a single evaluation, anywhere in window 1, better than the
starting heuristic (`new_best=0` across all 6 window-1 runs, calibration
and main phase combined). Window 2 still finds exactly one improving
region and nothing further beyond it, regardless of how much additional
exploration the properly-calibrated search does.

**This changes the interpretation, not just confirms the number.** With
a demonstrably broken temperature, "no improvement found" was
ambiguous — it could mean the search barely tried. With a demonstrably
working, extensively-exploring temperature producing the SAME answer,
that ambiguity is gone. The more likely explanation now points at the
REPAIR mechanism, not the acceptance rule: `_repair_reassign` always
places a torn-out operation on the single, deterministic least-loaded
eligible line (Section 3b, reused verbatim from `local_search.py`'s own
rule). Whatever operations get destroyed, however many, by whichever
operator, repair collapses back onto a small, deterministic set of
"best" lines — the diversity the three destroy operators create is
substantially undone by a repair step with no randomness and no
alternative choices of its own. That would explain both the extensive
exploration finding nothing new (many different destroys, similar
repaired outcomes) and the earlier removal-fraction insensitivity
(bigger batches still collapse onto the same deterministic targets).

**Against the pre-committed decision rule: still FAILS**, and per
instruction this is now the real, final verdict, not provisional.

**Recommend / hold / discard — same category as memetic integration and
TCOX (`GA_AGENT_POSTMORTEM.md` Sections 7f, 7j): HOLD.** Not adopted, not
recommended as a default. Worth stating precisely how this differs from
memetic/TCOX's own record, not just filed under the same label by
default: those two showed genuinely inconsistent, landscape-flipping
results across validation attempts (a result that reversed sign between
runs). This one did not reverse — recalibration, the one attempt
specifically designed to distinguish "broken search" from "genuine
limit," reproduced the identical outcome under demonstrably real
exploration. That makes this a cleaner, more confident negative for the
SPECIFIC design tested (these three destroy operators, this deterministic
repair rule) than a call for more tuning. The two things worth carrying
forward, not because they are proven fixes but because this investigation
found them, not because they were assumed: `high_delay_risk` removal's
100%-real-sample improvement rate on window 2 (SEE SECTION 11 — this
specific claim did NOT survive a reproduction attempt and is corrected,
not just repeated, below) was reported here as a genuinely strong signal;
window 1's flat 0-of-90-real-samples result suggests that window's
landscape may simply not have further gains reachable through
line/machine-count reassignment alone from this starting point,
independent of which search finds them. `ga/alns.py` stays in the
codebase: implemented, tested, opt-in, not adopted — same treatment
memetic integration and TCOX both received.

## 11. Correction — the window 2 "100%-real-sample signal" did NOT
    reproduce; tempering Section 10's claim (2026-08-26)

Per instruction, tracing the 10 improving `high_delay_risk` moves from
Section 9/10 operation-by-operation (`scratch/alns_trace_window2_moves.py`)
to determine whether window 2's win reflects a repeatable structural
pattern or a lucky one-off. **The attempt could not reproduce the
original 10 moves at all** — a fresh run, same window, same frozen
snapshot, same code, same `Random(0)` seeding and call sequence as
`scratch/alns_delta_calibration.py`, found the OPPOSITE: 0 of 30
`high_delay_risk` samples improved, every one substantially WORSE
(deltas +32 to +428, versus the original's mix of ~20 worsening / 10
improving). This is reported directly, not smoothed into the earlier
claim — Section 10's "100%-real-sample improvement rate... a genuinely
strong, specific signal" does not hold up and is retracted as stated.

**Two plausible causes were checked directly, not assumed, and both
ruled out**: (1) hidden non-determinism inside `evaluate_schedule`
itself — re-evaluating the IDENTICAL candidate chromosome twice in the
same run gives bit-identical fitness every time (confirmed across all 30
samples), so the scoring function is not the source. (2) Cross-window
construction-order sensitivity — rebuilding window 1 first (matching
`alns_delta_calibration.py`'s own process order) before window 2 changed
nothing; the trace script's own output was bit-identical with or without
it.

**The most likely remaining explanation, stated as a hypothesis this
investigation did not chase down further (beyond "cheap, using data
already collected")**: PyTorch CPU inference is not guaranteed
bit-reproducible across separate process invocations (well-known
behaviour with multi-threaded reduction operations), and the GNN's delay
predictions are recomputed fresh via a real forward pass at the start of
every script in this investigation. `_destroy_high_delay_risk` ranks
operations by these continuous predictions and takes a hard top-N cutoff
— even a tiny run-to-run numeric difference near that cutoff could swap
which operations qualify, changing the entire destroyed set. If this
hypothesis is correct, it is a real, structural fragility specific to
THIS destroy operator (the other two — `bottleneck_pool`, keyed off
discrete load sums computed from the chromosome itself, and `random` —
do not depend on GNN output at all, and were not implicated by this
test).

**Direct answer to the question this analysis was scoped to
answer**: given the signal could not be reproduced under a careful,
methodologically matched retry, the honest conclusion leans toward
window 2's win being **closer to a one-off than a confirmed, repeatable
structural pattern** — not because a repeatable pattern was tested and
found absent, but because the specific evidence previously offered FOR a
repeatable pattern did not survive a direct reproduction check. This is
a real limit on what this investigation can currently claim, not a
settled "it was definitely luck" finding either — the underlying
mechanism (whether `high_delay_risk` removal has real, exploitable value
on window-2-like landscapes) remains genuinely open, just less
supported by evidence than Section 10 stated. Any future design work
building on the `high_delay_risk` operator specifically should treat
this as an untested hypothesis, not a confirmed mechanism — reflected in
the tempered framing carried into `ALNS_REPAIR_SCORING_PROPOSAL.md`.

**Further correction (2026-08-26, same day) — the GNN-inference-noise
hypothesis above was tested directly and did NOT hold up**: per
`ALNS_REPAIR_SCORING_PROPOSAL.md` Section 7, the same GNN forward pass
was run as three fully independent process invocations (a direct test of
exactly the claim made two paragraphs up). All three produced
BIT-IDENTICAL `delay` predictions, to six decimal places, across 450 real
comparisons. **The hypothesis is retracted, not merely left unconfirmed.**
This means the ORIGINAL discrepancy this section opened with — why
`alns_trace_window2_moves.py` could not reproduce
`alns_delta_calibration.py`'s original 10 moves — remains genuinely
unexplained. It is not attributable to GNN-inference non-determinism,
since that specific mechanism has since been tested directly and ruled
out. The true cause is an open item, not chased down further here (a new,
separate investigation, beyond this section's original "cheap" scope) —
`GA_AGENT_POSTMORTEM.md` Section 10's methodology note is corrected to
match, not left standing on a hypothesis that has since failed its own
direct test.
