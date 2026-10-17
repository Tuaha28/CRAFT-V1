# Domain-Aware Crossover — Scoping Proposal (no code changes)

Same discipline as `WLC_RELEASE_GATE_PROPOSAL.md`: scope first, no code
changes yet. Every claim below is tied to either a line in the actual
`ga/chromosome.py`/`ga/operators.py` source or a real measurement from
`GA_AGENT_POSTMORTEM.md` Sections 7a/7h/7i — nothing here is a generic
literature description of crossover operators in general.

## 1. Why this, why now

`GA_AGENT_POSTMORTEM.md` Section 7a found that 80-90% of crossover/
mutation offspring are worse than their parent, harmful moves several
times more damaging than beneficial ones gain (Section 7h re-validated
this across 3 independent frozen-snapshot cases: 81.41%, 83.17%, 89.83%
worse — never once below 81%, the most consistent finding in this whole
investigation). Section 7g held a proposal for this operator pending two
things: (1) confirming the 81% figure wasn't a single-instance artifact
(done, 7h), and (2) confirming which operator — crossover or mutation —
the harm actually concentrates in, since the original diagnostic
couldn't distinguish them. Section 7i answered that directly:
**crossover-only offspring are worse 83.60% / 85.37% / 90.51% of the
time across the 3 cases; mutation-only offspring are worse only 58.74% /
55.92% / 80.00% of the time — crossover is consistently 10-30 points
more damaging than mutation, in every case, without exception.** This
proposal is written on that confirmed basis, not on the code-reading
hypothesis alone.

## 2. The chromosome, exactly

`ga/chromosome.py::ChromosomeFactory.generate_random_individual` builds
one flat list:

```
chromosome = os_array + la_array + mc_array
```

three same-length segments concatenated, POSITION-ALIGNED across
segments: `os_array[i]` is an operation key, `la_array[i]` is the line
assigned to it, `mc_array[i]` is its machine count. The three segments
are not stored as a list of `(op, line, mc)` triples — they are three
independently-indexed arrays that happen to line up by list position.
`os_array` is a PERMUTATION (every real operation appears exactly once);
`la_array`/`mc_array` are not (values legitimately repeat).

## 3. The current crossover operator, exactly, and the mechanism

`ga/operators.py::custom_crossover` (lines 184-238):

1. **Lines 210-215**: draws `pt1, pt2 = sorted(random.sample(range(num_ops), 2))`
   and 2-point-swaps `la1[pt1:pt2]` with `la2[pt1:pt2]`, and
   `mc1[pt1:pt2]` with `mc2[pt1:pt2]`, at THOSE points.
2. **Line 219**: calls `order_crossover(os1, os2)` for the OS segment —
   which draws its OWN, entirely independent cut points internally
   (`order_crossover`, lines 51-52: `a, b = sorted(random.sample(range(size), 2))`).
3. **Lines 223-224**: `repair_precedence` fixes step ORDER only (does not
   touch which operation is at which position).
4. **Lines 230-232**: `repair_line_and_machine_count` re-validates: for
   every position `i`, if `la_array[i]` isn't eligible for whatever
   operation now sits at `os_array[i]`, reassign to a valid line
   (`choose_line_work_aware` — load-informed, not random) and clamp
   `mc_array[i]` to that line's real capacity.

**The mechanism**: steps 1 and 2 use TWO DIFFERENT, UNRELATED random cut
points (`pt1,pt2` vs `a,b`) on the SAME chromosome. After step 2, the
operation sitting at position `i` in a child's `os_array` is generally
NOT the operation that `la_array[i]`/`mc_array[i]` were originally chosen
for in either parent — position `i`'s (line, machine-count) pair was
inherited from whichever parent's slice/fill logic (steps 1 and 2,
independently) happened to leave a value at index `i`, not from the
parent that produced a good pairing FOR the operation now occupying that
position. Step 4 is a **validity-only repair**: it fixes ELIGIBILITY
(is this line legal for this operation) and CAPS (is machine-count within
range) — it does not ask whether the resulting pairing is a GOOD one,
because it has no way to know what "good" was in either parent once the
original (operation, line, machine-count) association has already been
severed by steps 1-2. A good line/machine-count choice, wherever it
survives, survives by coincidence of which position it landed on, not
because it stayed attached to the operation it was chosen for.

