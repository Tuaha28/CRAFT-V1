# Full Pipeline Re-Run Report — Post-Fix Verification

**Date:** 2026-09-10
**Scope:** Fix and re-verify the three confirmed issues from `PIPELINE_METHODOLOGY_TRACE.md`, then re-run the entire pipeline end-to-end (train → evaluate → schedule → authoritative comparison) against the existing synthetic database — no reseeding, code-level fixes only.

---

## 1. The Three Fixes

### FIX 1 — Silent INNER JOIN drop in `database/queries.py`

**Problem:** `fetch_active_orders_and_routings()` used `JOIN Purchase_Orders`. Any active order without a matching `Purchase_Orders` row would be silently dropped from the query result with no warning — GA, GNN, and the dispatch simulation would simply never see it.

**Fix:**
```python
# database/queries.py
- JOIN Purchase_Orders ...
+ LEFT JOIN Purchase_Orders ...

df = pd.read_sql(query, get_engine())
missing_po = df.loc[df['order_deadline'].isna(), 'production_order_id'].unique()
if len(missing_po) > 0:
    logger.warning(f"{len(missing_po)} active order(s) have no matching Purchase_Orders row ...")
return df
```

**Verification:** Ran against the live DB — **0 of 1,029 active orders** are missing a `Purchase_Orders` match. No behavioral change today; this is a correctness/observability fix for the future, not a fix that altered today's order composition or any downstream number.

---

### FIX 2 — GNN evaluation silently re-derived its own val split

**Problem:** `models/evaluate.py` recomputed the train/val split itself via `FactoryGraphBuilder._build_split_masks` at eval time instead of loading the exact split used at training time. Since the graph is rebuilt from the *live* DB (which drifts after training as orders complete/change state), the "val set" scored at evaluation time was not guaranteed to be the same order set the model was actually held out on.

**Fix:**
- `graph/graph_builder.py` — exposed `self.order_ids_per_node` (per-node-index → order ID mapping) so it can be reused outside the builder.
- `models/train.py` — at the end of a training run, writes `outputs/val_order_ids.json`:
```python
val_order_ids = sorted({str(oid) for oid in builder.order_ids_per_node[val_mask.cpu().numpy()].tolist()})
val_ids_path = os.path.join(OUTPUT_DIR, "val_order_ids.json")
json.dump({
    "split_seed": SPLIT_SEED, "val_fraction": VAL_FRACTION,
    "num_val_orders": len(val_order_ids),
    "production_order_ids": val_order_ids,
}, f, indent=2)
```
- `models/evaluate.py` — new `_load_val_mask(builder, hetero_graph)`: loads `val_order_ids.json` and rebuilds the val mask from the **recorded order IDs**, matched against the current graph's `order_ids_per_node`. Falls back to re-deriving the split (with a printed + logged warning) only if the file is missing.

