# Delay-Label Fix — Scoping Proposal (no code changes)

Companion to the model-module audit (see chat). This is a design proposal
only, per instruction — nothing here has been implemented. Reviewed and
approved before any reseed happens.

---

## 1. The problem, precisely, traced through the actual code

`data_generation/seed_database_fixed.py`, inside the per-lot dispatch loop:

```python
actual_start_t = start_t
actual_work_mins = duration_mins * sample_execution_variance_multiplier()
actual_end_t = calculate_shift_aware_end_time(
    start_t, actual_work_mins + (repair_time_mins if dynamic_breakdown else 0)
)
```

`sample_execution_variance_multiplier()`:

```python
EXECUTION_VARIANCE_MEAN = 1.0
EXECUTION_VARIANCE_SD = 0.15
EXECUTION_VARIANCE_MIN = 0.65
EXECUTION_VARIANCE_MAX = 1.6

def sample_execution_variance_multiplier(rng=random):
    return min(EXECUTION_VARIANCE_MAX, max(EXECUTION_VARIANCE_MIN,
        rng.gauss(EXECUTION_VARIANCE_MEAN, EXECUTION_VARIANCE_SD)))
```

`actual_end_t` (via `_aggregate_actual_vs_planned` in `graph/labels.py`) is
the **sole** source of the GNN delay label. Tracing exactly what does and
doesn't reach it:

- **Already causal, correctly**: `dynamic_breakdown` — a 15% roll once a
  machine's cumulative runtime exceeds 1,200 minutes — adds
  `repair_time_mins` (120-360 min) directly onto `actual_work_mins` for
  the specific lot it occurs on. This is a real, working channel already.
- **Captured but NOT causal to `actual_end_t`**: `machine_breakdown_count_at_dispatch`
  (this machine's *prior* breakdown history, not this lot's own roll),
  `on_leave` (this lot's assigned employee's leave status), and
  `queue_delay_mins` (how long this lot waited before starting). All
  three are computed and stored (feeding `mean_breakdown_count_norm`,
  the removed `frac_employee_leave`, and `mean_queue_delay_norm` as node
  features) but **never influence `actual_work_mins` at all.**

This is confirmed, not inferred, as the root cause: three independent
methods (raw correlation, permutation importance on the trained model,
143-of-150-epoch training flatness) all point at the same fact — the
label's variance is dominated by a fixed-distribution random draw with no
dependence on anything the model can observe.

One structural note worth flagging before the mechanism design: `queue_delay_mins`
already shifts `start_t` (via `earliest_possible`), and **both**
`planned_end_t` and `actual_end_t` are computed from the *same* `start_t`.
So queue delay is already "priced in" equally on both sides — a heavily
queued lot doesn't naturally produce *relative* lateness (actual vs.
plan) under the current design, and it shouldn't be forced to as if that
were the bug. Its proposed mechanism (below) is deliberately different in
kind from the other two for exactly this reason.

---

## 2. Proposed mechanism, per causal factor

All three modify `sample_execution_variance_multiplier()`'s effective
parameters at the point of the per-lot draw — using `machine_breakdown_count_at_dispatch`,
`on_leave`, and `queue_delay_mins`, all three already computed at that
point in the loop, zero new data needed.

### 2a. Machine breakdown history → mean shift

```python
breakdown_effect = min(0.08, machine_breakdown_count_at_dispatch * 0.01)
```

A machine with a longer prior breakdown history runs a *bit* slower on
average even between discrete failures — mechanical wear, alignment
drift, the kind of thing TPM programs track separately from outright
downtime. **Grounded, not invented**: OEE literature's "performance rate"
component (distinct from availability/breakdown time itself) is
consistently reported with speed losses "reducing effective output by
8%" in typical production scenarios ([EMQ](https://www.emqx.com/en/blog/oee-in-lean-manufacturing)).
That's the ceiling I've used. The per-breakdown increment (1%) is a
reasoned choice, not independently cited: it's calibrated so the
*mean* real machine in this database (breakdown count ≈3.1, per the
existing `mean_breakdown_count_norm` measurement) sees roughly a third
of the ceiling effect (~3%), and only chronically-problematic machines
(count ≥8, near the measured max of 9) reach the full 8%.