This is consistent with, and now confirmed by, Section 7i: crossover
offspring are worse 83.60-90.51% of the time, meaningfully more often
than mutation offspring (55.92-80.00%) — and `custom_mutate`'s LA/MC
mutation is already domain-aware (`choose_line_work_aware`, load-biased
MC), while crossover's LA/MC recombination is a domain-blind positional
swap. The one operator in this chromosome that is NOT already
domain-aware in some form is exactly the one measured as most harmful.

## 4. What this proposal does NOT target — explicit scope boundary

- **OS mutation** (`custom_mutate`, lines 292-295: plain `random.randint`
  swap of two positions) is also domain-blind, and mutation-only harm is
  still real (55.92-80.00% worse, not a clean result) — but Section 7i
  measured mutation as consistently less damaging than crossover, and the
  instruction that scoped this proposal was specifically about crossover.
  A domain-aware OS mutation operator is a plausible FUTURE idea, not
  addressed here.
- **LA/MC mutation** is already domain-aware (`choose_line_work_aware`,
  load-biased) — not a target.
- **Mutation's surprising marginal-repair effect** (7i: net-improving in
  2 of 3 cases when applied on top of crossover) is noted as a real,
  interesting finding but is NOT relied on by this proposal — it wasn't
  independently confirmed beyond one diagnostic run, and this proposal
  should stand or fall on the crossover mechanism itself, not on hoping
  mutation cleans up after it.

## 5. Proposed design — Triple-Coupled Order Crossover (TCOX)

**Core idea**: stop treating OS/LA/MC as three independently-recombined
arrays reconciled by position after the fact. Recombine them as coupled
`(operation, line, machine_count)` triples, keyed by OPERATION IDENTITY,
so a triple that was good in a parent stays together through crossover
regardless of which position it ends up at in the child.

**Mechanism** (description, not code): treat each parent as a mapping
`triple_for[operation] = (line, machine_count)` alongside its existing
`os_array` (the order the triples get sequenced in). Apply Order
Crossover to the OS PERMUTATION exactly as today (same slice-and-fill
logic, same guarantee that every operation appears exactly once) — but
whenever an operation is placed into a child, whether from the copied
slice or the fill pass, its `(line, machine_count)` values travel WITH
it, sourced from whichever parent that specific operation instance came
from in the OX fill logic (slice region → from the slice-source parent;
fill region → from the fill-source parent, exactly mirroring which
parent `order_crossover`'s `build_child` already draws each operation
from). This requires no new cut-point logic beyond what `order_crossover`
already computes — it changes what travels ALONGSIDE each operation
during the existing slice/fill construction, from nothing to its
(line, machine_count) pair.

`repair_line_and_machine_count` is still run afterward — it is still
needed for genuine edge cases (e.g., a line eligible in one parent's
context is not eligible if a protected-machine-type fraction differs, or
after two operations end up needing the same over-subscribed line) — but
it should fire far less often, since most pairings are no longer
arbitrary to begin with.

**What this does NOT change**: LA/MC's own 2-point crossover (step 1
above) stays as-is for now — it was not the confirmed target (crossover's
harm was measured in aggregate; TCOX addresses the specific decoupling
mechanism identified, not a redesign of every crossover sub-step). If
TCOX's validation isolates further attributable harm to the LA/MC 2-point
swap specifically, that would be a follow-up, not bundled in here.

## 6. Cheaper alternative — shared cut points (comparison arm, not a replacement)

A smaller change: make `custom_crossover` draw ONE set of cut points and
pass them into both `order_crossover` and the LA/MC swap (instead of
`order_crossover` drawing its own `a,b` independently of `pt1,pt2`).
This aligns the SLICE region only — within `[a,b)`, if both segments copy
from the same parent at the same positions, alignment holds by
construction. It does NOT fix the FILL region (positions outside the
slice), which is the majority of the chromosome and where OX's fill
logic still reorders operations independently of where LA/MC values end
up. This is worth implementing and measuring alongside TCOX, not instead
of it: it isolates how much of TCOX's benefit (if any) comes from fixing
the whole chromosome versus just the slice, at a fraction of the
implementation cost. If it captures most of the benefit, TCOX's added
complexity may not be justified; if it captures little, that is itself
evidence the fill-region decoupling is where the real damage is.

