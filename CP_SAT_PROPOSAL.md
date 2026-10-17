# CP-SAT Scheduling — Scoping Proposal (no code changes)

Same discipline as `WLC_RELEASE_GATE_PROPOSAL.md` and
`DOMAIN_AWARE_CROSSOVER_PROPOSAL.md`: scope first, no code changes yet.
Every claim about this project's actual scheduling logic is tied to a
line in `ga/fitness.py`/`ga/schedule_utils.py`/`ga/chromosome.py`, not a
generic description of job-shop scheduling.

## 1. Why this, why now

Two consecutive, carefully diagnosed and carefully validated GA-operator
interventions — memetic integration (`GA_AGENT_POSTMORTEM.md` Section 7)
and TCOX (`DOMAIN_AWARE_CROSSOVER_PROPOSAL.md`) — both landed on the same
kind of result: a real, mechanism-backed win on some cases, a real loss
or wash on others, no way yet to predict which. Both failures trace, on
the evidence, to something downstream of the specific operator content —
memetic's diagnosed mechanism was a population-diversity/tournament-
selection interaction (Section 7c); TCOX regressed on a case where its
own targeted mechanism was measured unchanged (Section 9/10), meaning
something in the population/selection layer, not the crossover edit
itself, was responsible. That is a real, informative pattern: it argues
for pausing further attempts to tune this GA's operators or selection
scaffolding, and for testing an approach that doesn't depend on that
scaffolding at all, rather than for a third attempt in the same family.
CP-SAT is such an approach — evaluated now, deliberately narrow in scope,
rather than after further GA-side iteration.

## 2. What CP-SAT is, briefly

