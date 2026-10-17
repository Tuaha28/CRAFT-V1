# Richer Repair-Line Scoring — Scoping Proposal (no code changes)

Same discipline as `WLC_RELEASE_GATE_PROPOSAL.md`, `DOMAIN_AWARE_CROSSOVER_
PROPOSAL.md`, `CP_SAT_PROPOSAL.md`, and `ALNS_REFINEMENT_PROPOSAL.md`:
scope first, no code changes yet.

## 1. Why this, why now

Three independent GA-refinement interventions were evaluated and held
(`GA_AGENT_POSTMORTEM.md` Section 7k): memetic integration, TCOX, and
sequential ALNS. All three targeted the SEARCH mechanism — which
individuals survive, how crossover recombines genes, how destroy/repair
explores. None touched a fixed assumption every one of them inherited
unexamined: when any of these mechanisms needs to place an operation on
a line, the line is chosen by ONE rule, everywhere in this codebase —
whichever eligible line currently carries the least work per machine.
Load, and only load.

ALNS's recalibration result is the direct motivation
(`ALNS_REFINEMENT_PROPOSAL.md` Section 10): a properly-calibrated search,
demonstrably exploring hundreds of real accepted moves per run — not a
handful — still converged on the IDENTICAL final answer as the broken,
near-inert version. The most likely explanation offered there: the
REPAIR step, not the search around it, collapses whatever diversity the
destroy operators create back onto a small, deterministic set of "best"
lines, because it has no randomness and no alternative criteria of its
own. This proposal targets that specific, previously-unexamined
assumption directly, shifting the question from "which search mechanism"
to "what is fixed and deterministic in construction/repair that might be
too greedy."

**Explicitly NOT built on the window-2 `high_delay_risk` finding as
confirmed evidence** — `ALNS_REFINEMENT_PROPOSAL.md` Section 11 found
that specific signal did not survive a reproduction attempt and retracted
it. This proposal is motivated by the repair-mechanism hypothesis alone
(a real, structural, code-level observation, independent of that
retraction), not by an unconfirmed operator-level result.

## 2. The current rule, exactly, everywhere it appears

The SAME selection rule, independently implemented at least three times:

- `ga/local_search.py::_move_reassign_line`: `target = min(eligible,
  key=lambda ln: pool_loads.get((ln, optype), 0.0))` — pure load argmin.
- `ga/alns.py::_repair_reassign`: reuses this exact rule verbatim (a
  deliberate choice documented in its own docstring — reused specifically
  because it is deterministic and rng-free, not because it was evaluated
  against alternatives).
- `ga/heuristics.py::choose_line_least_loaded` and `ga/operators.py::
  choose_line_work_aware`: the construction-time and mutation-time
  equivalents — both also purely load-based (with randomized top-k
  tie-breaking, a difference in HOW ties are broken, not in WHAT is
  scored).

Every one of these functions answers the same question the same way:
"which eligible line currently has the least work per machine." None of
them ask whether the OPERATION being placed is itself risky (GNN-predicted
delay probability, already computed and already used elsewhere in this
project for the risk-buffer cushion — `ga/schedule_utils.py::
apply_risk_buffer`), and none of them ask what placing it there does to
operations that will compete for the same resource LATER, only what the
resource's state looks like right now.

## 3. Proposed design: a richer candidate-line score

Replace the single-factor `pool_loads.get((ln, optype), 0.0)` key with a
combined score, still selected by the same `min(eligible, key=...)`
structure (a minimal, targeted change to the SCORING function, not to
the selection mechanism around it):

```
score(line) = w_load * normalized_load(line)
            + w_risk * (delay_prob * normalized_load(line))
            + w_impact * downstream_impact(line)
```

- **`normalized_load(line)`**: the existing signal (work per machine on
  that `(line, type)` pool), normalized against the pool's own capacity
  so lines of different sizes compare fairly — the current rule already
  effectively does this via `sizes.get(k, 1)` in `compute_pool_loads`.
- **`delay_prob * normalized_load(line)`**: an INTERACTION term, not an
  independent one, deliberately. A high-delay-risk operation placed on an
  already-busy line compounds two problems at once (this project's own
  risk-buffer mechanism already treats risk and duration as multiplying,
  not adding — `duration_mins * (1.0 + p * RISK_BUFFER_AT_CERTAINTY)` —
  this score should be structurally consistent with that, not invent a
  different relationship). The GNN's `delay` prediction is already
  computed for every operation; no new model, no new data.