## 7. What would need testing before this is trusted

1. **Legality, unconditionally**: every child must still be a valid
   permutation (no duplicate/missing operations), precedence-valid after
   repair, and every `(line, machine_count)` pair must be eligible and
   within cap after `repair_line_and_machine_count` runs. New unit tests
   mirroring `tests/test_ga_orchestration.py`'s existing legality checks
   for `custom_crossover`.
2. **Re-run the offspring-harm diagnostic with the new operator**,
   swapped in for `custom_crossover` (not full GA fitness — a more
   direct, less noisy signal): does crossover-only worse% actually drop
   from the confirmed 83.60-90.51% baseline? This is the direct test of
   whether TCOX addresses the mechanism it claims to, before spending a
   full GA run to find out indirectly.
3. **Same 3-frozen-snapshot validation standard as everything else in
   this investigation** (window 1/seed 42, window 1/seed 123, window
   2/seed 42, snapshot `"20260825"`, `database/ga_snapshot.py`) — full GA
   runs, baseline (current crossover) vs. TCOX vs. the shared-cut-points
   alternative, comparing final fitness, on-time rate, and makespan, not
   just the offspring-harm rate in isolation. Given this project's own
   track record (memetic integration's sign-flipping benefit, Section
   7f), a single favorable case would NOT be reported as a win without
   this same 3-case check.
4. **Computational cost**: TCOX operates on operation-keyed structures
   (dict lookups) rather than flat array slicing — must be measured, not
   assumed free, since crossover runs `cxpb x population` times per
   generation.
5. **Elitism/reproducibility guarantees**, same standard as memetic
   integration's own tests: legal chromosomes, reproducible for a fixed
   seed, never regresses the population's best generation-to-generation
   under elitism.

## 8. Explicitly out of scope for this document

No code changes. No implementation. This document exists to decide
WHETHER and HOW to build TCOX, not to build it — matching the sequencing
already used for the WLC release gate (scope → stress-test → approve →
implement) and for memetic integration (diagnose → implement → validate
→ re-validate under a frozen snapshot). The next step, if this scope is
approved, is implementation plus the Section 7 checks above — not
starting here.

## 9. Implementation — IMPLEMENTED, LEGALITY VERIFIED (2026-08-26)

Both operators implemented in `ga/operators.py`: `triple_coupled_crossover`
(TCOX, Section 5) and `shared_cutpoint_crossover` (the comparison arm,
Section 6), plus the small backward-compatible addition `order_crossover`
needed to support the latter (an optional `cut_points` parameter,
default `None` = unchanged existing behaviour). Neither new operator is
wired into `GAScheduler` by default — opt-in only, via
`toolbox.register("mate", ...)`, same as `memetic_interval`.

Per instruction: legality tests first, before anything else.
`tests/test_domain_aware_crossover.py` (11 tests, all passing; full suite
468 → 479/479):

- Both operators produce only legal offspring across 50 randomized trials
  each (no duplicate/missing operations, precedence respected, every line
  eligible, every machine-count within cap) — same `_is_legal` check
  `test_ga_orchestration.py` already applies to `custom_crossover`.
- Both mutate individuals in place and return the same objects, matching
  `custom_crossover`'s contract (required for `toolbox.register("mate", ...)`
  swap-in to work).
- Both handle the `num_ops <= 1` edge case without crashing.
- Both are reproducible for a fixed seed.
- `order_crossover`'s new `cut_points` parameter is backward compatible
  (unchanged behaviour when omitted) and is correctly honoured when given
  (`shared_cutpoint_crossover`'s LA/MC swap region and OS's slice region
  provably cover the same index range).
- **TCOX's actual coupling guarantee, tested directly**: every operation
  in every child, checked against `triple_coupled_order_crossover`'s raw
  output (50 trials), has a `(line, machine_count)` pairing matching
  exactly one of its two parents' own pairings for that operation —
  never a third, position-derived value.

