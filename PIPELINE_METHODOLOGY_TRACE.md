# Full Pipeline Trace — Methodology-Section Audit (2026-09-08)

Same discipline as `GA_AUDIT.md`/`DATA_AUDIT.md`/`FULL_CODEBASE_AUDIT.md`: every
claim below is sourced from a direct read of the file and line cited in this
session, not from memory, and no code was changed while producing this
document. Intended for direct use in the Methodology section of the
accompanying FYP paper — each stage answers what happens, which specific
technique/algorithm is used (named precisely, not vaguely), the exact
inputs/outputs, and file:line references, then closes with a short bullet
list meant to be lifted directly into the paper.

Where the actual implementation is simpler than a name in the project might
suggest (e.g. "fuzzy logic," "explainability engine," GA hyperparameters
implied by `utils/config.py`), that is flagged explicitly and is not softened
for presentation — this document is written for accuracy, not to flatter the
project.

**A note on row counts:** this environment has no live database connection.
Any table row count cited below is quoted from a specific prior in-code
comment (with its source named), not re-verified against a live query — the
live counts move as the database changes and should be re-measured before
being quoted as current in the paper.

---

## STAGE 1 — Real company data

**(a) What happens:** Four real spreadsheets are read at the top of
`data_generation/seed_database_fixed.py::execute_complete_seed()` (lines
284–301), *before* the database is touched (deliberately ordered this way —
line 267-278's comment explains a prior incident where a missing file wiped
an already-seeded DB because the truncate ran first). Each file feeds
specific, named statistics into the synthetic generator; nothing is used
verbatim as a live table — everything is read, aggregated into a
distribution/constant, and then used to *parameterize* synthetic generation.

**(b) Technique:** Empirical calibration from real data via `pandas`
aggregation (groupby-mean, median, value_counts) — not a formal statistical
estimator, a direct measured statistic used as a generator parameter. The
project's own documentation (`DATA_AUDIT.md`, referenced throughout the
seeder's comments) classifies every such quantity into one of three buckets,
and the seeder's comments cite the bucket for each: **(a)** a real value used
directly, **(b)** a principled derivation from real values (no direct real
source for that exact quantity), **(c)** a deliberate synthetic choice with
no real source at all, documented as such.

**(c) Inputs → outputs, by file:**

| File | Real columns used | What they calibrate | Bucket |
|---|---|---|---|
| `grid.xls` | `StyleId`, `GarmentType`, `PO`, `Color`, `BuyMonth`, `StartDate`, `EndDate`, `ExFtyDate`, `PlanTarget` | Garment-type frequency distribution (`category_weights`, line 329-330); real order quantity distribution (median ~330, range 52–3,300 units, from 139 PO-grouped rows, lines 384–392); total synthetic order count `TOTAL_ORDERS` (derived by grouping the 93.5% of rows with no real `PO` by `StyleId+Color+BuyMonth`, lines 418–420, justified by a cross-check at lines 405–417); order lead-time/end-gap day distributions (lines 470-473) | (a) direct / (b) derived count |
| `Plan.xlsx` | `GarmentType`, `GarmentSmv`, `Efficency` | Per-garment-type total SMV (`smv_by_type`, lines 340-353, with a documented fallback chain to routing-group average then dataset-wide mean for 4 categories absent from `Plan.xlsx`); real worker-efficiency value distribution (mean 77.8%, std 5.6%, n=1,382 — sampled from, not joined per-line, because the `LineId` join key was verified unreliable: lines 355-374) | (a) |
| `PPC-1.xlsx` | `Output`, `WorkHrs` | The duration-calibration target: median real minutes-per-unit (2.51, n=4,052 valid rows, lines 612-623, 634-635) | (a), with a documented interpretation caveat (`Output` is ambiguous between a raw count and a percentage; the seeder picked the literal-count reading and says so explicitly) |
| `WorkStation.xlsx` | `Department`, `ID`, `LineName`, `Worker Capacity`, `LayoutType`, `LineType`, `IsEnabled` | Factory/department structure (line 481); per-line crew capacity — replaces an invented `random.choice([32,48,64])` (lines 494-503, 551-552); `layout_type`/`line_type` stored as real data but explicitly **not** wired into any routing logic after a cross-check found no discriminating signal (lines 505-515) | (a), with one explicitly-rejected hypothesis |

**(d) File/lines:** `data_generation/seed_database_fixed.py:284-301` (file
loading), `:318-425` (grid.xls), `:340-374` (Plan.xlsx), `:605-657` (PPC-1.xlsx
calibration), `:481-552` (WorkStation.xlsx).