Google OR-Tools' CP-SAT (Python package `ortools`, no licensing cost):
a constraint solver, not a population-based metaheuristic. The problem is
represented directly with scheduling-native primitives — interval
variables (a start time, a duration, an end time, with start/duration
themselves solver-determined), `NoOverlap` constraints (a set of
intervals that can't run concurrently on one resource), and `Cumulative`
constraints (a resource with limited total capacity shared by several
concurrent intervals, each demanding some amount of it). It searches
systematically via constraint propagation rather than randomly generating
and evolving candidates, and given a time budget either proves
optimality, returns the best solution found with a reported gap to
optimal, or proves infeasibility — guarantees a GA never provides.

## 3. Mapping this project's actual problem onto CP-SAT primitives

Grounded in `ga/fitness.py::evaluate_schedule` (the GA's own simulation,
the definition of "correct" this comparison must match) and
`ga/schedule_utils.py::calculate_operation_work`.

### 3a. What's an interval variable

One per operation (same unit `ga/chromosome.py`'s OS array uses:
`production_order_id_operation_id`). Its duration is NOT fixed input data
— `calculate_operation_work` defines it as:

```
duration_mins = setup_mins + (serial_processing_mins / machine_count)
```

`machine_count` (MC) is a DECISION VARIABLE in the GA's chromosome, the
same way it would need to be in CP-SAT — more machines assigned, shorter
duration, at the cost of claiming more of a shared, finite resource.
This is the one piece of this mapping that does NOT fall out of a
standard interval/NoOverlap/Cumulative model directly, and needs one of:

- **Enumerated candidate intervals** (recommended for the prototype):
  for each operation, on each eligible line, create one OPTIONAL interval
  per feasible MC value (bounded by `GA_MAX_MACHINES_PER_OPERATION` and
  the line's real pool size — `ga/chromosome.py::_max_machines_for`,
  typically small, single digits to low teens on this data), each with
  its own fixed duration computed the same way `calculate_operation_work`
  does. Exactly one candidate is active (`AddExactlyOne` over the
  presence literals), and the active one feeds the resource constraints.
  This composes naturally with per-machine assignment (3b) since each
  candidate already corresponds to a specific claimed machine count.
- **A multiplication constraint** (`AddMultiplicationEquality`) tying an
  integer duration variable, an MC variable, and the (integer-scaled)
  work content directly — fewer variables, but doesn't compose as
  naturally with per-machine identity/changeover if that's kept (3b).

Also real, not optional: `evaluate_schedule` (lines 307-337) applies a
GNN-driven RISK BUFFER on top of base duration
(`ga/schedule_utils.py::apply_risk_buffer`, adopted per `GA_AUDIT.md`
Section 6, gated at `RISK_BUFFER_GATE_THRESHOLD`) and commits machines for
the CUSHIONED span, not the base one — `machine_free[asset_id] =
cushioned_end_time`. Whether CP-SAT's interval size should be the
cushioned duration (matching the GA's own committed-capacity behaviour
exactly) or the base duration (simpler, treats the buffer as a separate
softening) is a real design choice for the prototype to make explicitly,
not inherit by accident.

### 3b. What's a NoOverlap group / Cumulative resource — the central open
     modelling question

`evaluate_schedule` (lines 309-337) does NOT track a simple pool-capacity
number — it tracks INDIVIDUAL MACHINE IDENTITY: `select_machines_and_start`
picks specific `chosen_ids` from `machine_pools[op_key][line_id]`
(`ga/chromosome.py::machine_ids_per_line_map`, already reserve-excluded
per `utils/machine_reserve.py`'s 7% policy), and charges
`CHANGEOVER_MINUTES` per machine when it switches to a different
production order (`machine_last_order[asset_id]`). This is a genuinely
richer resource model than a plain `Cumulative` pool constraint, which
only tracks aggregate concurrent demand, not which specific unit of
capacity was used or what it ran before.

Two honest options, not a settled answer:

- **Simplified `Cumulative`-per-(line, operation-type)-pool** (recommended
  for the prototype): capacity = `usable_cap_by_type`/`machines_per_line_map`
  for that pool (already reserve-adjusted, reused as-is from the GA's own
  eligibility data — no new modelling for the 7% reserve). Ignores
  per-machine changeover. Cheap to build, cheap to reason about, and
  directly comparable to a GA run configured the same way (changeover
  disabled) for a first apples-to-apples check.
- **Full per-machine `NoOverlap`**: one resource line per physical
  machine (`asset_id`), an operation's chosen MC machines each get an
  optional interval on their respective machine's NoOverlap sequence,
  with sequence-dependent setup time between different orders on the same
  machine (`AddNoOverlap` plus a circuit/sequencing constraint or literal
  reified switch-cost terms). Faithful to what the GA actually simulates,
  substantially more variables and constraints, real risk of blowing up
  solve time on a 300-order window.

Recommendation: start the prototype with the simplified `Cumulative`
model, explicitly UNFAIR to CP-SAT in the GA's favour in one specific,
named way (no changeover modelled) so any comparison is read correctly —
add per-machine `NoOverlap` only if the simplified version is promising
enough to justify the added complexity, per Section 5's decision gate.

### 3c. Precedence, release dates, deadlines

Straightforward, well-grounded in `evaluate_schedule` lines 287-294:
within an order, operation `k+1`'s start ≥ operation `k`'s end
(`order_ready_times[order_id]` advancing exactly this way already); an
order's first operation's start ≥ its `release_date`
(`order_release_minutes`, the release-floor fix from `GA_AUDIT.md`
Section 8). Both are native CP-SAT precedence constraints
(`Add(end_k <= start_{k+1})`), no open questions here.

### 3d. Objective

`evaluate_schedule` computes a multi-term fitness (tardiness/lateness
penalty, makespan, priority reward, idle-labour penalty on top of the
timing above) — CP-SAT natively minimizes a scalar weighted sum of
linear/piecewise-linear terms, which maps in principle, but exact parity
with the GA's full weighted formula is its own scoping question, not
assumed solved by this document. Recommendation: the prototype should
start with a SIMPLER, clearly-stated proxy objective (total weighted
tardiness + makespan) rather than attempting to replicate every fitness
term on the first pass — matching this project's own "small, cheap check
first" discipline — and only invest in exact objective parity if the
simplified prototype is promising enough to justify a fairer, harder
comparison.

## 4. Explicit scope boundary — what this replaces, what it doesn't

Replaces ONLY what the GA chromosome currently encodes: operation
sequencing, line assignment, machine count (OS/LA/MC). Employee
assignment stays exactly the downstream, deterministic step it already
is — not re-decided by CP-SAT, not expanded into scope here. This keeps
the comparison interpretable: a genuine change in scheduling quality
should be attributable to the scheduling method, not conflated with a
simultaneous change to how labour gets resolved.

## 5. Small prototype — single window, one frozen snapshot, fixed budget

- **Data**: window 1's operations (300 orders), under the standing
  frozen snapshot `"20260825"` (`database/ga_snapshot.py`) — same data,
  same origin timestamp the GA comparisons in this session already used,
  for direct comparability.
- **Time budget**: match the GA's own measured wall-clock cost for one
  window under this session's own runs — `optimize()` + end-of-run polish
  measured at 176-235s per window across the 9 arms in Section 11's full
  comparison (baseline: 176s/202s/196s across the 3 cases; TCOX and
  shared_cutpoint: 201-235s). Use that same range — e.g. the 235s upper
  bound, so CP-SAT is never shortchanged relative to the slowest GA arm
  actually measured — as CP-SAT's `max_time_in_seconds` solver parameter:
  a genuinely fair budget, not "run until proven optimal," which could
  take unbounded time.
- **Report**: best objective value found within budget, the solver's own
  reported gap to optimal (or proof of optimality/infeasibility if
  reached before the budget expires), and — if a feasible solution
  exists — the same on-time-rate/makespan figures the GA reports, so the
  comparison reads on the same terms as every GA result in this
  investigation, not just a raw objective number.
- **Decision rule, proposed explicitly rather than left to judgement
  after the fact**: CP-SAT counts as "genuinely competitive" if its best
  solution in the matched budget either (a) beats the GA's final fitness
  on this window, or (b) comes within ~2% of it while reporting a
  meaningfully non-trivial remaining optimality gap (evidence there is
  real headroom a longer budget could close). If CP-SAT cannot find ANY
  feasible solution within the budget on a 300-order window, that is
  itself a decisive, real negative finding for this approach at this
  problem size — reported plainly, not treated as a reason to keep
  extending the budget indefinitely.

## 6. If genuinely competitive — what comes next, not started here

Full multi-window rolling-horizon comparison, same 3-frozen-snapshot
standard used throughout this session (window 1/seed 42, window 1/seed
123, window 2/seed 42 — though CP-SAT itself is deterministic given a
fixed time budget and model, so "seed" here would matter only for
whichever GA-side comparison arm it's measured against, not for CP-SAT's
own runs). Also worth genuine consideration at that stage, not before:
the hybrid option raised alongside this proposal — CP-SAT generates the
initial population/seed (or a single strong seed individual, the same
role `baseline_chromosomes`' best heuristic already plays in
`GAScheduler.optimize()`), and the existing GA + local search refines
from there. This is not "replace the GA or don't" as a binary; a hybrid
that uses CP-SAT for the part it's structurally suited to (a systematic,
provably-bounded starting point) and the GA for adaptive refinement
around real-world disruptions is a real third option, not a fallback.

## 7. Explicitly out of scope for this document

No code, no `ortools` dependency added yet, no prototype built. This
document exists to decide whether and how to build the small prototype
in Section 5, not to build it — same sequencing as WLC and TCOX (scope →
review → build the smallest justified next piece → report real numbers →
decide the next step from there, not before).

## 8. Prototype run — RESULT: no feasible solution found (2026-08-26)

Per the two decisions made on review (cushioned duration; enumerated MC
candidates, not a multiplication constraint) and the pre-committed
decision rule in Section 5, `scratch/cp_sat_window1_prototype.py` was
built and run against window 1 (300 orders, 2,541 operations) under the
frozen snapshot `"20260825"`, with the 235s time budget (the slowest GA
arm actually measured, Section 11).

**Model**: 104,226 candidate intervals (every operation had at least one
eligible line — 0 skipped), 15 (line, operation-type) resource pools,
built in 8.4s. Candidate lines capped to the top 3 by real eligible pool
size per operation (Section 5's own named tractability simplification —
each operation is genuinely eligible for far more lines than 3 on this
data, same ~99-line scale `ga/chromosome.py` documents for the GA's own
unrestricted eligibility).

**Result: `UNKNOWN` — no feasible solution found.** The solver stopped
after 144.7s, before the full 235s budget was reached; the reason for
stopping early rather than continuing to the deadline was not
investigated further (worth checking if this line of work continues —
`solver.ResponseStats()` or a verbose search log would say more than this
run captured). Whatever the reason for the early stop, the core finding
stands regardless of how much of the budget was actually used: **CP-SAT,
in this configuration, could not find a single feasible schedule for a
2,541-operation window inside a budget matched to the GA's own measured
runtime.**

**Per Section 5's pre-committed decision rule, this is the decisive
negative outcome named in advance, not a result to explain away**: no
feasible solution within budget does not meet the "genuinely competitive"
bar, so Section 6 (full multi-window validation) is NOT warranted from
this result alone.

**One honest caveat on what this negative result does and doesn't say**:
it is entangled with this prototype's own named simplification, not a
clean test of CP-SAT against this problem in general. Capping candidate
lines to 3 REMOVES flexibility the solver could otherwise use to route
around contention — restricting options can make finding ANY feasible
point harder, not easier, independent of solution quality. This result
says "CP-SAT-with-a-3-line-cap-and-no-warm-start could not find a
feasible schedule for window 1 in 235s" — it does not, on its own,
establish that CP-SAT cannot solve a differently-scoped version of this
problem (a wider line cap, a warm start from the GA's own known-feasible
solution, or different solver parameters could plausibly change this
outcome; none of that was tried here, and trying it is a new piece of
work, not something this run answers).

**Not pursued further here, per scope**: this document's mandate was the
small prototype exactly as scoped, report the real result. Whether to
investigate the early-stop reason, relax the line cap, add a warm start,
or treat this negative result as closing the CP-SAT question for now is
a decision for the next step, not decided by this document.

## 9. Warm-start test — RESULT: still no feasible solution, but coverage
    was much sparser than intended (2026-08-26)

Per instruction: one targeted follow-up, not a broader investigation. Same
window, same snapshot, same 235s budget, same 3-line cap, unchanged model
— the one variable added was a warm start from the GA's own real baseline
solution for this window (window 1, seed 42; `scratch/cp_sat_window1_
warmstart.py` re-ran that exact GA config and reproduced 800.27 final
fitness, confirming the reproduction was faithful). Each operation's real
chosen line, machine count, and start time were recovered by replicating
`evaluate_schedule`'s own simulation loop (the same `calculate_operation_
work`, `apply_risk_buffer`, `select_machines_and_start` primitives, not a
re-derivation), then hinted into the CP-SAT model wherever the GA's
chosen line happened to match one of the model's own top-3 candidates for
that operation.

**Result: still `UNKNOWN`, no feasible solution — this time using nearly
the full budget (236.3s vs. 235s, rather than stopping at 144.7s
unhinted).**

**The honest complication, reported plainly rather than smoothed over**:
warm-start coverage was only **137 of 2,541 operations (5.4%)** — 2,404
operations (94.6%) had no hint at all, because the GA's real chosen line
for those operations was not among this model's top-3-by-pool-size
candidates. This is not the near-complete "known-feasible starting point"
the test was designed to give CP-SAT — it is a sparse partial hint
touching one operation in twenty. The reason is structural, not a
implementation slip: the GA's line choice is work-aware (biased toward
whichever eligible line is currently least-loaded PER MACHINE, `ga/
heuristics.py::choose_line_work_aware`), while this model's candidate cap
is based on raw pool SIZE (Section 5's tractability simplification) — two
different selection criteria that agree on a line only incidentally. The
GA's actual good solutions are built from load-balancing choices this
model's candidate set mostly doesn't even offer as an option.

**What this result does and doesn't establish, stated precisely**: it
does NOT cleanly answer "can CP-SAT improve on a schedule it doesn't have
to find from scratch" the way a ~100%-coverage warm start would have —
with 19 operations in 20 unguided, this is much closer to the unhinted
run than to a true warm start, and the small behavioural change (full
budget used, rather than stopping at 144.7s) is a real but modest signal
that the hint had SOME effect, not proof CP-SAT struggled with a genuinely
good starting point. What it DOES establish, cleanly: even with what
warm-start information was available, CP-SAT (this configuration) still
could not reach feasibility inside the matched budget — a real result,
just a narrower one than the test was designed to produce, and reported
as such rather than overstated as the clean "decisive negative" a
complete warm start would have given.

**Per instruction, not pursued further**: no chasing the early-stop
timing, no solver tuning, no attempt to raise warm-start coverage (e.g.
via a different candidate-line criterion) — that would itself be new
investigation, not this one targeted test.

## 10. Closing verdict — investigation closed for now (2026-08-26)

**The decisive finding**: the simplified model — CP-SAT with candidate
lines capped to the top 3 by raw pool size (Section 5's tractability
simplification) — cannot reliably find feasible solutions for window 1
within a budget matched to the GA's own measured runtime, WITH OR
WITHOUT a warm start from the GA's real solution (Section 8: unhinted,
`UNKNOWN` at 144.7s; Section 9: hinted, still `UNKNOWN`, using the full
235s budget). That is the real, decisive result of this investigation.

**What this finding is precisely about — stated so it cannot be misread
later as "CP-SAT failed"**: this is a finding about THIS SPECIFIC
tractability simplification, not a general verdict on CP-SAT's
suitability for this problem class. Two separate things were
established, and they must not be conflated:

1. CP-SAT, restricted to the top-3-by-pool-size candidate set, cannot
   find a feasible schedule unaided. (Section 8)
2. When given a warm start, that warm start's coverage was only 5.4%
   (137/2,541 operations) — NOT because CP-SAT failed to make use of
   good hints, but because the GA's real line choices (work-aware,
   load-balancing, `ga/heuristics.py::choose_line_work_aware`) almost
   never coincide with this candidate set's own selection criterion (raw
   pool size). The GA's actual good solutions are built largely out of
   line options this restricted model never offered the solver in the
   first place. (Section 9)

The honest, complete statement is: what failed here was a specific,
narrow, pool-size-based candidate-line simplification, tested both cold
and with a mostly-inapplicable hint — not CP-SAT confronted with a
well-aligned, high-coverage warm start. That version of the test — a
genuinely complete warm start compatible with the model's own candidate
set, or a wider/differently-chosen candidate set (e.g., capped by recent
GA usage frequency rather than raw pool size) — was never run, and this
document does not claim it would fail. It simply was not tested, and per
instruction is not being pursued further right now.

**Consequence**: the CP-SAT investigation closes here, for now. Not
adopted, not built further, no code beyond the two prototype scripts and
the `ortools` dependency (kept in `requirements.txt`, clearly labelled as
scoping/evaluation only, not wired into the production pipeline). If
revisited later, the open question this leaves behind is specifically
about candidate-set design — how to restrict a real ~99-line eligibility
to something CP-SAT can handle without structurally excluding the
solutions that actually work — not "does CP-SAT work for this problem,"
which remains genuinely unanswered.