- **`downstream_impact(line)`**: the least-defined term, deliberately
  flagged as needing empirical shape rather than assumed here. A cheap,
  computable proxy worth testing first: how many operations LATER in the
  current `os_array` sequence (not yet destroyed/repaired, or in the
  broader chromosome for construction-time use) are already assigned to
  or eligible only for this same `(line, type)` pool — a proxy for
  "how contested does this pool get AFTER this placement," without a full
  re-simulation per candidate.

**Weights are explicitly NOT proposed here as fixed values.** The
minimal, honest scope is the STRUCTURE above; the actual `w_load`,
`w_risk`, `w_impact` values are an empirical question, matching how this
project treated `removal_frac` (tested at multiple values, not assumed) —
Section 6 proposes testing a small number of weight combinations, not
hand-picking one.

## 4. What this does NOT change

- **Tie-breaking mechanics**: still a deterministic argmin over the
  combined score (or a small top-k random choice among near-ties, if
  `_repair_reassign`'s current fully-deterministic behaviour turns out to
  itself be part of the collapse — worth testing, not assumed, Section 6).
- **Which destroy operators exist, or the ALNS search/acceptance loop**:
  unchanged, `ga/alns.py`'s adaptive weighting and dual-solution
  acceptance stay exactly as validated in `ALNS_REFINEMENT_PROPOSAL.md`.