**Verification (today's run):**
```
Loaded 205 val order IDs from outputs/val_order_ids.json --
1744 operation nodes matched in the current graph
```
205 orders recorded at training time, all 205 still present in the live DB (order-count match — a mismatch there would have meant a val order left the DB entirely; only the *operation*-count-per-order is expected to drift with partial-completion state). Confirmed loaded, not re-derived.

---

### FIX 3 — GA population/generation config was dead code

**Problem:** `utils/config.py` defined `GA_POPULATION_SIZE` / `GA_GENERATIONS`, but `run_final_pipeline.py` and `agent/decision_engine.py::trigger_rescheduling()` both hardcoded `50, 50` locally — the config values were read from `.env` but never actually consulted by the two real call sites. `GA_MUTATION_RATE` was configured but `ga/scheduler.py` derives its mutation rate dynamically (`MUTATIONS_PER_INDIVIDUAL / num_ops`) and never reads the config value at all.

**Fix:**
- `utils/config.py` — `GA_GENERATIONS` default `100 → 50` (matches actual usage); `GA_MUTATION_RATE` deleted, replaced with a comment pointing to the dynamic derivation in `ga/scheduler.py`.
- `.env` / `.env.example` — same changes (the `.env` override was masking the config.py default change until fixed there too — same pattern already documented in `.env`'s own comments for `GNN_EPOCHS`).
- `run_final_pipeline.py` — `POP, GENS = GA_POPULATION_SIZE, GA_GENERATIONS` (was hardcoded `50, 50`).
- `agent/decision_engine.py` — `trigger_rescheduling()` now imports and uses `GA_POPULATION_SIZE` / `GA_GENERATIONS` (was hardcoded).

**Verification:** Effective value now genuinely sourced from config (confirmed `GA_GENERATIONS=50` takes effect after both the code and `.env` changes). Numerically a no-op for these two call sites since the hardcoded value already matched (50/50) — this fix is about establishing config as the actual source of truth, not about changing today's run's numbers. **Note:** `run_authoritative_comparison.py` was explicitly out of scope for this fix and still sets its own GA params independently (see §6 — this is *why* its results are unaffected by Fix 3).

---

## 2. GNN Retraining (from scratch, 150 epochs)

Training was run against the unmodified synthetic database (no reseed). Across the session this took 4 attempts — the first 3 were killed by Windows OOM protection (system had only 5.8–6.3GB free out of 15.6GB even at idle, with Chrome/VS Code/WhatsApp running); the training process's own per-epoch RSS stayed flat at 330–840MB every attempt, confirming this was external memory pressure, not a leak. After closing Chrome, VS Code, and WhatsApp (freeing memory to ~8.8GB), the 4th attempt completed all 150 epochs cleanly.

- **Val split:** order-level 80/20, `SPLIT_SEED=42`, `VAL_FRACTION=0.2` — 205 orders held out (1,744 of 8,745 operation nodes), 7,001 operation nodes for training.
- **`val_order_ids.json` written:** confirmed present, 205 `production_order_ids`, `split_seed: 42`, `val_fraction: 0.2`.
- Checkpoint promoted to `outputs/checkpoints/gnn_multitask_latest.pth`.

---

## 3. GNN Evaluation (`models/evaluate.py`)

Confirmed **loaded** (not re-derived) the recorded val split — log line: `Loaded 205 val order IDs from outputs/val_order_ids.json -- 1744 operation nodes matched`.

| Metric | Value |
|---|---|
| delay_accuracy | 0.8882 |
| delay_precision | 0.0000 |
| delay_recall | 0.0000 |
| delay_f1 | 0.0000 |
| completion_mae_minutes | 257.6018 |
| completion_rmse_minutes | 416.3391 |
| priority_mae | 0.0571 |
| positive_rate | 0.1118 |
| average_precision | 0.1723 |
| roc_auc | 0.5579 |
| n_samples | 1744 |

**Reading these honestly:** the delay head predicts **zero positives** at its default threshold (precision/recall/F1 all 0.0000) despite 88.8% raw accuracy — a direct consequence of the 11.2% positive rate (`positive_rate: 0.1118`): a trivial always-negative classifier already scores ~88.8% accuracy, which is exactly what's happening here. `average_precision` (0.1723, barely above the 0.1118 no-skill baseline) and `roc_auc` (0.5579, barely above the 0.5 no-skill line) show the model has only weak ranking signal for delay risk — this is the honest state of the delay head and should be reported as such in the paper, not obscured by the accuracy figure alone.

---

## 4. GA Scheduling — Full Pipeline Run (`run_final_pipeline.py::do_schedule()`)

Corrected config in effect (`GA_POPULATION_SIZE=50`, `GA_GENERATIONS=50`, sourced from `utils/config.py`, no local hardcoding).

### Whole-book aggregate (1,029 orders, 4 rolling-horizon windows)

| Metric | Value |
|---|---|
| Orders | 1,029 |
| On-time % | 46.55% |
| Orders late | 550 |
| Mean tardiness (late orders) | 82.06 h |
| Max tardiness | 359.96 h |
| Makespan | 311.25 days |
| Throughput | 3.31 orders/day |
| Labour utilisation | 0.5355% |
| Local search gain (mean across windows) | +0.137% |

### Per-window (best-schedule margin over best-of-5 pure heuristics, GA polish runtime)

| Window | On-time % | GA margin | Polish runtime |
|---|---|---|---|
| 1 (dense) | 5.3% | +6.3% | 575 s |
| 2 (dense) | 25.7% | +4.8% | 331 s |
| 3 (slack) | 87.3% | +0.9% | 276 s |
| 4 (slack) | 96.1% | +0.1% | 96 s |

### Window 1 labour breakdown (exported window summary)

- Labour utilisation: 5.81% (vs. 69.61% ceiling → 8.35% of ceiling used); binding resource: **machines**
- Present operators: 4,004; machines held in standby reserve: 213
- Operator sourcing: batch 1,739 / factory 795 / anywhere 7 / unfilled 0
- Labour minutes by shift: Morning 620,366 / Evening 380,894 / Night 791,135
- Labour minutes by sourcing tier: batch 1,205,274 / factory 582,346 / anywhere 4,775
- On-time rate: 5.33% (284 of 300 orders-with-deadline breaching), mean tardiness of late orders 58.2h, max 128.4h

---

## 5. Window-1 Fitness Component Breakdown

Diagnostic script reconstructed each of the 7 weighted terms in the scalarized fitness function (`ga/fitness.py`) for window 1's polished chromosome, without modifying the fitness module itself — cross-validated by confirming the reconstructed total exactly matches the real returned `fitness_score`.

- GA best fitness (before polish): **807.31**
- After local search polish: **804.67** (+0.33% improvement; 148 evaluations, accepted `{swap: 6, line: 4, machines: 1}`, rejected 136)

| # | Term | Weight | Value |
|---|---|---|---|
| 1 | Makespan | ALPHA = 1.0 | 127.2501 |
| 2 | Delay-risk penalty | GAMMA = 1.0 | 5.2328 |
| 3 | Priority reward (subtracted) | DELTA = 1.0 | −36.0291 |
| 4 | Load-balance penalty | EPSILON = 1.0 | 78.5097 |
| 5 | Deadline lateness | ZETA = 3.0 | 167.4336 |
| 6 | On-time-rate penalty | SERVICE_LEVEL_WEIGHT = 24.0 | 22.7200 |
| 7 | Idle-labour penalty | IDLE_LABOR_WEIGHT = 480.0 | **439.5545** |
| | **Reconstructed total** | | **804.6717** |
| | **Actual `fitness_score`** | | **804.6717** ✓ exact match |

**Idle-labour penalty dominates at 54.6% of total fitness** — consistent with window 1 being the most resource-starved window (5.3% on-time, only 8.4% of the labour ceiling actually used, machines the binding resource). This is the single largest lever in the objective for dense windows.

---

## 6. Authoritative GA-vs-Baselines Comparison (`run_authoritative_comparison.py`)

Run against the frozen snapshot `authoritative_20260827_fixed` (origin `2026-08-27T19:47:13`) — same 1,029 orders, same GNN checkpoint, same seed (42), so today's numbers are directly comparable to all prior authoritative runs.

### Per-window table

**Window 1 (dense)**
| strategy | on-time% | cum makespan (d) | throughput/d | mean tard (h) | labour% |
|---|---|---|---|---|---|
| **GA** | **10.0%** | 5.30 | 56.58 | 57.63 | 5.9% |
| FIFO | 9.7% | 8.86 | 33.86 | 76.54 | 3.5% |
| EDF | 8.0% | 8.22 | 36.49 | 62.04 | 3.8% |
| CriticalRatio | 8.7% | 9.94 | 30.17 | 44.38 | 3.1% |

GA margin over best of 5 pure heuristics: **+9.88%** (vs. RandomRepaired)

**Window 2 (dense)**
| strategy | on-time% | cum makespan (d) | throughput/d | mean tard (h) | labour% | floor_ratio |
|---|---|---|---|---|---|---|
| **GA** | **45.3%** | 15.73 | 28.77 | 89.03 | 4.4% | **3.24** |
| FIFO | 40.0% | 24.30 | 19.43 | 196.78 | 3.0% | 5.01 |
| EDF | 43.0% | 25.80 | 17.06 | 151.36 | 2.6% | 5.32 |
| CriticalRatio | 35.3% | 24.32 | 20.87 | 166.66 | 3.2% | 5.01 |

GA margin over best of 5 pure heuristics: **+6.65%** (vs. CriticalRatio). GA is meaningfully closer to the release-date-aware capacity floor (3.24x) than any baseline (5.01–5.32x).

**Window 3 (slack)**
| strategy | on-time% | cum makespan (d) | throughput/d | mean tard (h) | floor_ratio |
|---|---|---|---|---|---|
| GA | 90.0% | 116.16 | 2.99 | 41.03 | 1.04 |
| FIFO | 30.0% | 115.63 | 3.28 | 1100.58 | 1.03 |
| **EDF** | **93.7%** | 116.27 | 3.32 | 69.52 | 1.04 |
| CriticalRatio | 46.0% | 118.13 | 3.20 | 607.93 | 1.06 |

GA margin over best of 5: +0.34% (vs. EDF) — GA loses to EDF on raw on-time% here (90.0% vs 93.7%), a finding that has now reproduced across every authoritative run to date; it looks like a genuine property of this window's landscape, not a bug artifact.

**Window 4 (slack)**
| strategy | on-time% | cum makespan (d) | throughput/d | mean tard (h) | floor_ratio |
|---|---|---|---|---|---|
| GA | 96.1% | 324.41 | 0.62 | 22.63 | 1.00 |
| FIFO | 36.4% | 328.07 | 0.61 | 2327.45 | 1.01 |
| EDF | 96.1% | 324.41 | 0.62 | 22.63 | 1.00 |
| CriticalRatio | 58.1% | 324.38 | 0.63 | 1404.84 | 1.00 |

GA and EDF tied exactly (96.1% on-time, 22.63h mean tardiness). GA margin: +0.14% vs EDF.

### Whole-book aggregate (footnote only — never the headline metric)

| strategy | on-time% | makespan (d) | throughput/d | mean tard (h) | labour% |
|---|---|---|---|---|---|
| GA | 54.4% | 324.41 | 3.17 | 67.17 | 0.5% |
| FIFO | 27.8% | 328.07 | 3.14 | 643.52 | 0.5% |
| EDF | 54.2% | 324.41 | 3.17 | 94.35 | 0.5% |
| CriticalRatio | 33.5% | 324.38 | 3.17 | 319.94 | 0.5% |

Whole-book release-date-aware capacity floor: **324.33 days**. GA/EDF/CriticalRatio ≈1.00x; FIFO 1.01x — makespan is floor-bound (driven by the latest-released order's critical path) for every strategy at whole-book scale.

---

## 7. Step 5 — Comparison Against the Historical Baseline

The historical reference point (memory: `authoritative-comparison-20260827`) is the **2026-08-27** authoritative run — the true final result at that time, run with `RISK_BUFFER_AT_CERTAINTY=0.5`:

| window | Aug-27 GA on-time | Aug-27 GA margin |
|---|---|---|
| 1 | 10.0% | +9.16% (vs RandomRepaired) |
| 2 | 45.7% | +5.32% (vs CriticalRatio) |
| 3 | 90.0% | +0.26% (vs EDF) |
| 4 | 96.1% | +0.14% (vs EDF) |

Whole-book: 54.5% on-time, 324.41d makespan.

**Today's numbers differ slightly** (window 1: +9.16% → +9.88%; window 2: 45.7%→45.3% on-time, +5.32%→+6.65% margin; window 3: +0.26%→+0.34%; window 4: unchanged; whole-book: 54.5%→54.4%, makespan unchanged at 324.41d). **Root cause, verified directly rather than assumed:** I compared today's fresh output byte-for-byte against `outputs/authoritative_comparison_report_PRE_FIX_BACKUP.json` — a backup taken on 2026-09-01, *before* any of today's three fixes were applied. Every aggregate and per-window field (on-time%, makespan, mean tardiness, labour%, floor_ratio) in today's run is **bit-for-bit identical** to that Sep-1 backup, out to 13+ decimal places.

This proves two things directly, not by inference:
1. **Today's three fixes had zero effect on the authoritative comparison's results.** This is expected: Fix 1 (LEFT JOIN) affects 0 of 1,029 orders (verified in §1); Fix 2 (`val_order_ids.json`) only touches `models/evaluate.py`, not GA scheduling; Fix 3 (config wiring) only touches `run_final_pipeline.py` and `agent/decision_engine.py` — `run_authoritative_comparison.py` was explicitly out of scope for that fix and still sets its own GA population/generation parameters independently (confirmed by its log: `50 pop size, 50 generations`, matching both the Sep-1 and today's runs exactly).
2. **The entire deviation from the Aug-27 baseline predates this session's work.** The Sep-1 backup already reflects the `RISK_BUFFER_AT_CERTAINTY` change from 0.5 → 0.0, made as a separate, unrelated decision between Aug 27 and Sep 1. That confound — not anything fixed today — accounts for the small margin shifts above.

**Qualitative story is fully preserved** and is the one safe to write in the paper: GA's edge concentrates in the dense windows (+9.88%/+6.65% today vs. +9.16%/+5.32% at the Aug-27 baseline — same story, tighter margins), converges to baseline-equivalent performance in slack windows, loses to EDF specifically on window-3 raw on-time% (a reproducible, non-bug property of that window), and FIFO remains the clear loser everywhere, especially on tardiness. No large or concerning deviation was found.

---

## 8. Techniques-Considered Summary (for the Methodology/Related-Work section)

| Technique | Source doc | Status | Reason |
|---|---|---|---|
| **Generational GA (OX1 + 2-pt swap, elitism, random immigrants)** | `ga/scheduler.py` | **Adopted — production** | Current system; scalarized weighted-sum fitness, order-level chromosome |
| CP-SAT (top-3-by-pool-size candidate cap) | `CP_SAT_PROPOSAL.md` §8–10 | **CLOSED** (narrow claim) | Could not find a feasible solution even warm-started with the GA's own schedule; not a general CP-SAT verdict — the specific candidate-cap formulation tested was infeasible |
| Sequential ALNS (destroy/repair) | `ALNS_REFINEMENT_PROPOSAL.md` §9–10 | **HOLD** | A recalibrated version (fixing an earlier bug) converged to the *same* answer as the originally-broken version — no measurable improvement over plain GA+polish |
| ALNS repair-scoring | `ALNS_REPAIR_SCORING_PROPOSAL.md` | **HOLD** | Corrected formula does genuinely differentiate candidates (28.7% of cases overall, 85% in window 2) but the differentiation **regresses** results by −0.94% to −1.36% vs. plain ALNS in validation — not adopted, code kept opt-in only |
| Island-model / memetic GA | `GA_AGENT_POSTMORTEM.md` §2 | **Measured, not adopted** | Marginal gain (+0.4%/+0.9%) vs. the simpler local-search polish's own +0.615% — not worth the added complexity |
| Domain-aware crossover (TCOX) | `DOMAIN_AWARE_CROSSOVER_PROPOSAL.md` §11 | **HOLD** | 1 win / 1 regression / 1 wash across 3 test cases — no consistent signal |

All six refinement mechanisms above are consolidated in `GA_AGENT_POSTMORTEM.md` §13's master table: **GA-refinement work is fully closed — zero of six evaluated mechanisms were adopted.** The production system is deliberately the plain generational GA + local-search polish, confidence-gated buffer on, everything else off — every more elaborate alternative that was tried either regressed, tied, or failed to find a feasible solution at all.

---

## Summary

All three confirmed pipeline bugs are fixed and independently verified. The full pipeline was re-run end-to-end from scratch (retraining included) against the unchanged synthetic database. The GNN's delay-classification head has a real, honestly-reported weakness (zero-positive predictions at default threshold, weak ranking signal) that should be stated plainly in the paper rather than masked by its 88.8% raw accuracy. The GA scheduling and authoritative comparison results are consistent with all prior findings — the fixes did not materially change any headline number, and the one comparison that did shift slightly (today's authoritative run vs. the Aug-27 baseline) was directly traced to a pre-existing, unrelated confound (the risk-buffer change) rather than to anything done in this session. GA's real, defensible edge remains in dense windows (+9.88%/+6.65% margin over the best heuristic baseline), converging to baseline-equivalent performance under slack, with the GA-refinement literature (CP-SAT, ALNS, island-model, domain-aware crossover) now fully and rigorously closed as a negative-result section for the paper.