### 2b. Worker leave / substitute coverage → mean shift

```python
leave_effect = 0.10 if on_leave else 0.0
```

`on_leave=True` means this specific lot's assigned employee was flagged
on leave at dispatch — in practice, coverage by a substitute, a
less-familiar operator, or a batch/factory-fallback assignment (per the
seeder's own tiered fallback logic). **Grounded directionally, the exact
figure is a reasoned assumption**: apparel-manufacturing planning
literature is explicit that real garment lines are planned at ~75%
efficiency, not 100%, with "regular operator absences" named as one of
the standing reasons for that gap, alongside changeovers and breakdowns
([OnlineClothingStudy](https://www.onlineclothingstudy.com/2021/08/why-do-garment-manufacturers-plan.html)).
That source bundles multiple frictions together and doesn't isolate
absence-coverage's own share, so I can't cite a clean isolated
percentage — 10% is chosen as a moderate value comfortably inside that
~25%-total-friction envelope (leaving room for the breakdown effect and
existing baseline noise to coexist under the same ceiling without
stacking into something unrealistic).

### 2c. Queue congestion → variance widening, not mean shift

```python
queue_sd_multiplier = 1.0 + min(0.5, queue_delay_mins / 10000.0 * 0.5)
effective_sd = EXECUTION_VARIANCE_SD * queue_sd_multiplier
```

Deliberately a **different kind of effect** from the two above, for the
structural reason in Section 1: queue delay already shifts *when* a lot
starts identically for both plan and actual, so it shouldn't also force
a mean slowdown once work begins — that would double-count the same
congestion twice. What congestion literature does support is increased
*unpredictability*: WIP/queue-time literature confirms "the variability
of production lead-time increases as the queue grows" and that
high-WIP conditions make "schedule adherence nearly impossible because
planning estimates are unreliable" ([User Solutions RMDB](https://usersolutions.com/blog/wip-management)).
Widening the SD (up to 1.5x at very high queue delay, ~7+ days) encodes
exactly that: heavily-backlogged dispatch conditions produce *less
predictable* outcomes, not a directionally slower ones. The specific
scaling constant (queue delay normalized against 10,000 minutes, capped
at +50% SD) is a reasoned choice matched to this database's own measured
queue-delay range (p99 ≈ 10.9 days pre-Module-7, ≈9.3 days after), not
an external citation.

### Combined formula

```python
effective_mean = EXECUTION_VARIANCE_MEAN + breakdown_effect + leave_effect
effective_sd = EXECUTION_VARIANCE_SD * queue_sd_multiplier
actual_work_mins = duration_mins * min(EXECUTION_VARIANCE_MAX, max(EXECUTION_VARIANCE_MIN,
    random.gauss(effective_mean, effective_sd)))
```

`EXECUTION_VARIANCE_MIN`/`MAX` (0.65/1.6) stay unchanged — at the
maximum realistic stacking (breakdown 8% + leave 10% = effective mean
1.18, SD up to 0.225), the existing clip bounds still contain the large
majority of the shifted distribution; only genuinely extreme draws get
clipped, consistent with the existing "clipped normal, not unbounded"
rationale.

---

## 3. Why this shouldn't repeat "too weak" or "too strong"

- **Not deterministic**: every effect is a *shift* to the distribution
  parameters, never a direct assignment of lateness. Noise stays on top
  of every causal factor — an operation with a bad-history machine and
  an on-leave employee is *more likely* to run long, not guaranteed to.
- **Bounded, not runaway**: each effect has an explicit cap (8%, 10%,
  50% SD respectively); combined worst-case stacking was checked against
  the existing clip bounds above, not left to chance.
- **Grounded where evidence exists, flagged where it's a judgment call**:
  the OEE 8% ceiling and the WIP-variability *direction* are cited; the
  garment-specific leave magnitude and the exact per-unit scaling
  constants are explicitly reasoned assumptions, stated as such rather
  than presented as more certain than they are.

---

## 4. Expected resulting label statistics — an estimate, not a promise

Current: 8.37% positive rate (n=8,745), essentially unlearnable (per the
audit's three-method confirmation).

I'm not going to hand-derive a precise new positive rate — the label's
real aggregation (MAX actual-end minus MAX planned-end across an
operation's ~19 lots, compared against mean single-lot duration) has
already burned me once this session on a hand-derived estimate that
overstated the true number by roughly 10x, because it doesn't reduce to
a simple closed form. What I can state with reasonable confidence,
directionally:

- The positive rate should **rise moderately**, plausibly into a
  **12-20% range** — a meaningful minority of operations will have at
  least one lot touched by an elevated-mean draw (a machine with real
  breakdown history, or a leave-covered lot) across their ~19 lots, while
  operations with clean machines and full staffing throughout stay close
  to the current noise-only ~8% baseline.
- The label should become **measurably more discriminating**: LogisticRegression/
  RandomForest baselines fit on the now-causally-connected features
  (`mean_breakdown_count_norm`, a reinstated `frac_employee_leave` --
  see Section 6 -- and `mean_queue_delay_norm`) should clear the
  no-skill floor by a real margin, not the ~0.03 AP edge currently
  measured.

**This must be measured, not assumed**, before committing to a full
retrain — see Section 5's first step.

---

## 5. What gets reseeded / retrained / re-verified

**Reseed**: yes, full reseed required (the seeder is monolithic; there's
no way to regenerate only `actual_start_time`/`actual_end_time` in
isolation). With the fixed seed (42), only `actual_*` timestamps and
values causally downstream of them should change in any way that
matters.

**Confirmed untouched, by tracing the actual code, not assumed**:
- `planned_start_time`/`planned_end_time` (`start_t`/`end_t`): explicitly
  computed *before* and independently of `actual_work_mins` in the loop
  — this proposal touches nothing upstream of them.
- `execution_status`: derived from `has_causal_delay`, `start_t`, `end_t`,
  `now_time` only — never reads `actual_end_t`. Unaffected.
- **Completion label** (`_aggregate_planned_span` in `graph/labels.py`):
  sources exclusively from `planned_start_time`/`planned_end_time`. Unaffected.
- **Priority label** (`_slack_days_for`): sources from `order_deadline`,
  planned_start (same source as above), `release_date`. Unaffected.
- **All 9 node features**: `mean_breakdown_count_norm`, `mean_queue_delay_norm`,
  `pool_occupancy_norm`, `machine_parallelism_norm`, etc. are all built
  from dispatch-context/assignment data captured *before* a lot's own
  outcome — this proposal doesn't change how any of those columns are
  computed, only adds them as additional *inputs* to the actual-timing
  draw. Unaffected.

So this is a narrowly-scoped change to `actual_start_time`/`actual_end_time`
values only, and through them, to the delay label only — verified by
tracing every consumer, not assumed from the design intent.

**Sequence, once approved**:
1. Backup DB, implement, reseed.
2. **Data-level check first** (matching this project's established
   discipline): pull the new label distribution directly — positive
   rate, correlation of the three causal features against it — *before*
   spending a ~150-epoch retrain. If the positive rate or correlations
   look degenerate (still ~flat, or swung to some unrealistic extreme),
   stop and adjust the constants rather than train on it.
3. Full retrain via the real `train_gnn()` production entry point.
4. Fair-fight comparison specifically on delay (GNN vs. LogisticRegression
   vs. RandomForest) — the actual test of whether this worked.
5. Permutation importance on delay specifically, confirming the *intended*
   features (breakdown history, leave, queue delay) are what's carrying
   any new signal, not some other artifact.
6. Full epoch-by-epoch trajectory check, specifically watching whether
   delay loss finally moves substantially past its historic ~epoch-7
   settle point.

---

## 6. One consequence worth flagging explicitly: `frac_employee_leave`

Step 1 of the model/feature audit removed `frac_employee_leave` because
it showed zero permutation importance **under the old, noise-only
label**. That finding was correct for that label — but if this fix
succeeds, `on_leave` becomes causally connected to delay for the first
time, and the removed feature might become genuinely predictive. **Not
proposing to reinstate it now** — that's a separate decision, to be
made after the new label's own statistics and permutation importance are
actually measured (Section 5, steps 2 and 5), not assumed as a
consequence of this proposal.

---

# Part 2 — Expanded Evaluation Metrics (completion/priority only) — scoping, parallel track

Not implementing yet, per instruction — scoped now so it's ready once
Part 1's design is settled and measured against the *final* post-fix
checkpoint, not one about to be superseded.

**Not for delay**: three independent methods already conclusively
established it's noise-dominated under the current label. More metrics
on a confirmed-flat classifier restates the same fact from another
angle, not new evidence. Revisit only after Part 1 lands and delay
itself is re-measured.

**For completion and priority**:

1. **Calibration** — reliability curves (predicted value bucketed vs.
   mean actual outcome in that bucket) for both heads. Priority in
   particular: since its magnitude directly weights the GA fitness
   reward term (not just used for ranking), a priority of 0.8 needs to
   actually *mean* something close to 0.8's real urgency, not just
   rank correctly relative to other predictions. Standard tooling:
   `sklearn.calibration.calibration_curve`-style binning, adapted for a
   continuous regression target rather than a classifier.
2. **Error breakdown by garment category** — completion's MAE/R² split
   by `products.category`, specifically checking whether the small-count
   categories (Helmet n=2, Tube n=3, Jump Suit n=5, Skirt n=21 real
   source rows, per the data audit) show blown-up error that the
   aggregate R²=0.57-0.60 currently hides entirely. A single held-out
   Helmet or Tube operation landing in the validation split could swing
   its own category's error wildly with so few examples — worth knowing
   whether that's happening, not assuming it away.
3. **Residual plots** — predicted vs. actual scatter, and residual vs.
   predicted, for both heads. Quick visual check for systematic bias
   (e.g., consistently under-predicting long-duration operations, or
   a specific band of predictions that's fine everywhere except one
   region) that an aggregate MAE/R² can't surface on its own.

Deliverable once scoped for real: a short script (not part of the
production pipeline, an evaluation-only addition) run against the
checkpoint produced by Part 1's eventual retrain, plus a written summary
of what it found — same "measure, then report" discipline as everything
else this session.

---

## IMPLEMENTATION STATUS (as of 2026-08-21) — read this first if resuming

**Part 1 (delay-label fix): implemented, retrained, verified. Done, with one
open decision below.**

- `sample_execution_variance_multiplier()` rewritten per this doc's Section 2
  (breakdown/leave mean-shift, queue SD-widening). DB backed up
  (`backups/pre_delay_label_fix_20260820_192323.sql`), reseeded. New positive
  rate measured: **10.63%** (sane, per condition 1).
- **Aggregation-mismatch bug found and fixed mid-verification**: the
  breakdown/leave features were averaging across an operation's ~19 lots,
  diluting a real per-lot causal signal to near-zero at the operation level
  (the label is MAX-based, driven by one decisive lot, not an average of
  all of them — the same class of bug already fixed once for the label
  itself). Fixed in `graph/node_features.py::_aggregate_dispatch_context`:
  breakdown/leave now read from the DECISIVE lot (max `planned_end_time`,
  96.0%-validated leak-safe proxy for the true max-actual-end lot); queue
  delay deliberately stays a mean (its mechanism is SD-widening, not a mean
  shift, so averaging was never the problem there). `database/queries.py`
  now also exposes `planned_end_time` for this. Feature renamed
  `mean_breakdown_count_norm` → `decisive_lot_breakdown_count_norm` (the old
  name became a misnomer). Tests updated (`tests/test_dispatch_context_features.py`),
  402/402 passing.
- Re-ran the per-lot vs. operation-level correlation check after the fix:
  leave fully recovered (r=0.1246 vs. per-lot 0.1555); breakdown only
  partially recovered on its own (r=0.0178, p=0.097) — diagnosed as
  queue-delay noise on the same decisive lot swamping a small signal, NOT a
  proxy failure: restricting to lots with near-zero queue delay recovered
  r=0.0375 (p=0.002), ~92% of the per-lot number. Confirmed via a fresh
  correlation re-check, not assumed.
- **150-epoch retrain completed** (via `python models/train.py`, the real
  production entry point — two earlier background attempts were killed by
  the OS at ~60-75min; ruled out a code-level memory leak by full review
  and by adding per-epoch RSS logging to `models/train.py` itself, which
  showed memory is flat/bounded per epoch; root cause was most likely
  system-wide RAM pressure, not this code — 16GB total, ~3.6GB free at the
  time). Final checkpoint: `outputs/checkpoints/gnn_multitask_latest.pth`
  (2026-08-21 02:32).
- **Results, verified**:
  - **Delay**: real, above-chance signal for the first time — AP 0.1617 vs.
    no-skill baseline 0.1118; ROC AUC 0.54 vs. 0.50. Modest, as expected
    (the restored causal effects themselves are modest: r=0.0375 breakdown,
    r=0.1246 leave) — not a dramatic fix, a genuine one.
  - **Priority**: confirmed unchanged (R² 0.8241 vs. baseline ~0.87, Δ=-0.046,
    within the ±0.05 tolerance).
  - **Completion**: MOVED (R² 0.6376 vs. baseline ~0.57, Δ=+0.068) — investigated
    per the stop-gate, and **the conclusion evolved twice, both times only
    after actually measuring**:
    1. First hypothesis: pure calendar drift (this project's seeder anchors
       `base_time`/`order_start` to real `datetime.now()`, not the fixed
       random seed — confirmed in code and empirically: comparing the
       pre-fix DB backup to the current one, 167,690/167,691
       `job_shop_schedules` rows differ in planned times, and completion's
       *label itself* (duration span) shifted +100.5min on average). This
       part is real and confirmed.
    2. But a same-database, aggregation-only ablation (old mean-based vs.
       new decisive-lot breakdown feature, holding data/labels/model type
       fixed, via LinearRegression + 5-seed RandomForest) showed the
       aggregation fix ALSO has its own real, robust effect: completion R²
       drops ~0.06-0.11 with the new feature (RF: 0.569→0.458, 5 seeds,
       noise band only ±0.002). Feature importance explains why: the old
       mean-based breakdown feature was 42.6% of completion's RF
       importance (a good proxy for an operation's OVERALL multi-lot
       congestion, since completion is a span across all lots) vs. only
       9.5% for the new decisive-lot version (correctly focused on delay's
       single decisive lot instead).
    - **Net conclusion**: BOTH effects are real and run in opposite
      directions (drift pushes completion up, the aggregation fix pushes
      it down); they happened to net positive in the actual retrain. The
      fix is NOT an innocent bystander — it has a genuine, explainable cost
      to completion, alongside its genuine delay benefit.

**DECISION MADE AND IMPLEMENTED (2026-08-22, update to the above — option
(b) was chosen):** completion got its own separate mean-based breakdown
feature (`mean_breakdown_count_norm`) reinstated as an ADDITIONAL column
alongside the decisive-lot one (`decisive_lot_breakdown_count_norm`) — 10
continuous features total. Retrained 150 epochs (`gnn_multitask_latest.pth`,
2026-08-22 05:28, this checkpoint referred to below as "Option B").
**Verified working, not just theorized:**
  - Completion R² = 0.6675 (val), beating RandomForest (0.5712) and its own
    pre-Option-B checkpoint (0.6376) — the fix recovered the cost and then
    some.
  - Delay essentially unchanged (AP 0.1591 vs. 0.1617 pre-Option-B, within
    noise) — confirms the fix didn't cost delay anything.
  - Priority unaffected as expected (R² 0.9240).
  - Permutation importance (previously flagged pending) run against this
    checkpoint: `mean_breakdown_count_norm` matters for completion (0.060 R²
    drop) as designed, AND turned out to be priority's SECOND-biggest driver
    (0.41 R² drop, next to `deadline_budget_norm`'s 0.91) — an unplanned
    bonus. `frac_employee_leave` was never reinstated (stays excluded; the
    check that was pending is now moot since it was never added back).
  - Fair-fight re-run under Option B: GNN now WINS on delay AP (0.159 vs.
    LogisticRegression 0.145 / RandomForest 0.150) and decisively wins
    completion R² (0.667 vs. RF's 0.571) — a reversal of the OLD 0H.12-0H.15
    verdict, which was measured against the pre-fix, near-degenerate label
    and does not transfer.

Part 2 (calibration) — **done for delay** (not originally scoped for it,
but the trigger condition below was met): see the new section below. Still
not done for completion/priority as originally scoped (error-breakdown by
category, residual plots) — genuinely still pending, lower priority now
given the finding below.

---

## DELAY'S THEORETICAL AP CEILING — MEASURED, NOT ASSUMED (2026-08-23)

**Question that prompted this**: after Option B, delay AP sits at 0.159–
0.172 (0.172 from an unreplicated split-encoder architecture experiment —
see chat/PROJECT_STATUS.txt for that thread). Is that actually close to as
good as this label can ever be predicted, or is real signal being left on
the table by imperfect features/architecture ("maybe feature selection is
wrong")?

**This is synthetic data, so the ceiling isn't a statistical inference —
it's computable directly from the label's own generating function**
(`data_generation/seed_database_fixed.py::sample_execution_variance_multiplier`).
Method: for every held-out validation operation, took its REAL recorded
per-lot causal inputs (exact `machine_breakdown_count_at_dispatch`,
`employee_on_leave_at_dispatch`, `queue_delay_mins_at_dispatch` — not the
GNN's lossy aggregated proxies) and Monte-Carlo resampled the multiplier
500 times per operation to get an ORACLE P(label=1 | true causal inputs) —
a prediction no real feature set could ever beat, since it conditions on
the exact values the label's randomness is generated from. Scored via AP
against the operations' real observed labels (n=1,744, same val split the
GNN was scored on).

**Result: theoretical ceiling AP = 0.1574 (no-skill baseline 0.1118). The
GNN's actual measured AP (0.159–0.172) is statistically indistinguishable
from this ceiling, not clearly above it.**

**CORRECTED 2026-09-01** (SESSION_HANDOFF.md Thread 2): the original "already
AT OR ABOVE this ceiling" framing above overstated what a single point
estimate can support. A live re-measurement of the current production
checkpoint reproduced the cited AP almost exactly (0.1723), then bootstrapped
it directly — 3,000 resamples of the 1,744-node validation set (195
positives) — giving a 90% CI of **[0.1424, 0.2200]**. The 0.1574 ceiling
sits at the **19th percentile** of that distribution: comfortably inside
ordinary estimation noise for a sample this size, not evidence the model has
exceeded a supposedly unbeatable bound. (A separate hypothesis, that the
graph's `contends_with` contention edges were leaking validation signal and
inflating this number, was tested directly — an inference-time ablation
removing val←train edges of that type barely moved it, 0.1723 → 0.1715 — and
ruled out; see PROJECT_STATUS.txt's dated 2026-09-01 section.) The
conclusion below is unaffected: the two readings aren't distinguishable, so
there's still no evidence of exploitable signal being left on the table.

**Conclusion: feature selection is not the problem, and further
architecture/feature work on delay has low expected payoff.** The model is
already extracting essentially all the causal signal the label's own
design contains. Why the ceiling is this low: each lot's outcome is drawn
from Normal(1.0, 0.15) clipped to [0.65, 1.6] (`EXECUTION_VARIANCE_MEAN/SD/
MIN/MAX`), with breakdown history and leave only shifting the MEAN by up to
+8%/+10% and queue delay only widening the SD by up to 50% — small nudges
on a wide base-noise distribution. The operation label then takes the MAX
across ~19 such draws, which is dominated by which lot's noise happened to
land high, not by the causal factors riding on top of it. This is a
consequence of the label's deliberately modest, realistic effect sizes
(a defensible design choice, not a bug) — not a fixable modeling gap.

**One honest limitation, not resolved**: the simulation modeled the three
documented continuous mechanisms above but did NOT model a separate
discrete term also present in the seeder — `repair_time_mins` added when a
NEW ("dynamic") breakdown occurs during a specific lot's own execution
(distinct from `machine_breakdown_count_at_dispatch`'s PRIOR-history
effect). Whether that event is itself predictable from any pre-dispatch
signal was not checked; if it is a genuinely independent draw, it would be
equally invisible to any real predictive model too and wouldn't change the
practical ceiling — but this was not verified, so 0.1574 should be read as
"the ceiling from the three known continuous mechanisms," not proven to be
the absolute final number.

**Practical implication going forward**: don't spend further effort trying
to make delay MORE predictable via features/architecture — the ceiling is
label-set, not model-set, and this is now a measured fact rather than a
judgment call. The higher-leverage direction already identified and tested
is using the existing model's confidence well (the gated-buffer ablation —
see PROJECT_STATUS.txt / chat, 2026-08-23), not sharpening the classifier
further. If a more predictable delay signal is ever wanted, that requires
changing the LABEL's generating design in the seeder (larger effect sizes,
a different aggregation), not tuning what reads it.

Script: `scratch/delay_dgp_ceiling.py` (one-off, not part of the pipeline).

---

## POS_WEIGHT ON DELAY'S SEPARATE ENCODER BRANCH — TESTED, NOT ADOPTED (2026-08-24)

Tested after the split-encoder migration above. Delay has separate projection
and graph-encoder weights, but shares the trainable op_type_embedding and
training controls with completion/priority; this is not full isolation.
`pos_weight` computed from the real train-set class balance (735/7001
positive, `neg/pos = 8.5252`), applied to `BCEWithLogitsLoss`. 150 epochs,
otherwise identical to production `models/train.py`.

**Result: a net negative, not adopted.**

| metric | production (no pos_weight) | pos_weight=8.5252 | Δ |
|---|---|---|---|
| Delay AP | 0.1723 | 0.1698 | -0.0025 |
| Delay ROC-AUC | 0.5579 | 0.5497 | -0.0082 |
| Completion R² | 0.6914 | **0.6366** | **-0.0548** |
| Priority R² | 0.9303 | **0.8479** | **-0.0824** |

Delay itself didn't improve (flat to slightly worse) — no gap was closed.
Completion and priority also regressed. The branches share one trainable
`op_type_embedding`, one optimizer, one `ReduceLROnPlateau` LR scheduler,
and combined-validation-loss checkpoint selection. Both losses can update
the embedding, so parameter coupling remains. Another possible channel,
NOT confirmed (neither script logs LR directly), is the shared scheduler
watching the *combined* `val_total_loss`.
`pos_weight=8.5` inflates delay's loss magnitude ~4x (0.35 → 1.27), so the
combined total the scheduler watches is now dominated by delay's inflated,
early-plateauing number — plausibly triggering an earlier LR cut than the
baseline run, before completion/priority (still improving on their own)
had finished benefiting from the higher LR. A shared scheduler is a real,
if indirect, coupling channel between branches with separate graph encoders —
worth remembering for any future per-head loss-weight change.

Production checkpoint (`gnn_multitask_latest.pth`) was never touched by
this experiment — it trained to a separate file
(`outputs/checkpoints/gnn_pos_weight_delay_best.pth`), so nothing needed
rolling back. Script: `scratch/train_pos_weight.py`.