**A real, honest finding from writing this test, not from the design
review**: that coupling check FAILED the first time it was run, against
the full `triple_coupled_crossover` wrapper's output rather than the
inner function's raw output. Root cause: `repair_precedence` — called
right after TCOX's recombination, unchanged from `custom_crossover` — can
still reorder POSITIONS for operations whose relative sequence a
crossover left invalid, without moving their `(line, machine_count)`
values along with them. This is a real, smaller, PRE-EXISTING mechanism
(identical in today's `custom_crossover`, not something TCOX introduces)
that TCOX does not address — documented directly in
`triple_coupled_crossover`'s docstring, and the test now correctly
isolates TCOX's own guarantee (on `triple_coupled_order_crossover`'s raw
output) from this separate, shared repair step, rather than overclaiming
the guarantee survives the full pipeline untouched. Worth remembering:
TCOX fixes the CONFIRMED dominant mechanism (the two-independent-cut-
points issue, Section 7i), not every possible source of OS/LA/MC
decoupling — this residual one is out of scope, same as Section 4 says.

## 10. Direct mechanism check — TCOX's harm-rate reduction is real but
    landscape-dependent, not uniform (2026-08-26)

Per instruction: before any full GA comparison, re-run the crossover-only
harm-rate diagnostic with each operator swapped in via
`toolbox.register("mate", ...)`, across the same 3 frozen-snapshot cases,
comparing baseline (`custom_crossover`) against both TCOX and the cheaper
`shared_cutpoint_crossover` side by side (`scratch/ga_tcox_mechanism_check.py`,
9 runs: 3 cases × 3 operators).

| case | baseline worse% | TCOX worse% | shared_cutpoint worse% | TCOX's reduction |
|---|---|---|---|---|
| window 1, seed 42 | 83.60% | 83.48% | 84.32% | **0.12pp — noise** |
| window 1, seed 123 | 85.37% | 83.51% | 83.19% | **1.86pp — weak** |
| window 2, seed 42 | 90.51% | **80.74%** | 88.08% | **9.77pp — real** |

**This does not confirm the reduction "holds consistently across all
three," and it also is not simply "doesn't hold" — it is a third,
specific pattern, reported as found**: TCOX produces a substantial,
almost certainly real effect on window 2 (~1300 crossover-only
observations per run; a 9.77-point swing is roughly 9 standard errors
under a binomial approximation, not sampling noise) and a negligible-to-
weak effect on window 1 under both seeds (0.12 and 1.86 points, both
within or barely outside the ~1-point standard error band for this
sample size). **TCOX's benefit is concentrated specifically in the case
that already had the worst baseline harm rate (window 2, 90.51%) — the
same window that showed the least local-search-reachable improvement for
memetic integration (Section 7f) and the lowest crossover improved% in
Section 7h/7i.** A plausible, not yet independently confirmed,
explanation: window 2's harder capacity/demand landscape (Section 7f/7h)
gives the OS/LA/MC decoupling mechanism more room to do damage — more
positions land on genuinely bad, previously-good-elsewhere pairings — so
there is more for TCOX's fix to correct. Window 1's already-tighter
landscape may leave less of that specific damage for TCOX to prevent,
even though the confirmed mechanism (Section 7i) is present there too.

**The cheaper `shared_cutpoint_crossover` arm does NOT capture a
meaningful share of TCOX's benefit anywhere, and on window 1/seed 42 it
is nominally worse than baseline (84.32% vs 83.60%, within noise but
certainly not an improvement)**: its best showing (window 2: 88.08%,
a 2.43-point reduction) is barely a quarter of TCOX's 9.77-point
reduction on the same case. Fixing only the slice-region alignment,
leaving the fill region (the majority of the chromosome) exactly as
domain-blind as `custom_crossover` today, is not a useful partial
substitute for the full triple-coupling — the fill region is evidently
where most of the exploitable damage actually lives, not the slice.

