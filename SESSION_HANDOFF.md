# Session Handoff — Six Threads, Re-Verified Against Real Output

A working session spent checking claims already in the record — a metric's denominator, a classifier's ceiling, an adopted decision's evidence — against what the code and data actually do right now. Two hold up unmodified. One does not replicate.

**Scope:** labour utilisation · delay-classifier AP · buffer ablation · run-report tool
**Runs executed:** 9 full rolling-horizon runs, 3 seeds × 3 policies
**Net code change:** 0 production files reverted or altered

| # | Thread | Status |
|---|---|---|
| 1 | Labour utilisation denominator | ✅ No bug |
| 2 | Delay AP vs. theoretical ceiling | ✅ Resolved |
| 3 | Precision-at-top-k table | ➖ Data ready |
| 4 | ORDER BY reproducibility | ⚠️ Mixed |
| 5 | Buffer policy ablation, 3 seeds | ❌ Doesn't replicate |
| 6 | Human-readable run report | ✅ Built & tested |

---

## 1. Labour utilisation denominator

**Ask:** suspicion raised that `labour_utilisation_pct` might divide by raw calendar hours × full headcount, hiding a much higher real figure.

**Verdict: no bug found.**

Traced to `ga/rolling_horizon.py:382-384`. The denominator is `capacity × horizon_min`, where `capacity` comes from `database/queries.py::fetch_concurrent_operator_capacity()` — which already models the real 3-shift rotation (×1/3) and staggered rest days (crew ×6/7, floaters ÷ distinct bands), read from live `Batch_Members` / `Work_Schedules` data. This is the exact fix the docstring says was already made once, for the same suspicion. Verified live: concurrent capacity **4,004** vs. raw 3-shift roster **13,884** — the correct path is genuinely in use.

**Why the whole-book figure reads 0.52%, not 37%.** Makespan grew from 91.5 to 324.4 days after the (separately verified) release-date-enforcement fix. Same capacity, same work, ~3.5× longer calendar span — the drop is real and structural, not a metric artefact.

### Dense vs. slack, not one blended average

Extended on request: per-window figures from `outputs/authoritative_comparison_report.json`, using the dense/slack split already established there (windows 1–2 dense, 3–4 slack).

Time-weighted labour utilisation — dense period vs. slack tail, GA vs. baselines:

| policy | dense (w1–2, 16.4 d) | slack tail (w3–4, 308.0 d) | whole-book blend |
|---|---|---|---|
| **GA** | **4.80%** | 0.29% | 0.52% |
| FIFO | 3.21% | 0.30% | 0.52% |
| EDF | 3.02% | 0.30% | 0.52% |
| CriticalRatio | 3.22% | 0.30% | 0.52% |

**For Table 1:** quote the dense-period figure (~4.8–5.9% for GA, per-window) alongside the whole-book blend, not instead of it — the blend alone reads as near-zero because a 308-day near-idle tail swamps a genuinely busy 16-day opening.

---

## 2. Delay classifier AP vs. its own theoretical ceiling

**Ask:** the classifier's measured AP (0.159–0.172) sits at or above the label's own DGP-computed oracle ceiling (0.1574) — a result that should be near-impossible. Find a real cause, or re-check for leakage.

**Verdict: resolved — noise, not leakage.**

Live re-measurement of the current production checkpoint reproduced the cited range almost exactly: **AP 0.1723**, ROC-AUC 0.5579, positive rate 0.1118 — confirming the number in the proposal document is current, not stale.

### Two hypotheses tested, not just proposed

- **Leakage via contention edges.** Found a real, previously unaudited exposure: the graph's company-wide `contends_with` edges give **100% of validation nodes** direct message-passing access to a mean of **1,627 training nodes** each (max 2,101) — far larger than the already-documented and already-fixed 72.8% precedence-edge exposure. A direct ablation (removing val←train edges of this type at inference, same trained weights) barely moved the number: **0.1723 → 0.1715**. Ruled out as the cause.
- **Sampling noise.** Bootstrapped the model's own AP, 3,000 resamples of the 1,744-node validation set (195 positives): 90% CI **[0.1424, 0.2200]**. The 0.1574 ceiling sits at the **19th percentile** of that distribution — comfortably inside ordinary estimation noise for a sample this size.