- **Machine-count redraw**: unchanged (upper-half-biased within cap).
- **Construction** (`ChromosomeFactory.generate_random_individual`):
  out of scope for a first test — this proposal targets REPAIR
  specifically (the mechanism ALNS's own result implicated), not every
  place the shared rule appears. Extending a validated richer score to
  construction and mutation-repair (`choose_line_least_loaded`,
  `choose_line_work_aware`) is a natural later step, not this one.

## 5. CP-SAT-as-repair, still not built — a related but distinct idea

`ALNS_REFINEMENT_PROPOSAL.md` Section 4 already flagged CP-SAT-as-repair-
on-the-torn-out-subset as a future option. This proposal is a cheaper,
more targeted alternative to try FIRST: a richer heuristic score is a
small, local change to one function, not a second solver integration —
worth testing before reaching for CP-SAT again, especially given
`CP_SAT_PROPOSAL.md`'s own closed investigation found the solver could
not reliably find feasible solutions on this problem's full scale under
the simplification tested.

## 6. Validation methodology, before any of this is trusted

**Step 1 — a direct mechanism check, cheap, before any full run**:
sample real destroy batches (the same methodology
`scratch/alns_delta_calibration.py` and `scratch/alns_trace_window2_moves.py`
already established) and compare which lines the CURRENT rule picks
versus the RICHER score, for the SAME destroyed operations. Does the
richer score actually choose differently, meaningfully often — the
direct test of whether it can address the diversity-collapse hypothesis
at all — before investing in a full ALNS re-validation. If the richer
score picks the same line as the current rule in the overwhelming
majority of cases, that is itself a decisive, cheap negative, reported
plainly rather than proceeding to a bigger test anyway.

**Step 2, only if Step 1 shows real behavioural difference**: swap the
richer score into `ga/alns.py::_repair_reassign`, test a small number of
weight combinations (not an open-ended tuning search — 3-4 combinations,
matching the discipline `removal_frac` was tested under), and re-run the
SAME 3-frozen-snapshot ALNS validation (`scratch/alns_validation.py`,
snapshot `"20260825"`) that already produced a clean, trusted baseline
comparison point (the recalibrated HOLD result). Same pre-committed
decision rule discipline as every other intervention in this
investigation: state the bar for adoption BEFORE running, not after —
proposed here as the same bar ALNS itself was held to (beats baseline on
≥2 of 3 cases, no real regression on the third).

**Report legality first**, same standing instruction as every operator
change in this project: a richer score must never produce an ineligible
line or an out-of-cap machine-count — cheap to verify directly given the
existing `_is_legal` test pattern.

## 7. Step 1 result — RESULT: the interaction term as specified cannot
    differentiate lines; the stability check also refuted the hypothesis
    that motivated it (2026-08-26)

`scratch/alns_repair_scoring_step1.py`, placeholder weights (`w_load=1.0,
w_risk=1.0, w_impact=0.3`, explicitly not tuned defaults per Section 3),
450 real comparable decisions (225 per window, both windows, real
`high_delay_risk` destroy batches at all three removal fractions).

**The richer score picked a DIFFERENT line than the current pure-load
rule in ZERO of 450 comparisons — 100% identical in both windows.** Not a
weight-tuning artifact: the reason is structural, provable directly from
the formula in Section 3. `delay_prob` is a property of the OPERATION
being placed, not of the candidate line — it is IDENTICAL across every
line eligible for that operation. The interaction term
`w_risk * (delay_prob * normalized_load(line))` therefore reduces to
`(w_load + w_risk * delay_prob) * normalized_load(line)` — a single
POSITIVE CONSTANT (for that operation) multiplied uniformly onto every
candidate's load. Multiplying every candidate's score by the same
positive constant can never change which one is smallest — the
interaction term, exactly as specified, is mathematically incapable of
ever differentiating between lines, regardless of weight. Only the
`downstream_impact` term (the only signal that actually varies by
candidate line) could ever change the argmin, and at `w_impact=0.3` it
never won against a real load difference across all 450 real samples
tested.

**This is a real, decisive finding for the SPECIFIC formula proposed in
Section 3, not a call for more weight tuning**: for a risk-load
interaction to actually do what Section 3 intended (make risk matter
differently across candidate lines), it needs to depend on something
that varies BY LINE for the same operation — e.g. risk × the MARGINAL
load this operation would ADD to each specific candidate (not the line's
pre-existing load), or risk interacting with a genuinely per-line signal
(a line's own historical delay rate, if such a thing were computed). The
formula as written in Section 3 needs revision before a further test is
worth running, not just different weights.

**The stability check, per instruction, added because Section 10's
methodology note (`GA_AGENT_POSTMORTEM.md`) flagged GNN-inference
noise near a hard cutoff as a real risk**: the same script was run three
times as fully independent process invocations. All three runs produced
BIT-IDENTICAL output — every `delay_prob` value, to six decimal places,
across all 450 decisions in every run, with zero diff lines between any
pair of the three logs. **This directly tested, and does NOT confirm, the
hypothesis carried over from `ALNS_REFINEMENT_PROPOSAL.md` Section 11**
("PyTorch GNN inference is not guaranteed bit-reproducible across
separate process invocations") — in this direct, repeated test, it was
perfectly reproducible. See Section 8 below for the correction this
requires to that earlier document and to `GA_AGENT_POSTMORTEM.md`
Section 10.

## 8. Correction — the GNN-inference-noise hypothesis was tested
    directly and did not hold up (2026-08-26)

`ALNS_REFINEMENT_PROPOSAL.md` Section 11 offered, as "the most likely
remaining explanation, stated as a hypothesis... not chased down
further," that PyTorch GNN inference's lack of guaranteed cross-process
bit-reproducibility explained why `scratch/alns_trace_window2_moves.py`
could not reproduce `scratch/alns_delta_calibration.py`'s original 10
improving moves. `GA_AGENT_POSTMORTEM.md` Section 10 generalized this
into a standing methodology note. Section 7's stability check directly
tested the same claim — the same GNN forward pass, the same model
checkpoint, three independent process launches — and found it perfectly
reproducible, not fragile. **The hypothesis is retracted, not
reconfirmed by omission.**

This means the ORIGINAL discrepancy between those two earlier scripts
remains genuinely unexplained — not attributable to simple GNN-inference
non-determinism, since that specific mechanism has now been tested
directly and ruled out. Chasing the real cause further is a new,
separate investigation and is NOT undertaken here, consistent with the
"cheap, bounded" scope this whole check was given — flagged as a real,
open item rather than silently dropped. `GA_AGENT_POSTMORTEM.md` Section
10 and `ALNS_REFINEMENT_PROPOSAL.md` Section 11 are both being corrected
to reflect this, not left standing on a hypothesis that has since failed
its own direct test.

## 9. Explicitly out of scope for this document

No code, no implementation, no weight values chosen, and — per Section
7's result — no further weight tuning of the SPECIFIC formula in Section
3 either, since the interaction term needs structural revision before
that would be a meaningful test. The next step, if pursued, is
redesigning the interaction term to depend on a genuinely per-line
signal (Section 7), not adjusting `w_risk`/`w_impact` on the current one.

## 10. CLOSED on this specific formula (2026-08-26)

Per instruction: this proposal is closed on the SPECIFIC scoring formula
in Section 3, not revised and re-tested today. Step 1's result was
decisive and structural, not a weight-tuning shortfall — the interaction
term as written can never differentiate between candidate lines, for a
provable mathematical reason (Section 7), and no amount of retuning
`w_risk`/`w_impact` changes that.

**What carries forward as a well-defined future direction, not pursued
further today**: a revised interaction term that depends on something
that genuinely varies BY CANDIDATE LINE for the same operation — the
concrete example already named in Section 7, risk × the MARGINAL load
this operation would ADD to each specific candidate, rather than risk ×
that line's pre-existing load. This is well-defined enough to implement
and test directly whenever this thread is picked back up; it is not
being scoped further or attempted today.

**The reopened window-2 move-trace mystery (Section 8) stays flagged as
a genuine open item, not investigated further right now** — deliberately,
not by default. Same reasoning this project already applied to holding
domain-aware crossover after TCOX (`GA_AGENT_POSTMORTEM.md` Section 7g):
the open question is real and worth remembering, but chasing it now would
be a sixth thread pulling on the same general question (why do isolated
pieces of this pipeline sometimes fail to reproduce across separate
process invocations) without a clear, separate reason to prioritize it
over everything else currently open in this project. Revisit if a future
piece of work gives a specific, concrete reason to need the answer — not
as a standalone curiosity chase.

## 11. Corrected interaction term — Step 1 re-run: REAL differentiation,
    concentrated in one window, stable (2026-08-27)

Per Section 10's carried-forward direction: the interaction term is
revised to depend on the MARGINAL load this specific operation would add
to each candidate line, not the line's pre-existing load.

**Corrected formula:**

```
score(line) = w_load * norm_load(line)
            + w_risk * delay_prob * norm_marginal_load(line)
            + w_impact * norm_impact(line)

marginal_load(line) = op_work(op) / pool_size(line, optype)
norm_marginal_load(line) = marginal_load(line) / max_marginal_load_across_eligible
```

`pool_size(line, optype)` is the same quantity `ga/local_search.py::
compute_pool_loads` already uses to normalize the load term — machine
count for this operation's type on that line, from
`factory.machine_ids_per_line_map`. Unlike the original term, this is NOT
a rescaling of `norm_load(line)`: `norm_load` depends on each line's
already-accumulated work, `norm_marginal_load` depends on each line's
machine COUNT for this operation type. Two lines can rank oppositely on
these two quantities, which is exactly what makes the sum non-degenerate.

**Algebraic proof of differentiation (concrete, non-degenerate case),
same rigor as Section 7's original bug proof:** operation with
`op_work = 32`, two eligible lines, `delay_prob = 0.8`, `w_load = 1.0,
w_risk = 1.0, w_impact = 0` (impact term zeroed to isolate the
interaction term itself):

- Line A: existing load 60 on 2 machines → `norm_load` numerator 30/machine.
  Adding this op: marginal = 32/2 = 16/machine.
- Line B: existing load 280 on 8 machines → `norm_load` numerator 35/machine.
  Adding this op: marginal = 32/8 = 4/machine.

The CURRENT (load-only) rule picks A: 30 < 35.

Normalizing across {A, B}: `norm_load_A = 30/35 = 0.857`, `norm_load_B =
1.0`; `norm_marginal_A = 16/16 = 1.0`, `norm_marginal_B = 4/16 = 0.25`.

```
score_A = 1.0*0.857 + 1.0*0.8*1.0  = 1.657
score_B = 1.0*1.0   + 1.0*0.8*0.25 = 1.200
```

`argmin` is B — the corrected score picks the OPPOSITE line from the
current rule, for a realistic, non-degenerate case, because A's small
machine count means this operation would spike its per-machine load far
more than the same operation would spike B's, even though A's
pre-existing load is lower. This is the general mechanism: whenever
eligible lines differ in machine count for an operation's type (routine
in this project — pools vary in size), load-ranking and marginal-ranking
can disagree, and the interaction term can move the argmin.

**Step 1 empirical result** (`scratch/alns_repair_scoring_step1_corrected.py`,
same snapshot `"20260825"`, same destroy batches, same sampling, same
placeholder weights `w_load=1.0, w_risk=1.0, w_impact=0.3` as the original
Step 1 run, so directly comparable to the original 0/450):

**450 real comparable decisions total. The corrected score picked a
DIFFERENT line than the current pure-load rule in 135 of 450 (30.0%)** —
not near-zero, not trivial. Split sharply by window: **WINDOW 1: 0 of 225
(0.0%) different** — identical to the original formula's result in that
window. **WINDOW 2: 135 of 225 (60.0%) different.** A real example from
the run: operation `987_1937` (`delay_prob=0.285`, 99 eligible lines):
current rule picks line 77 (load=619.191, marginal=9.400); corrected
score picks line 76 (load=632.064, marginal=6.789) — line 76 has slightly
higher existing load but adding this operation would spike it noticeably
less, and the risk weighting on that smaller marginal spike is enough to
flip the argmin.

**Stability check, same standard as Section 7** (three fully independent
process invocations, `scratch_alns_repair_scoring_step1_corrected_run{1,2,3}.log`):
**bit-identical across all three runs** — all 450 decision lines
byte-for-byte identical in every run (`diff` reports zero differing lines
between every pair), matching Section 7's finding and consistent with the
GNN-inference-noise hypothesis staying retracted (Section 8).

**This is a genuine, structural, non-trivial positive result for the
corrected formula, not a weight-tuning artifact** — the algebra above
shows why it can happen, and the empirical run shows it does happen, in
30% of real decisions overall. **The window split is a real, reportable
observation, not chased further here** — per the task's own scope
(“stop here and report”), it is flagged rather than investigated: window
1's pools may be more uniform in machine-count across eligible lines
(making `norm_marginal_load` collapse back toward `norm_load` there),
or this may connect to the still-open window-2 anomaly from Section 8 —
either is speculation until tested directly, not asserted here.

**Per the pre-committed Section 6 bar** ("if the richer score picks the
same line as the current rule in the overwhelming majority of cases,
that is itself a decisive, cheap negative... proceed to a bigger test"
only if Step 1 shows real behavioural difference in a meaningful
fraction of cases): 30% overall, 60% in one window, clears that bar.
**Per this task's explicit instruction, Step 2 (full 3-frozen-snapshot
validation) is NOT run here** — this section stops at reporting the
Step 1 result, leaving the decision to proceed to Step 2 for a separate,
explicit go-ahead.

## 12. Window split explained, and a bigger correction to the "450
    decisions" framing (2026-08-27)

Two candidate mechanisms for Section 11's window1=0% vs window2=60%
split were checked directly, same standard as Section 7's own proof.

**Hypothesis 1 (pool-size uniformity in window 1) — REFUTED.** Mean
coefficient of variation of `pool_size` across eligible lines: window1
0.356, window2 0.386 (nearly identical). Decisions where every eligible
line has an identical `pool_size`: 0/225 in BOTH windows. Window 1's
pools are not more uniform.

**Hypothesis 2 (near-zero delay_prob in window 1) — REFUTED.** Mean
delay_prob: window1 0.228, window2 0.249 (nearly identical, both
comfortably above zero). Fraction with `delay_prob < 0.10`: 0/225 in
BOTH windows.

**The real mechanism (checked directly,
`scratch/alns_repair_scoring_loadgap_diagnostic.py`): the MARGIN by
which the current rule's pick beats the runner-up eligible line on
load.** The interaction term's magnitude is bounded (`delay_prob *
norm_marginal_load`, typically ≤ ~0.25 given the delay_prob range
above) — it can only flip the argmin when that margin is smaller than
the term can close. Measured directly: window 1's load-gap (2nd-best
minus best, normalized by max_load) is **0.0132 for every decision** —
never flips. Window 2's load-gap is **0.0040 exactly** for every
decision that flips, versus 0.0034-0.0108 (mean 0.0108) for decisions
that don't — a real, mechanistic threshold effect, not a window-level
property in itself.

**A more important correction, found while checking the above: "225
decisions" / "450 total" (both this section's and Section 7's original
result) are NOT 225/450 independent samples.** `_destroy_high_delay_risk`
ranks operations purely by `delay_prob` with no rng, and its top-5 is
`removal_frac`-invariant; `start` (the seeded chromosome) is never
mutated across the sampling loop. Every one of the 45 "repeats" per
window (15 draws x 3 fracs) is therefore the IDENTICAL 5 operations,
replayed identically:

```
window1 unique op_keys: 4_2721, 415_3551, 6_1209, 767_2140, 860_2409   (45x each)
window2 unique op_keys: 148_1579, 647_3826, 676_352, 886_783, 987_1937 (45x each)
```

**The true sample size is 10 unique decisions (5 per window), not 450.**
Section 11's "135/450 (30%)" is really **3 of 5 unique window-2
operations flip (0 of 5 in window 1)** — 3/10 real, distinct cases. This
does not change the DIRECTION of either result (original formula: 0/10
unique, still fully inert; corrected formula: 3/10 unique flip, still
non-zero and mechanistically explained) — but it substantially thins the
evidentiary basis for "a meaningful fraction of real cases" versus what
"450 comparisons" implied, and the same redundancy inflates Section 7's
original 0/450 result too (also 0/10 unique underneath, confirmed
directly against `scratch_alns_repair_scoring_step1_run1.log`).

**Before Step 2**, the sampling methodology itself needs fixing to draw
from a genuinely larger and more diverse pool of real high-delay-risk
operations (e.g. across more than one removal_frac's worth of distinct
top-N operations, or across multiple distinct starting chromosomes/
windows) — testing 45 identical repeats of 5 operations was never a
450-sample test, for either formula, and that gap was not visible until
checked here. Not fixed or re-run in this session; flagged as a
precondition for trusting any larger-N claim before Step 2, not a reason
to discard the qualitative finding (a real, provable, non-zero
differentiation mechanism exists, unlike the original formula).
See `GA_AGENT_POSTMORTEM.md` Section 12 for the standalone, generalized
methodology note this warranted.

## 13. Step 1 re-run with the sampling methodology fixed — result holds,
    and sharpens (2026-08-27)

Two fixes applied together in `scratch/
alns_repair_scoring_step1_diverse_sampling.py`, per Section 12's
diagnosis: **(1) three windows**, not two — orders `[0:300)`, `[300:600)`,
`[600:900)` of the 1,029 resampled synthetic orders in snapshot `"20260825"` (verified
count, not assumed), each with its own independently-built scheduler and
heuristic-seeded baseline chromosome — genuinely different load
landscapes, not resamples of the same one. **(2) real random sampling
within each destroy batch**: for every `(window, removal_frac)` pair,
`_destroy_high_delay_risk` is called once to get the real high-delay-risk
batch (unchanged, still the actual operator), then each of 20 repeats
draws a random K=8 subset of that batch via seeded `rng.sample` — every
sampled operation is still a genuine member of that batch's real
high-delay-risk set, but which ones get compared now actually varies.

**Every summary line reports UNIQUE op_key count alongside the raw
comparison count**, directly closing the gap Section 12 flagged.

**Result: 350 truly unique operations across 1,440 raw comparisons**
(116 unique in window 1, 118 in window 2, 116 in window 3 — verified by
extracting and counting distinct `op_key`s from the run log, not
assumed).

**ORIGINAL formula: 0/1440 (0.0%) different — confirmed, robustly, under
genuinely diverse real sampling.** This is the expected result (Section
7's algebraic proof says it MUST be exactly zero, structurally, for any
input), and this re-run confirms the original 0/450 verdict was never an
artifact of the sampling bug — it is a true mathematical impossibility,
independent of how the samples were drawn.

**CORRECTED formula: 413/1440 (28.7%) different overall — consistent
with, and now resting on a real foundation instead of Section 11's
inflated 30% figure.** But the per-window breakdown sharpened
considerably with real diversity, rather than smoothing out:

| window | raw | unique ops | different |
|---|---|---|---|
| 1 (orders 0-300) | 480 | 116 | 2 (0.4%) |
| 2 (orders 300-600) | 480 | 118 | **408 (85.0%)** |
| 3 (orders 600-900) | 480 | 116 | 3 (0.6%) |

**Windows 1 and 3 now agree closely with each other (0.4% and 0.6%,
both near-zero) — window 2 is the outlier, and dramatically more so than
Section 11's small-sample result suggested (85.0% vs. the earlier 60.0%
computed from only 5 unique operations).** With three independent
windows instead of two, this is no longer "window1 vs window2" as an
arbitrary pair — it is two consistent near-zero windows against one
sharply different window, which is a materially stronger signal that
window 2 specifically has a real, distinct structural property (not
sampling noise, not an artifact of which 5 operations happened to be
drawn), though what that property IS remains uninvestigated here.

**Stability, same standard as every prior check in this thread**: 3
independent process invocations, bit-identical output every time — now
meaningfully, since the underlying sample is 350 real distinct operations
rather than 10.

**This is flagged as a real, load-bearing observation worth naming
explicitly: `ALNS_REFINEMENT_PROPOSAL.md` Section 11 already carries an
OPEN, unexplained "window-2 anomaly" from a completely different test
(the move-trace reproduction mystery, Section 8 of this document) — this
is a second, independent test where window 2 behaves qualitatively
differently from its neighbors.** Whether these are the same underlying
cause is speculation, not established here — flagged as a specific,
concrete reason this open item may deserve prioritizing before/alongside
Step 2, per the standing instruction (`GA_AGENT_POSTMORTEM.md` Section
7g reasoning) that open items get chased once there is a specific reason,
not as a standing curiosity. Not chased further in this session.

**Per the pre-committed Section 6 bar, on a real 350-unique-operation
sample: the corrected formula clears the bar for Step 2** (28.7% overall
is not near-zero or trivial) — but the window-2 concentration is worth
resolving, or at minimum stating explicitly as a known characteristic
going into Step 2, since a formula that only differentiates in
~1-in-3-windows-tested is a different, more specific claim than "picks
different lines broadly." Step 2 itself is still not run in this
session — reported here for a decision, same as Section 11.

## 14. Cheap composition check on window 2, then Step 2 — CLOSED,
    real regression where it matters (2026-08-27)

**Composition check first** (`scratch/
alns_repair_scoring_window2_composition_check.py`), per instruction —
this is the SECOND independent test to single out window 2, so worth
five real minutes before treating it as an unexplained curiosity again.
Three concrete, checkable properties, window 1 / window 2 / window 3:

| property | window 1 | window 2 | window 3 |
|---|---|---|---|
| top category (garment type) | Pant 41.7%, U Neck 20.7% | Pant 46.0%, U Neck 17.3% | Pant 38.0%, U Neck 21.0% |
| slack (deadline-release) days, mean/median | 26.8 / 21.0 | 28.1 / 23.0 | 27.0 / 21.0 |
| delay_prob over ALL real ops: mean/median | 0.1047 / 0.0985 | 0.1037 / 0.0985 | 0.1118 / 0.1076 |
| delay_prob > 0.3, any window | 0.0% | 0.0% | 0.0% |

**All three come back statistically indistinguishable between windows.**
Garment-type mix is dominated by the same two categories (Pant, U Neck)
in the same rough proportions everywhere; deadline pressure is nearly
identical (window 2 if anything has slightly MORE slack, the opposite of
what would make it a harder case); aggregate delay-risk level is
nearly identical and, notably, no operation in ANY window exceeds even
0.3 delay_prob — the whole real range this project's GNN outputs sits
well under the values often used as illustrative "high risk" examples
elsewhere in this document. **Window 2's anomaly does NOT trace to
order composition, date range/deadline pressure, or aggregate risk
level** — ruled out directly, not left unexamined a second time. The
mechanism remains genuinely unexplained at the input-data level; it is
evidently a property of the GA construction/scheduling landscape itself
(machine-pool geometry, line-load distribution as actually realized by
the heuristic-seeded chromosome), not of what orders happen to be in the
window.

**Step 2 — real ALNS validation**
(`scratch/alns_repair_scoring_step2_validation.py`). Richer scoring wired
into `ga/alns.py::_repair_reassign` as a genuine opt-in parameter
(`repair_score_weights`, `None` default = unchanged plain-load behaviour
— threaded through `_destroy_and_repair`, `alns_refine`, and
`GAScheduler.alns_refine`; 12 new tests in
`tests/test_alns_refinement.py`, including a direct regression test of
Section 12's algebraic proof; full suite 479→501/501). 4 real
frozen-snapshot cases (window1/seed42, window1/seed123, window2/seed42,
window3/seed42 — window 3 added specifically so window 2 gets its own
comparison point, not folded into window 1's pair), `removal_frac=0.035`
fixed, 4 weight combinations (`(1.0,1.0,0.3)` placeholder,
`(1.0,0.5,0.3)` lower risk, `(1.0,2.0,0.3)` higher risk,
`(1.0,1.0,0.0)` interaction-only), each compared against BOTH
`baseline_polish` and the already-adopted plain-load ALNS.

**Windows 1 and 3 — richer scoring is a no-op, byte-identical to plain
ALNS (0.000% delta, every weight combo, every case).** Consistent with
Section 13: the corrected formula essentially never changes a repair
decision in these windows (0.4%/0.6% differentiation), so the final
schedule comes out identical regardless of scoring rule.

**Window 2, reported on its own per instruction — a REAL REGRESSION, not
a wash.** This is the one case where richer scoring actually changes many
real decisions (85.0%, Section 13), and here it makes things WORSE than
the already-adopted plain-load ALNS, consistently across all 4 weight
combinations:

```
window2_seed42: baseline_polish=1006.25   alns_plain=917.29 (+8.841% vs baseline)
  w1_placeholder      (1.0,1.0,0.3): final=929.74  vs baseline=+7.603%  vs plain-ALNS=-1.357%
  w2_lower_risk       (1.0,0.5,0.3): final=927.52  vs baseline=+7.825%  vs plain-ALNS=-1.115%
  w3_higher_risk      (1.0,2.0,0.3): final=925.93  vs baseline=+7.982%  vs plain-ALNS=-0.942%
  w4_interaction_only (1.0,1.0,0.0): final=929.74  vs baseline=+7.603%  vs plain-ALNS=-1.357%
```

Every weight combination still beats `baseline_polish` (+7.6% to +8.0%)
— richer scoring is not harmful in an absolute sense — but every one
beats it by LESS than plain-load ALNS already does (+8.841%), a real,
consistent regression of 0.94-1.36 percentage points relative to
TODAY'S adopted behaviour, not noise (four independent weight
combinations, same direction, same rough magnitude). `w1_placeholder`
and `w4_interaction_only` (impact weight 0.3 vs 0.0) land on the
IDENTICAL final fitness — the same pattern Section 7 found for the
original formula: the `downstream_impact` term is not what's driving
outcomes here either. Turning risk weight UP (`w3_higher_risk`) gives
the mildest regression of the four, but still a real loss, not a
recovery.

**Verdict: CLOSED, HOLD — richer repair-line scoring does not beat
plain-load ALNS in any tested case, and regresses where it actually
does anything.** This is the decisive, honest result the pre-committed
bar was designed to produce: Step 1 correctly identified that the
corrected formula CAN differentiate (a genuine, provable, non-zero
mechanism, unlike the original dead formula) — but differentiating is
not the same as improving, and Step 2 shows directly that where it
differentiates, it makes the schedule WORSE, not better. The most likely
explanation, consistent with the mechanism itself: `_repair_reassign`'s
existing plain-load rule is deliberately, exactly the rule that
minimizes the busiest-pool-per-machine bound the schedule's own
makespan is constrained by (`ga/local_search.py`'s own docstring) —
diverting a placement away from the genuinely least-loaded line for a
risk-driven reason trades a small, speculative delay-avoidance benefit
against a direct, measured cost to the metric the whole schedule is
optimized on. Code stays in the repo, tested, fully opt-in
(`repair_score_weights=None` unchanged default) — zero effect on
`GAScheduler`'s actual behavior, same treatment as memetic integration
and TCOX's own final HOLD verdicts. Not recommended for further weight
tuning or a different `downstream_impact` proxy without a specific new
reason to expect the direction to flip — four weight combinations
spanning a 4x range on the risk term all landed in the same place.