**Techniques used in this stage:**
- Empirical calibration of synthetic-generator parameters from real spreadsheet data (pandas groupby/median/value_counts), not a formal estimator
- Explicit real/derived/synthetic provenance tagging per quantity (the project's own "bucket a/b/c" convention)
- No machine learning or statistical inference at this stage — pure descriptive statistics feeding a simulator

---

## STAGE 2 — Synthetic data generation

**Order provenance:** 139 genuine PO groups plus 890 proxy groups calibrate
the count of 1,029 synthetic records. Attributes are sampled independently;
original source order tuples are not preserved.

**(a) Step by step**, one lot-operation dispatch:
1. Machines are held in per-type min-heaps keyed by `(free_time, asset_id)` (`seed_database_fixed.py:1239-1243`).
2. For each order → each of its `n_lots` → each operation in its routing chain: pop the earliest-free machine of the required type (`heapq.heappop`, line 1315).
3. Pick an operator via a 3-tier fallback (same-batch → same-factory → company-wide, lines 1339-1348), filtered to who is actually on shift at the provisional start time (lines 1368-1383).
4. Compute duration: `duration_mins = DURATION_CALIBRATION_FACTOR * (stage_qty * sam) / efficiency`, clamped to [30, 480] minutes (lines 1394-1395).
5. Decide the start time: `earliest_possible = max(machine_free_time[asset], employee_free_time[emp], prev_end)` (lines 1397-1401); `start_t = get_next_valid_shift_start(earliest_possible)`, a no-op under the current 24h model (line 1409).
6. Roll for a new breakdown (line 1433); if it fires, fold repair time into the planned end and mark the machine `Breakdown`. Independently look up whether this lot's employee is on leave (line 1459).
7. Compute synthetic "actual" timing by multiplying productive work duration by `sample_execution_variance_multiplier(...)` and adding the same sampled repair time (lines 1478-1485).
8. Write one row to `Job_Shop_Schedules` carrying both timelines plus the causal-context columns (lines 1530-1535).
9. Advance `machine_free_time`, `employee_free_time`, `prev_end`, and the heap using **planned `end_t`**, not `actual_end_t` (lines 1487-1491). Actual overruns do not cascade; actual timestamps are not a fully resource-constrained execution timeline.

**(b) Technique, named precisely:** This is a **discrete-event simulation**
using **priority-queue (min-heap) greedy earliest-available-resource
dispatch** — every machine-type pool is a `heapq` ordered by next-free time,
and the "next event" is always "pop the soonest-free machine, assign it,
push it back at its new free time." This is the classical machine/job-shop
simulation pattern (equivalent to a list-scheduling / greedy dispatch rule),
**not** a GA, **not** a constraint solver, and **not** the same code as the
live GA scheduler (`ga/`) — it is an entirely separate, one-time generator
that produces the historical "ground truth" the GNN later learns from.

**(c) Inputs → outputs:**
- Inputs: routing chains from `data_generation/routing_tables.py` (garment-type → `[(operation_name, machine_type)]`), calibration constants derived in Stage 1, `random`/`heapq` state.
- Outputs: rows in `Job_Shop_Schedules` with columns `production_order_id, line_id, asset_id, operation_id, employee_id, batch_id, planned_start_time, planned_end_time, execution_status, actual_start_time, actual_end_time, machine_status_at_dispatch, employee_on_leave_at_dispatch, queue_delay_mins_at_dispatch, machine_breakdown_count_at_dispatch` (insert statement at `seed_database_fixed.py:1587`).

**Breakdown injection, exact model** (lines 1429-1449): a machine accumulates
`machine_runtime_tracker[asset_id] += duration_mins` every dispatch; once
that exceeds 1200 minutes, each subsequent dispatch on it has a flat **15%
probability** (`random.random() < 0.15`) of triggering a breakdown, at which
point `machine_runtime_tracker` resets to 0, repair time is drawn uniformly
from `random.randint(120, 360)` minutes, and the repair time is added
directly into that operation's *planned* end time. This is a simple
**threshold-then-Bernoulli-trial** model, not a hazard-rate/reliability
distribution (e.g. no Weibull/exponential failure-time model is used).

**Execution-variance multiplier, exact formula** (`seed_database_fixed.py:151-208`):
```
breakdown_effect = min(0.08, machine_breakdown_count_at_dispatch * 0.01)   # BREAKDOWN_EFFECT_CAP, _PER_COUNT
leave_effect      = 0.10 if on_leave else 0.0                              # LEAVE_EFFECT
effective_mean    = 1.0 + breakdown_effect + leave_effect                  # EXECUTION_VARIANCE_MEAN
queue_sd_mult     = 1.0 + min(0.5, queue_delay_mins / 10000.0 * 0.5)        # QUEUE_SD_WIDEN_CAP/SCALE
effective_sd      = 0.15 * queue_sd_mult                                   # EXECUTION_VARIANCE_SD
multiplier        = clip(gauss(effective_mean, effective_sd), 0.65, 1.6)   # EXECUTION_VARIANCE_MIN/MAX
```
Three distinct causal factors, each shifting a **clipped Gaussian's**
parameters, never assigning an outcome directly: prior breakdown count and
leave status shift the **mean** (additively, capped); queue delay widens the
**SD** only (modeling increased unpredictability under congestion, not a
directional slowdown — the docstring at lines 179-186 is explicit this is a
deliberate distinction, not an oversight).

**(d) File/lines:** `data_generation/seed_database_fixed.py:1219-1610` (whole
loop), `:151-208` (variance multiplier), `:1394-1395` (duration formula),
`:1397-1409` (start-time rule), `:1429-1449` (breakdown injection).

**Techniques used in this stage:**
- Discrete-event simulation with priority-queue (min-heap) greedy earliest-available-resource dispatch
- Threshold-triggered Bernoulli-trial breakdown injection (not a survival/hazard model)
- Clipped-Gaussian multiplicative execution-variance model with three additive/multiplicative causal shift factors

---

## STAGE 3 — Database schema (tables actually read by the training/inference pipeline)

**(a)/(c)** Every table below is reached via a `database/queries.py` function
that `graph/graph_builder.py::FactoryGraphBuilder` calls. Row counts are
**not independently verifiable right now** — this environment has no live DB
connection (the codebase's own `agent/impact_assessor.py` docstring says the
same thing about its testing) — so the counts below are cited from specific
prior measurements recorded in the code's own comments, with their source,
not re-verified live.

| Table | Role in the pipeline | Cited row count (source) |
|---|---|---|
| `Machinery_Assets` | Machine nodes (`fetch_machine_state`) | ~3,000 (seeder hardcodes `range(1,3001)`, `seed_database_fixed.py:1228`) |
| `Machine_Capabilities` | Machine `operation_type` (joined into machine fetch) | — |
| `Employees` | Worker identity (`fetch_worker_state`) | ~15,066 cited in `graph/graph_builder.py`'s own historical note (not re-verified) |
| `Work_Schedules` | Floater shift/leave state | — |
| `Operator_Skill_Matrix` | Worker skill/efficiency | — |
| `Batch_Members` / `Employee_Batches` | Crew rotation membership (`fetch_assignable_operators`, `fetch_concurrent_operator_capacity`) | — |
| `Production_Orders` | Order status/quantity/dates (`fetch_active_orders_and_routings`) | cited "1,410 active orders" / "1,029" at different points in the project's history (`GA_AGENT_POSTMORTEM.md:32` cites 578 of 1,029) — **this number moves as the live DB changes**, not a fixed constant |
| `Purchase_Orders` | Customer `delivery_deadline` (inner-joined into `fetch_active_orders_and_routings`, `database/queries.py:127`) | — |
| `Routing_Operations` | Per-product operation sequence (`fetch_active_orders_and_routings`) | ~7,050–8,745 operations cited at different points (`GA_AUDIT.md:15` cites "Operation: 8,745"; `SESSION_HANDOFF.md` cites "7,050") — **numbers genuinely differ across sessions because the underlying seed and live DB state changed between them**, flagged rather than reconciled |
| `Job_Shop_Schedules` | Planned/actual timing + dispatch-context (`fetch_current_shop_schedule`, `fetch_operation_assignments`, `fetch_actual_vs_planned_timing`, `fetch_dispatch_context`) | — |
| `Production_Lines` / `Departments` | Line/factory structure (`fetch_line_factory_map`, `_fetch_production_lines`) | 99 lines cited in several comments (e.g. `ga/chromosome.py:143`) |
| `Inventory_Stock` / `Raw_Materials` | RAG stock-agent feed (`fetch_inventory_state`) — **not consumed by the GNN/GA pipeline at all**, only by the separate, unimplemented RAG agent | — |
| `Optimization_Metrics_Log` | Write-only — GA run metadata logged post-hoc (`agent/decision_engine.py::_log_optimization_metrics`) | — |

**(b) Technique:** Plain SQL `SELECT`/`JOIN` via SQLAlchemy + `pandas.read_sql`
— no ORM model layer, no caching layer.

**(d)** `database/queries.py:9-564` (every fetch function); call sites
`graph/graph_builder.py:75-91`.

**Techniques used in this stage:**
- Relational schema queried via raw SQL through SQLAlchemy/pandas (no ORM, no ETL framework)

---

## STAGE 4 — Graph construction

**(a)/(c)** `graph/graph_builder.py::FactoryGraphBuilder.build_graph()` (lines
94-186) assembles one `torch_geometric.data.HeteroData` object (confirmed:
`from torch_geometric.data import HeteroData`, `graph/graph_builder.py:3`;
instantiated at line 92, `self.data = HeteroData()`).

**Node types (3):** `machine`, `worker`, `operation` — each assigned via
`self.data[node_type].x = ...` (lines 99, 102, 107).

**Edge types (built, plus 2 dead/unused ones kept for reference):**

| Edge type (PyG relation triple) | Connects | Real-world relationship | Built at |
|---|---|---|---|
| `('operation','precedes','operation')` | Operation *i* → operation *i+1* within one order's routing | Routing-sequence precedence | `graph_builder.py:118-119`, built by `graph/edge_builder.py::build_precedence_edges` (lines 92-110) |
| `('operation','contends_with','operation')` | Every pair of operations sharing the same canonical `operation_type`, company-wide (undirected, both directions stored) | Resource contention for the same machine-type pool | `graph_builder.py:131-132`, built by `edge_builder.py::build_disjunctive_edges` (lines 112-213) |
| `('machine','operates','operation')` / reverse `('operation','runs_on','machine')` | A specific machine → the specific operations whose lots it actually ran (from real per-lot assignment data, not type-matching) | "This machine did this work" | `graph_builder.py:141-145,155`, built by `edge_builder.py::build_assignment_edges_into_operations` (lines 10-89) |
| `('worker','staffs','operation')` / reverse `('operation','staffed_by','worker')` | Same, for workers | "This worker did this work" | `graph_builder.py:141-146,156` |
| *(dead, not called)* `build_machine_eligibility_edges`, `build_worker_assignment_edges` (`edge_builder.py:216-353`) | Type-based bipartite operation↔machine / operation↔worker eligibility | Superseded — explicitly documented as no longer used because they pointed the wrong direction for message passing and connected each operation to hundreds of interchangeable nodes | Kept in the file, not called by `graph_builder.py` |

**(b) Technique:** Heterogeneous graph construction for PyTorch Geometric
(`HeteroData`), with edges built from real per-lot assignment data (not
synthetic type-matching) specifically so machine/worker state can propagate
via message passing — this design choice, and the direction fix, is
documented in `edge_builder.py:21-46` as a correctness fix (randomizing
machine/worker features previously changed delay-head output by exactly
0.0).

**(d)** `graph/graph_builder.py:72-186`, `graph/edge_builder.py:1-353`.

**Techniques used in this stage:**
- Heterogeneous graph construction (PyTorch Geometric `HeteroData`) with 3 node types and 6 built edge relations (including 2 reverse types)
- Assignment-derived (not type-matched) bipartite edges, specifically to avoid a documented message-passing dead end

---

## STAGE 5 — Feature engineering

All from `graph/node_features.py`, confirmed by full read.

**Machine features (5-dim, `build_machine_node_features`, lines 10-23):**
one-hot `current_status` over `['Active','Under_Maintenance','Idle','Breakdown']`
(4 dims) + `max_rpm / 5000.0` (fixed-scale normalization, not data-fitted),
`.fillna(0)`.

**Worker features (4-dim, `build_worker_node_features`, lines 25-76):**
- one-hot `status` over `['Present','Absent']` (2 dims)
- `efficiency_score / 100.0`, fallback `0.84` (the real population mean, not a placeholder — a documented bug fix, lines 42-64) if no skill-matrix evidence
- `skill_breadth / 5.0`, clipped to 1.0, fallback `0.2`

**Operation features (10 continuous + 1 embedding index, `build_operation_node_features`, lines 293-552):**

| Feature | Formula | Source |
|---|---|---|
| `sam_norm` | `sam / 10.0` | routing |
| `qty_norm` | `target_quantity / 15000.0` | order |
| `machine_parallelism_norm` | `all_lot_machine_count / GA_LOTS_PER_OPERATION` (fixed after this session's audit; previously a literal `60.0`), clip 1.0 | `fetch_operation_assignments`, leak-free (see below) |
| `deadline_budget_norm` | `(order_deadline − release_date) days / 60.0`, clip [0,3] | order |
| `decisive_lot_breakdown_count_norm` | decisive-lot (max `planned_end_time`) `machine_breakdown_count_at_dispatch / 5.0`, clip 3.0 | dispatch context |
| `mean_queue_delay_norm` | mean-across-lots `queue_delay_mins_at_dispatch / 200.0`, clip 3.0 | dispatch context |
| `routing_step_norm` / `steps_remaining_norm` | `operation_sequence_number / routing_length`, and its complement | routing |
| `pool_occupancy_norm` | count of other same-type operations sharing ≥1 physical machine (sparse matrix product `M @ M.T`), `/1000.0`, clip 3.0 | assignment data |
| `mean_breakdown_count_norm` | mean-across-lots breakdown count `/5.0`, clip 3.0 (reinstated alongside the decisive-lot version — the two heads need different aggregations, per `node_features.py:101-115`) | dispatch context |
| `type_idx` (not concatenated into the float tensor — a separate `torch.long` index) | `operation_type_index(machine_type_to_operation_type(required_machine_type))` | `utils/machine_taxonomy.py` |

**Leakage avoidance, explicitly engineered, not incidental:** `is_running`/
`is_delayed`/`lot_delay_frac` were removed (documented at lines 352-389)
after being measured as perfect predictors of the label — because both were
derived from the same `execution_status` column the *old* label used.
`machine_parallelism_norm` is deliberately sourced from
`fetch_operation_assignments()` (all-lots, unfiltered) rather than the
Done-filtered `fetch_current_shop_schedule()`, because the filtered version
was measured at 99.4% single-threshold "accuracy" purely from its `WHERE`
clause (lines 373-389).

**(b) Technique:** Hand-engineered, domain-derived tabular features with
fixed-scale (not data-fitted/z-score) normalization throughout — no learned
feature embeddings except the one explicit `operation_type` embedding table
(Stage 8), no autoencoder/PCA/feature-learning step.

**(d)** `graph/node_features.py:10-552`.

**Techniques used in this stage:**
- Hand-engineered tabular features per node type, fixed-scale normalization (not standardization/min-max fitted to the data)
- Explicit, measured leakage elimination (features re-sourced from an unfiltered assignment table after a filtered one was proven to leak the label)
- One learned categorical embedding (operation type), everything else continuous

---

## STAGE 6 — Label extraction

All from `graph/labels.py`, confirmed by full read.

**Head 1 — Delay (binary):** `_aggregate_actual_vs_planned` (lines 14-81):
per operation, `lateness_mins = max(actual_end_time) − max(planned_end_time)`
across its lots; label = `1.0 if lateness_mins > 0.20 * mean_single_lot_planned_duration
else 0.0` (`DELAY_LABEL_LATENESS_FRACTION = 0.20`, line 11). Denominator is
the mean **single-lot** duration (~30-70 min), not the multi-lot span — a
documented fix for a prior dilution bug.

**Head 2 — Completion (continuous, minutes):** `_aggregate_planned_span`
(lines 84-124): `duration = max(planned_end_time) − min(planned_start_time)`
across **all** lots, unfiltered; label `= max(1.0, duration)`, or `sam * 50`
if no planned span exists at all (`labels.py:271`).

**Head 3 — Priority (continuous, [0,1]):**
1. `_slack_days_for` (lines 127-164): raw slack = `(order_deadline − reference).days`, where `reference` is the operation's own `planned_start_time` if available, else the order's `release_date`. Returns `None` if no deadline exists.
2. `_percentile_rank_priority` (lines 167-209): for the operation set with valid slack, exact formula:
   ```
   ranks = rank(slack_values, method='average')      # 1..n
   percentile = (ranks - 1) / (n - 1)                 # true 0..1 span
   priority = 1.0 - percentile                        # least slack -> priority near 1.0
   ```
   Operations with no valid slack, or fewer than 2 distinct slack values in
   the set, default to `0.5` (neutral). This is a **within-batch percentile
   rank**, not a fixed day-scale threshold — it explicitly replaced an
   earlier `urgency_score()`-based version (`agent/fuzzy_logic.py`) that was
   measured to collapse 97.8% of this dataset to an identical 0.0 (lines
   147-157).

**(b) Technique:** Rule-based label construction directly from the seeder's
synthetic ground truth (Stage 2) — thresholded lateness ratio (delay),
aggregated span (completion), and **percentile-rank normalization**
(priority) — not model-derived, not GA-derived (none of the three heads'
labels touch GA output anywhere).

**(d)** `graph/labels.py:1-292`.

**Techniques used in this stage:**
- Threshold-on-ratio binary labeling (delay)
- Direct span aggregation (completion)
- Within-batch percentile-rank normalization, not a fixed-threshold scale (priority) — an explicit, measured design correction over an earlier fixed-threshold approach

---

## STAGE 7 — Train/val split

Order-level, random, 80/20, `seed=42` — confirmed identical to the prior
audit, no code changed since. `graph/graph_builder.py:24-25`
(`SPLIT_SEED = 42`, `VAL_FRACTION = 0.2`), `_build_split_masks` (lines
28-70): `np.unique(order_ids)` → `rng.permutation` (NumPy `default_rng(42)`)
→ bottom 20% of the permuted unique order list → val; every operation of a
val order gets `val_mask=True`.

**Where it feeds into backprop:** `models/train.py:36-37` reads
`train_mask`/`val_mask` off the graph object; `compute_losses(out, train_mask)`
(line 92, called at line 122) is the **only** loss that receives
`.backward()` (line 124) and `optimizer.step()` (line 125). The validation
pass (lines 127-131) runs under `model.eval()` + `with torch.no_grad():` — it
never contributes a gradient; it only feeds the LR scheduler
(`scheduler.step(val_total_loss.item())`, line 134) and the checkpoint-save
decision (lines 136-138).

**Techniques used in this stage:**
- Group-level (order-level, not node-level) random holdout split, fixed seed for reproducibility — explicitly chosen over a naive per-node split to avoid precedence-edge leakage across the boundary

---

## STAGE 8 — GNN architecture and training

**(a)/(b) Architecture** (`models/gnn.py:33-271`): **split-encoder** design —
two separate projection/graph-encoder branches with one shared trainable
`op_type_embedding`, one optimizer, one LR scheduler, and combined-loss
checkpoint selection. They are not fully parameter-isolated:
- `SharedGraphEncoder` (lines 33-54): exactly **3 layers of
  `torch_geometric.nn.SAGEConv`** (confirmed — `from torch_geometric.nn
  import SAGEConv, to_hetero`, line 3), `SAGEConv((-1,-1), hidden_channels)`
  for layer 1 (lazy input dim) then `SAGEConv((hidden,hidden), hidden)` for
  layers 2-3. ReLU + Dropout after layers 1 and 2; **no activation after
  layer 3** (raw embedding passed to the head).
- Each branch wraps its `SharedGraphEncoder` in `to_hetero(base_encoder,
  metadata, aggr='sum')` (line 179) — PyG's heterogeneous-GNN transform,
  aggregator = **sum**, not mean/max/attention.
- Branch 1 (`encoder_delay`): feeds only the delay head. Branch 2
  (`encoder_cp`): feeds completion + priority, sharing one encoder between
  those two tasks only. This split was adopted 2026-08-23 after a controlled
  two-seed experiment (documented `gnn.py:58-79`), not assumed.
- `hidden_channels`: `GNN_HIDDEN_CHANNELS` = **64** default
  (`utils/config.py:64`). `dropout`: `GNN_DROPOUT` = **0.3** default
  (`config.py:80`).
- Operation input: 10 continuous features (Stage 5) + a 4-dim learned
  `nn.Embedding(NUM_OPERATION_TYPES, 4)` for operation type (lines 89, 170),
  concatenated before the per-node-type linear projection (`_project`,
  lines 228-247).
- **Heads** (lines 190-226): each is `Linear(hidden,32) → ReLU → Dropout →
  Linear(32,1)`, then a task-specific final activation — **none** for delay
  (raw logits, for `BCEWithLogitsLoss` numerical stability), **Softplus**
  for completion (chosen over ReLU after a documented dead-unit-collapse
  measurement, lines 199-210), **Sigmoid** for priority.

**Losses per head, exact:**
- Delay: `nn.BCEWithLogitsLoss()` (`train.py:49`)
- Completion: `nn.HuberLoss(delta=0.4)` (`train.py:67`), computed on both
  prediction and label **rescaled by `COMPLETION_SCALE_MINUTES=1000.0`**
  (`train.py:109-112`) — `delta=0.4` was itself derived from a measured
  residual distribution (mean 0.884, std 0.687 rescaled), not the PyTorch
  default of 1.0 (`train.py:50-66`)
- Priority: `nn.MSELoss()` (`train.py:68`)

**Multi-task combination:** `total = loss_delay + loss_completion +
loss_priority` — **equal, unweighted sum** ("parity", `train.py:114`), **not**
uncertainty weighting (Kendall et al.), **not** GradNorm, **not** a fixed
non-1.0 weight vector. This replaced an earlier `0.001 * loss_completion`
term measured to crush that head's gradient to ~1/1000th of the others
(`train.py:99-106`).

**Optimizer:** `optim.Adam(model.parameters(), lr=GNN_LEARNING_RATE,
weight_decay=1e-4)` (`train.py:45`), `lr` default `0.005` (`config.py:79`).

**LR schedule:** `optim.lr_scheduler.ReduceLROnPlateau(optimizer,
mode='min', factor=0.5, patience=10)` (`train.py:46`) — every other
parameter (`threshold`, `cooldown`, `min_lr`) is left at its PyTorch default
(not overridden).

**Epochs:** `GNN_EPOCHS = 150` default (`config.py:64-78`), derived from a
real 150-epoch log — val loss still improving at epoch 100, best checkpoint
at epoch 147, flattening by 148-150.

**Checkpoint selection:** best-val-loss (`if val_total_loss.item() <
best_val_loss: torch.save(...)`, `train.py:136-138`) — checked **every**
epoch. **There is no early-stopping mechanism** — the `for epoch in
range(1, epochs+1)` loop (`train.py:117`) always runs the full 150 epochs
regardless of plateau; only the *saved checkpoint* tracks the best point,
training itself never stops short. **This should be stated plainly in the
paper as "best-checkpoint selection without early stopping," not "early
stopping."**

**Hyperparameter search:** `utils/config.py`'s GNN constants are **manually
reasoned and measured** (each has a comment citing the specific run/number
that justified it — e.g. `GNN_EPOCHS`'s log-based derivation, `delta=0.4`'s
residual-distribution derivation). A genuine Optuna Bayesian search exists —
`scratch/optuna_completion_priority.py` (`optuna.pruners.MedianPruner`,
`N_TRIALS=25`, `MAX_EPOCHS_PER_TRIAL=40`) — but its own docstring calls it
explicitly **"a one-off diagnostic (not part of the pipeline)"**, targeting
only completion+priority loss, and it is never imported by
`models/train.py` or `run_final_pipeline.py`. **If the paper wants to claim
Optuna-based hyperparameter tuning, that claim needs the caveat that it's an
exploratory scratch artifact, not the mechanism that actually set the
production defaults.**

**(c)** Input: `hetero_graph.x_dict` (machine [N_m,5], worker [N_w,4],
operation [N_o,10]), `edge_index_dict` (6 relations), `type_idx` [N_o] long
tensor. Output: `{'delay': [N_o,1] logits, 'completion': [N_o,1] minutes,
'priority': [N_o,1] in [0,1]}`.

**(d)** `models/gnn.py:1-293`, `models/train.py:1-161`,
`utils/config.py:62-80`, `scratch/optuna_completion_priority.py:1-40`.

**Techniques used in this stage:**
- Heterogeneous GraphSAGE (`SAGEConv` × 3, `to_hetero`, sum aggregation), **split-encoder** multi-task architecture (adopted via controlled A/B experiment)
- Per-head loss: BCEWithLogitsLoss (delay), Huber loss with a measured delta (completion), MSE (priority); combined by **unweighted equal-parity sum**, not uncertainty/GradNorm weighting
- Adam + `ReduceLROnPlateau`; fixed-epoch training with best-checkpoint selection — **no early stopping**
- Hyperparameters manually measured and reasoned, not searched in production (a separate, unused Optuna scratch script exists)

---

## STAGE 9 — Evaluation

**(a)/(c)** `models/evaluate.py::_load_model_and_val_predictions` (lines
20-60) rebuilds the graph, loads the checkpoint, runs one forward pass, and
returns raw predictions/labels **restricted to `val_mask` only** — every
metric below is therefore a held-out-set metric, never a training-set one.

| Metric | Formula/source | Function |
|---|---|---|
| `delay_accuracy/precision/recall/f1` | sklearn, threshold 0.5 on sigmoid(delay logits) | `evaluate_gnn`, lines 125-128 |
| `completion_mae_minutes` / `completion_rmse_minutes` | sklearn `mean_absolute_error` / `root_mean_squared_error` on raw minutes | lines 131-132 |
| `priority_mae` | sklearn `mean_absolute_error` (variable renamed `mae_prio` in this session's fix — was misleadingly called `mse_prio`, computed MAE the whole time) | line 135 |
| `average_precision` (AP), `roc_auc`, full PR/ROC curves | sklearn `precision_recall_curve`, `roc_curve`, `average_precision_score`, `roc_auc_score` | `compute_delay_curves`, lines 63-106 |

**Not present in `models/evaluate.py`:** precision-at-top-k /
confidence-lift analysis. That metric exists only in
`scratch/delay_calibration_and_confidence_gate.py`, referenced by
`SESSION_HANDOFF.md` — **it is a one-off scratch measurement, not part of
the standing evaluation pipeline.** If the Methodology section cites the
3.07× top-2% lift figure, it should be attributed to that scratch analysis,
not to `models/evaluate.py`.

**(b) Technique:** Standard sklearn classification/regression metrics
computed once per checkpoint against the fixed held-out validation split —
no cross-validation, no bootstrap CI computed in `evaluate.py` itself (a
bootstrap CI for AP does exist, but again only as a scratch/one-off
measurement referenced in `SESSION_HANDOFF.md`, not in this module).

**(d)** `models/evaluate.py:1-258`.

**Techniques used in this stage:**
- sklearn-based classification metrics (accuracy/precision/recall/F1, PR-AUC/AP, ROC-AUC) and regression metrics (MAE, RMSE) on a fixed held-out split
- No cross-validation or bootstrap resampling in the standing pipeline (exists only as an ad hoc scratch analysis)

---

## STAGE 10 — GA scheduler

**(a)/(b) Chromosome:** `[OS array][LA array][MC array]`, each length
`num_ops` (`ga/chromosome.py:167-242`) — Operation Sequence (a permutation of
operation keys), Line Assignment, Machine Count. This is a **generational GA
with elitism and a scalarized (weighted-sum) single-objective fitness — not
NSGA-II, not any Pareto-based multi-objective algorithm.** There is exactly
one fitness scalar per individual (`ga/fitness.py:483-490`); no
non-dominated sorting, no crowding distance, no Pareto front exists anywhere
in the codebase (confirmed by the full read of `ga/scheduler.py`,
`ga/fitness.py`, `ga/operators.py` this session — no such terminology or
mechanism appears).

**Population/generations, and where the claimed config values actually come
from — flagged discrepancy:** `utils/config.py` defines
`GA_POPULATION_SIZE=50`, `GA_GENERATIONS=100`, `GA_MUTATION_RATE=0.1`, but
**grepping the entire codebase confirms these three constants are imported
and used nowhere else** — they are dead configuration. What actually runs:
`run_final_pipeline.py:42` hardcodes `POP, GENS = 50, 50` as a separate
module-level constant (**generations=50, not the config's 100**);
`agent/decision_engine.py`'s `trigger_rescheduling()` separately hardcodes
`pop_size = 50`, `generations = 50` locally (lines ~157-158). Mutation rate
is **not** a fixed rate at all: `ga/scheduler.py:414` derives it dynamically
per problem size, `self.mutation_indpb = min(0.1, MUTATIONS_PER_INDIVIDUAL /
max(1, self.num_ops))` with `MUTATIONS_PER_INDIVIDUAL = 6.0` (line 88) — i.e.
an expected ~6 gene mutations per individual regardless of chromosome
length, completely bypassing `GA_MUTATION_RATE`. **This is a real gap
between the documented/configurable design and the actual running numbers —
worth stating plainly rather than citing `utils/config.py`'s values as what
the GA runs with.**

**Selection:** `tools.selTournament(tournsize=2)` (`ga/scheduler.py:449`) —
DEAP tournament selection, reduced from an earlier `tournsize=3` after that
was measured to collapse population diversity within a few generations.

**Crossover** (`ga/operators.py::custom_crossover`, lines 245-299): 2-point
swap for LA/MC arrays at shared random cut points, **Order Crossover (OX1)**
(custom-implemented, `order_crossover`, lines 38-76, not DEAP's `cxOrdered`,
because that assumes integer-index genes) for the OS permutation, followed
by `repair_precedence` and `repair_line_and_machine_count`.

**Mutation** (`ga/operators.py::custom_mutate`, lines 402-471): work-aware
line reassignment, upper-half-biased machine-count redraw, swap mutation on
OS, each independently gated at `indpb` per gene.

**Elitism:** top `elite_frac` (default 0.04, i.e. 4%, min 1) cloned forward
unchanged each generation (`ga/scheduler.py:696-698, 989`). **Random
immigrants** (not part of a "textbook" GA, an explicit addition):
`immigrant_frac` (default 0.1) of offspring replaced with brand-new random
individuals each generation (lines 1001-1014) — added specifically because
elitism alone let the population collapse to near-clones.

**Full fitness function** (`ga/fitness.py:46-490`), every term with its
exact weight:

| Term | Weight | Formula |
|---|---|---|
| Makespan | `ALPHA = 1.0` | `scored_makespan_minutes / 60.0` (hours) |
| Delay-risk penalty | `GAMMA = 1.0` | mean over ops of `delay_prob * 50.0` |
| Priority reward (subtracted) | `DELTA = 1.0` | mean over ops of `priority * position_weight * 100.0`, `position_weight = 1 - i/num_ops` |
| Load-balance penalty | `EPSILON = 1.0` | `statistics.pstdev(busy_minutes_per_line) / 60.0` |
| Deadline lateness | `ZETA = 3.0` (confirmed) | `avg_lateness_hours * ZETA` |
| On-time-rate penalty | `SERVICE_LEVEL_WEIGHT = 24.0` | `frac_orders_late * 24.0` |
| Idle-labour penalty | `IDLE_LABOR_WEIGHT = 480.0` (from `utils/config.py:142`) | `(1 - labour_utilization_vs_ceiling) * 480.0` |

Plus two threshold/scale constants used inside the simulation, not the
fitness sum itself: `HIGH_RISK_THRESHOLD = 0.75` (post-hoc high-risk
re-route trigger, `ga/fitness.py:60`), `CHANGEOVER_MINUTES = 25.0`
(per-machine re-rigging penalty, line 69). `RISK_BUFFER_AT_CERTAINTY = 0.0`
(`ga/schedule_utils.py:62`, currently disabled after a 3-seed re-run found
no reliable effect) and `MC_PROTECTION_FRACTION = 1.0` (line 98, inactive)
are both present in code but currently inert by design, not by omission —
their history is documented in-file.

**(d)** `ga/chromosome.py:1-250`, `ga/scheduler.py:1-1090`,
`ga/operators.py:1-481`, `ga/fitness.py:1-579`, `ga/schedule_utils.py:1-351`.

**Techniques used in this stage:**
- Generational genetic algorithm, DEAP-based, tournament selection (k=2), elitism + random-immigrant injection — **not NSGA-II / not multi-objective Pareto optimization**
- Custom Order Crossover (OX1) for the permutation segment, 2-point swap for line/machine-count segments, with repair operators for precedence and eligibility validity
- Single scalarized weighted-sum fitness function, 7 named terms with fixed hand-tuned weights (not learned or auto-balanced)
- **Flag for the paper:** the population-size/generation-count/mutation-rate values actually used are hardcoded locally in `run_final_pipeline.py`/`decision_engine.py` and derived dynamically in `ga/scheduler.py`, respectively — **not** the `GA_POPULATION_SIZE`/`GA_GENERATIONS`/`GA_MUTATION_RATE` constants in `utils/config.py`, which exist but are dead code

---

## STAGE 11 — Order-completion aggregation

**(a)** `agent/decision_engine.py::_export_proposed_schedule` walks the
decoded chromosome once (lines 571-744), tracking per order the operation
with the highest `operation_sequence_number` seen so far (`order_final_op`,
lines 544, 672-678) — this is the order's true final operation regardless of
what position it happens to occupy in the GA's OS array. After the walk,
`_export_order_completions` (lines 865-954) computes, per order:

```python
predicted_completion   = final_op['cushioned_end_time']                              # line 887
quoted_delivery_date   = predicted_completion + timedelta(days=ORDER_QUOTE_CUSHION_DAYS)  # line 888
```

`ORDER_QUOTE_CUSHION_DAYS = 2.0` default (`utils/config.py:92`) — a flat,
order-level cushion **on top of** each operation's own per-operation
GNN-delay-risk cushion (`apply_risk_buffer`, `ga/schedule_utils.py:131-152`)
— i.e. two separate, additive cushioning mechanisms at two different
granularities, not one unified buffer.

**(b) Technique:** Deterministic rollup/aggregation of already-computed
GA-simulation output (max-by-sequence-number reduction, plus a fixed
additive offset) — no model, no optimization at this stage.

**(c)** Input: the decoded chromosome + `order_deadline_abs` (absolute
datetimes). Output: one dict per order — `production_order_id,
predicted_completion, quote_cushion_days, customer_quoted_delivery_date,
existing_committed_deadline, deadline_already_passed, breach_risk,
late_by_hours, schedule_added_late_hours` — written to
`outputs/order_completions.json`.

**(d)** `agent/decision_engine.py:865-954`.

**Techniques used in this stage:**
- Deterministic max-by-routing-sequence rollup of simulated per-operation completion times to an order-level figure
- Fixed additive cushion (business-policy constant), separate from and on top of the per-operation risk buffer

---

## STAGE 12 — Live agent / disruption handling

**(a) Path:** `agent/monitor.py` polls `Machinery_Assets.current_status` for
new `Breakdown` transitions (lines 42-96), and for each one calls
`agent/event_handler.py::handle_machine_breakdown` (lines 24-110), which
updates the DB then calls `agent/impact_assessor.py::assess_machine_breakdown`
(lines 325-556) to get a per-affected-operation tier decision, then either
applies a local patch (`apply_local_patch`) or escalates to
`AIDecisionEngine.trigger_rescheduling()`.

**(b) Fuzzy inference method, named precisely:** `agent/fuzzy_logic.py::decide_tier`
(lines 154-209) implements a **genuine Mamdani-style fuzzy inference
system** — real trapezoidal membership functions (`_trapezoid`, lines 57-71)
over two linguistic input variables, real fuzzy-AND (`min`) rule-antecedent
composition, and real fuzzy-OR (`max`) aggregation across rules sharing a
consequent. **This is not disguised if/else threshold logic** — confirmed by
reading the full membership-function and rule-evaluation code: values
genuinely vary continuously (e.g. an order at 4.9 vs 5.1 days of slack gets
almost-identical, not discretely-different, membership values, which is the
entire point of using fuzzy logic here per the module's own docstring, lines
4-8).

- **Fuzzy variables:** `slack_membership` (lines 74-96) → {`urgent`,
  `moderate`, `relaxed`}, anchored at `SLACK_URGENT_FULL=3`,
  `SLACK_URGENT_ZERO=10`, `SLACK_MODERATE_PEAK=12±8`, `SLACK_RELAXED_ZERO=14`,
  `SLACK_RELAXED_FULL=20` days. `impact_membership` (lines 99-110) →
  {`negligible`, `moderate`, `severe`}, anchored at 0/4h, 16±20h, 24/48h.
- **Rule base** (lines 158-190, 5 rules): `R1: impact=Negligible → NO_ACTION`;
  `R2: impact=Moderate ∧ slack=Relaxed → LOCAL_PATCH`; `R3: impact=Moderate ∧
  slack=Moderate → LOCAL_PATCH`; `R4: impact=Moderate ∧ slack=Urgent →
  FULL_RESCHEDULE`; `R5: impact=Severe → FULL_RESCHEDULE`.

**Where it is a genuine simplification, stated explicitly as requested:**
the **defuzzification** step is *not* classical Mamdani (no
centroid-of-aggregated-fuzzy-set computation) — because the output here is a
**categorical** decision (one of 3 discrete tiers), not a continuous
quantity, so a centroid defuzzification wouldn't apply anyway. Instead, the
three tiers' rule-strengths are combined by `max()` per tier and the tier
with the highest combined strength wins (`strengths = {...}; tier =
max(strengths, key=strengths.get)`, lines 192-197) — a **max-rule-strength
categorical decision**, appropriate for a categorical output but simpler
than a continuous-output Mamdani system would require. Separately,
`urgency_score()` (lines 113-151, used only for *ranking* orders against
each other in `impact_assessor.py`'s donor-preemption logic, not for the
tier decision) is a **single monotonic trapezoid ramp**, not a multi-rule
fuzzy system at all — call it what it is: a smoothed threshold function, not
fuzzy inference.

**(c)** Input: `slack_days` (float, days), `impact_hours` (float),
`manager_forced` (bool). Output: `{tier: NO_ACTION|LOCAL_PATCH|
FULL_RESCHEDULE, reason: str, rule_strengths: dict}`.

**(d)** `agent/fuzzy_logic.py:1-230`, `agent/impact_assessor.py:325-556`,
`agent/event_handler.py:24-110`, `agent/monitor.py:1-103`.

**Techniques used in this stage:**
- Mamdani-style fuzzy inference: trapezoidal membership functions, min-composition for rule antecedents, max-aggregation across rules — genuinely fuzzy, not disguised threshold logic
- Simplified defuzzification appropriate to a categorical (not continuous) output: highest-combined-rule-strength tier selection, not centroid defuzzification
- A separate, non-fuzzy monotonic ramp function (`urgency_score`) used only for donor-preemption ranking — explicitly not part of the fuzzy rule base

---

## STAGE 13 — Explainability

**(a)/(b) Technique, confirmed precisely, not taken at face value as
requested:** `agent/explainability.py::ExplainabilityEngine` is **pure
rule-based template text surfacing** — it reads the already-generated
`xai_reason` string field out of `outputs/proposed_schedule.json` (a field
that `agent/decision_engine.py::_export_proposed_schedule` populates at
schedule-export time using deterministic f-string templates describing the
business rule that fired — e.g. "Delay Risk {p} exceeded the {threshold}
escalation threshold — re-routed from...", `decision_engine.py:694-726`) and
does substring matching against that pre-written text to bucket operations
into categories (`"exceeded"` → reroute, `"re-rigging charged"` →
changeover, `"past its"` → deadline warning; `explainability.py:33-36`).

**This is not SHAP, not LIME, not attention-weight visualization, not any
post-hoc model-interrogation technique.** No such library or mechanism
(`shap`, `lime`, `captum`, attention-weight extraction from the GNN) appears
anywhere in the codebase — confirmed by the full reads this session of every
model/agent file. The "explanation" is a natural-language rendering of the
deterministic scheduling logic's own reasoning at the moment it made a
placement decision (line/machine choice, escalation, changeover), not an
interrogation of what the trained GNN internally weighted. The GNN's own
predictions (delay probability, priority) feed *into* the business rules
that get rendered as text (e.g. the escalation threshold check), but nothing
surfaces *why the GNN itself* produced a given probability — there is no
feature-importance or attention analysis anywhere in the live pipeline (only
ever done ad hoc in `scratch/` permutation-importance scripts referenced by
`SESSION_HANDOFF.md`, never as a standing "explainability" capability).

**(c)** Input: `outputs/proposed_schedule.json` (list of per-operation dicts
with `xai_reason`). Output: an executive-summary dict (counts + full audit
trail) or a single formatted natural-language string per (order, operation)
lookup.

**(d)** `agent/explainability.py:1-83`; the actual reasoning-text generation
it depends on lives in `agent/decision_engine.py:690-726`.

**Techniques used in this stage:**
- Rule-based natural-language template rendering of deterministic scheduling decisions (line/machine assignment, escalation, changeover), generated at export time — **not** a model-interrogation XAI technique (no SHAP/LIME/attention-weight analysis exists in the standing pipeline)
- **Flag for the paper:** if "Explainability (XAI) Engine" is presented as a formal XAI method, it should be qualified as rule-based decision justification, not feature-attribution or model-introspection XAI