**Caveat on the mutation-only numbers in the same run** (58.74% → 69.57%
on window 1/seed 42's baseline-vs-TCOX pair, for example): mutation
(`custom_mutate`) is byte-for-byte unchanged across all three arms, so
these swings are an expected RNG-stream-divergence artifact — different
crossover operators consume different amounts of randomness per
generation, so which individuals mutation lands on (and what it does to
them) diverges downstream even under "the same seed." Not evidence
mutation's own behaviour changed; the crossover-only column is the valid
comparison this check was designed to make.

**Consequence for the full GA comparison (Section 11, next)**: proceeding
as instructed, but with tempered, specific expectations rather than a
blanket prediction — a real, final-fitness-level win on window 2 would be
consistent with this mechanism check; little-to-no measurable win on
window 1 (either seed) would also be consistent with it, not a
contradiction. All three operators (baseline, TCOX, shared_cutpoint) are
carried into the full comparison across all three cases, so the
shared_cutpoint arm's weak showing here can be confirmed or reconsidered
at the fitness level too, not assumed from this diagnostic alone.

## 11. Full GA comparison — the final result, and the real verdict
    (2026-08-26)

Per instruction: the last step, run only after the mechanism check above.
`scratch/ga_tcox_full_comparison.py`, full `optimize()` + end-of-run
polish, same 3 frozen-snapshot cases, all three operators.

| case | baseline final | TCOX final | TCOX Δ | shared_cutpoint final | shared_cutpoint Δ |
|---|---|---|---|---|---|
| window 1, seed 42 | 800.27 | 802.86 | **-0.324% (WORSE)** | 802.72 | -0.307% (worse) |
| window 1, seed 123 | 792.57 | 792.66 | -0.011% (flat) | 793.66 | -0.138% (worse) |
| window 2, seed 42 | 914.75 | 911.36 | **+0.371% (better)** | 914.75 | +0.000% (no effect) |

**This does not confirm a net win, and it is reported exactly as it
landed.** TCOX helps on window 2, precisely the case Section 10's
mechanism check predicted it would (the 9.77-point crossover-only harm
reduction translates into a real, measurable fitness gain here — the one
case where mechanism and outcome agree cleanly). It is flat on window
1/seed 123, also roughly consistent with that case's weak 1.86-point
mechanism-level reduction not amounting to much at the fitness level.
**But on window 1/seed 42 it is a genuine REGRESSION (-0.324%), despite
Section 10 measuring essentially NO difference in crossover-only harm
rate there (83.60% vs 83.48%, 0.12 points, noise) — the diagnostic
predicted no effect, and what actually happened was a real loss, not a
wash.** Looking at the evolution-only numbers explains where: baseline's
own 50 generations of evolution found real improvement on this run
(808.14 → 805.20), while TCOX's evolution barely moved (808.14 → 807.93)
and shared_cutpoint's found nothing at all (808.14 → 808.14, unchanged
across all 50 generations) — end-of-run polish partially recovers both,
but not enough to catch baseline. **The crossover-only harm-RATE
diagnostic, while a real and correct test of the confirmed mechanism, is
not a complete predictor of full-GA outcome**: an aggregate rate can stay
unchanged while WHICH specific individuals get modified, and how, still
changes — with downstream effects on population diversity and selection
dynamics (the same class of mechanism flagged, not proven, as a
hypothesis in `GA_AGENT_POSTMORTEM.md` Section 7c's memetic-integration
diversity-collapse discussion) that an aggregate offspring-vs-parent
statistic cannot see.

**Final verdict — recommend / hold / discard, same standard as memetic
integration's own final verdict (Section 7f)**: **HOLD.** Not recommended
as a default. TCOX has a real, mechanism-backed win on one of three clean
test cases, a real regression on another, and a wash on the third — a
similar mixed record to memetic integration's, not a clean confirmation
of the design this proposal opened with. `shared_cutpoint_crossover` is
not competitive with either baseline or TCOX anywhere in this comparison
and is not worth pursuing further as a standalone alternative — it exists
in the codebase as the tested ablation that helped answer this question,
not as a candidate for adoption.

**No further code changes needed as a result of this verdict**: neither
operator was ever wired into `GAScheduler`'s default registration —
`__init__` still calls `toolbox.register("mate", custom_crossover, ...)`
unchanged, so this entire investigation had zero effect on the system's
actual default behaviour throughout. Both operators, and their tests,
stay in the codebase: implemented, legality-verified, and available as an
explicit opt-in for a future run that wants to try TCOX specifically on a
window-2-like problem instance, same treatment `memetic_interval` got.
Building further on top of either operator (e.g. addressing the
`repair_precedence` residual gap from Section 9, or investigating why
TCOX regresses window 1/seed 42 specifically) is not recommended without
a clearer, mechanism-confirmed reason to expect it would help broadly —
the same discipline this whole proposal was built on.