> **Correct framing for the paper:** the classifier's AP is statistically indistinguishable from its theoretical ceiling, not clearly above it. `DELAY_LABEL_FIX_PROPOSAL.md`'s "already AT OR ABOVE this ceiling" line should be softened accordingly.

The contention-edge exposure is real on its own terms even though it didn't explain this metric — see decision 2 below.

---

## 3. Precision-at-top-k / confidence lift

**Ask:** the paper asserts the classifier's top-confidence tail discriminates better than its aggregate, without showing the evidence.

**Verdict: real table, ready to publish.**

Regenerated live via `scratch/delay_calibration_and_confidence_gate.py` against the current checkpoint (n=1,744, base rate 11.18%):

| top K% | flagged | precision | recall | lift vs. base rate |
|---|---|---|---|---|
| 2% | 35 | 34.3% | 6.2% | **3.07×** |
| 5% | 87 | 23.0% | 10.3% | 2.06× |
| 10% | 174 | 23.6% | 21.0% | 2.11× |
| 15% | 262 | 19.1% | 25.6% | 1.71× |
| 20% | 349 | 16.6% | 29.7% | 1.49× |
| 30% | 523 | 14.5% | 39.0% | 1.30× |
| 50% | 872 | 12.7% | 56.9% | 1.14× |

The 15% cutoff's threshold (0.144) independently cross-checks against `RISK_BUFFER_GATE_THRESHOLD = 0.14`'s stated calibration band. Safe to drop straight into the paper as a table or lift-curve figure.

---

## 4. Pre/post ORDER BY reproducibility fix

**Ask:** confirm whether the delay-classifier and buffer-ablation numbers were generated before or after the 2026-08-27 `ORDER BY` fix.

**Verdict: split.**

- **Delay classifier — not affected, no re-run needed.** Traced the actual dependency: `graph/edge_builder.py:100` explicitly `sort_values('operation_sequence_number')`, and `graph/node_features.py` uses that column via groupby/transform — never raw row position. The undefined join order the fix targeted only ever reached `ga/chromosome.py`, which builds its gene list straight from `df_routings.iterrows()` with no re-sort.
- **Buffer ablation — affected, and worse than a reproducibility gap.** The original 3-arm run (`scratch/gated_buffer_ablation.py`, 2026-08-23, seed 42 only) predates *both* the `ORDER BY` fix (08-27) *and* the release-date-enforcement fix (08-24) — it ran under an obsolete ~32-day-makespan regime that no longer exists.

---

## 5. 3-arm buffer ablation, re-run on the corrected pipeline

**Ask:** re-run Disabled / Broad / Gated across 3 seeds on today's fixed pipeline; check whether the original single-seed ADOPT decision holds up — including inside the dense windows specifically.

**Verdict: original decision does not replicate.**

New script: `scratch/gated_buffer_ablation_multiseed.py`. One shared origin/graph/GNN-prediction set, seeds `42, 101, 202`, all 9 runs completed.

Whole-book results, matched by seed:

| seed | arm | on-time | makespan | throughput | mean tard. | labour | margin |
|---|---|---|---|---|---|---|---|
| 42 | A disabled | 52.1% | 320.2 d | 3.21/d | **81 h** | 0.52% | +4.1% |
| 42 | B broad 0.5 | 51.6% | 320.2 d | 3.21/d | 85 h | 0.55% | +4.6% |
| 42 | C gated 0.5 | 51.8% | 320.2 d | 3.21/d | 82 h | 0.53% | +4.2% |
| 101 | A disabled | 51.0% | 320.2 d | 3.21/d | **69 h** | 0.52% | +3.9% |
| 101 | B broad 0.5 | 52.9% | 319.8 d | 3.22/d | 78 h | 0.55% | +3.3% |
| 101 | C gated 0.5 | 53.0% | 319.8 d | 3.22/d | 78 h | 0.53% | +2.9% |
| 202 | A disabled | 51.6% | 319.8 d | 3.22/d | **73 h** | 0.52% | +3.4% |
| 202 | B broad 0.5 | 51.1% | 319.8 d | 3.22/d | 78 h | 0.55% | +3.8% |
| 202 | C gated 0.5 | 51.7% | 319.8 d | 3.22/d | 75 h | 0.53% | +3.5% |

On-time, makespan, throughput and margin all move less between arms than between seeds of the *same* arm — no reliable winner. Mean tardiness is the one seed-consistent signal, and it favours **Disabled** (lowest in all 3 seeds) — the opposite direction from the original ADOPT rationale.

### Extended per-window — does it show up in the dense windows specifically?

Re-run with per-window capture added, using the same dense/slack classification (w1–2 dense, w3–4 slack). It doesn't show up there either:

Time-weighted labour utilisation, dense vs. slack, range across 3 seeds:

| policy | dense (w1–2) | slack (w3–4) |
|---|---|---|
| A disabled | 5.03% – 5.41% | 0.29% |
| B broad 0.5 | 4.92% – 5.34% | 0.31% |
| C gated 0.5 | 5.02% – 5.37% | 0.29% – 0.30% |

Every policy's dense-window range fully overlaps the other two — the crisp whole-book ordering (A<C<B, zero seed spread) turns out to be an artefact of the huge, near-invariant slack tail dominating the blend, not a real per-window effect. Windows 1 and 4 are floor/ceiling effects: on-time is **exactly 7.7%** and **exactly 96.1%** respectively in all 9 runs, contributing zero discriminating power regardless of policy or seed.

> **Operational note:** the background run survived a multi-hour gap when the host machine slept overnight and was later killed; the incremental-save/skip-if-done design meant only the unfinished runs needed resuming, not a restart from zero.

Full raw data: `outputs/gated_buffer_ablation_multiseed.json` (`windows` key per run, plus `range_summary` / `window_range_summary`).

---

## 6. Human-readable run report generator

**Ask:** build a plain-language narrative report from real agent output, for a non-technical reader — not previously built (checked the module-audit phase; no prior attempt existed).

**Verdict: built and tested.**

`reporting/run_report.py` reads `authoritative_comparison_report.json`, `proposed_schedule.json`, `order_completions.json` and an optional breakdown log, and writes `outputs/run_report.md` — four sections (executive summary, order highlights, disruption narrative, honest caveats), every figure sourced from real fields, nothing invented, tier codes translated to plain sentences. 11 tests in `tests/test_run_report.py`, all passing, isolated from the repo's real output directory.

Deliberately **not** wired into `run_final_pipeline.py`: the comparison report it needs comes from a separate script (`run_authoritative_comparison.py`) with a different scope and runtime — auto-wiring would silently pair a fresh schedule with a stale comparison report.

---

## Decisions for next session

Nothing below was decided unilaterally — each is a genuine fork this session surfaced but didn't resolve.

1. **The confidence-gated buffer's ADOPT decision** (`GA_AUDIT.md` §6) doesn't replicate under the corrected pipeline, whole-book or per-window. Revert to disabled, keep gated as a harmless no-op, or something else — this is a policy call, not a code fix to make unilaterally.
2. **The 100%-exposure, 1,627-neighbour `contends_with` leakage channel** is real and undocumented anywhere in `PROJECT_STATUS.txt` or `GA_AUDIT.md`. It didn't explain the AP-ceiling question, but it's the same class of gap Section 0F/0G already fixed for precedence edges — worth a scoping decision on whether to close it too.
3. **Two write-ups are now known to be stale** and should be corrected in the actual project record, not just in this session's chat: `DELAY_LABEL_FIX_PROPOSAL.md`'s "AP at or above ceiling" line, and Table 1's whole-book-only labour utilisation figure.
4. **None of this session's findings have been folded into `PROJECT_STATUS.txt` yet** — it's still the project's authoritative record, and right now it doesn't know about any of the six threads above.

---

*Source data for every number above: `outputs/authoritative_comparison_report.json`, `outputs/gated_buffer_ablation_multiseed.json`, and live re-measurements against the current checkpoint.*
