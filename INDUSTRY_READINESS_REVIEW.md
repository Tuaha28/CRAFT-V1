# Industry readiness review

This is a static review of the production scheduling project, based on executable source and the project's existing experiment reports. It is not a new benchmark, spreadsheet audit, or live database validation. No synthetic outcomes are claimed to be observed company history.

The system supports further research and supervised shadow trials. It should not autonomously control production until schedule feasibility, publication, and recovery are established. The existing GA should be retained while those gaps are addressed. The delay-classifier ceiling remains a hypothesis requiring a corrected diagnostic.

## Implementation status of the narrowly authorized follow-up

The original findings below are preserved as review findings; this status distinguishes them from the subsequent changes.

- Item 2: the random-line/empty-pool substitution has been removed. An impossible instance raises ScheduleInfeasibleError with a structured result naming the order, operation, and capability. Positive-work candidates with no pool receive infinite fitness; detailed evaluation includes the structured infeasibility. The existing minimum-quantity/setup convention is unchanged.
- Item 6, checkpoint portion only: missing checkpoints now abort before graph/database work. Corrupt or incompatible checkpoints raise an explicit error. Successfully loaded models enter eval mode before inference.
- Order provenance, non-cascading synthetic actual timing, and shared GNN training components have been corrected in comments, docstrings, and reports. This did not change simulator or model logic.
- The delay-ceiling diagnostic and GNN-value ablation are explicitly deferred and unchanged. Other recommendations below are not implemented by this follow-up.

## 1. Proposed schedules and committed execution need one lifecycle

**Issue and evidence.** [decision_engine.py](agent/decision_engine.py) exports proposed JSON schedules, while [impact_assessor.py](agent/impact_assessor.py) reads and modifies Job_Shop_Schedules. The dashboard lacks a complete approval/publication/acknowledgement workflow connecting them. The runtime scheduler is constructed without current committed machine reservations; reading a schedule for graph features does not reserve it in the GA.

**Why it matters.** A manager can see a new proposal while the disruption handler acts on an older database schedule. Running work and commitments outside the selected horizon are not automatically protected. Candidate generation is not successful disruption recovery.

**Next steps.** Add immutable draft, validated, approved, active, and superseded schedule versions. Freeze running work and a near-term execution window. Load reservations before optimizing the rest. Publish the approved version atomically, record the input snapshot/model/config/approver, and require floor acknowledgement. Rollback must revalidate against present resource and WIP state.

**Effort/payoff.** Moderate restructuring; critical payoff. An advisory-only flow and feasibility gate fit an FYP; full publication control precedes autonomous deployment.

## 2. Missing machine capacity must be infeasible

**Original issue and evidence.** [chromosome.py](ga/chromosome.py) substituted a random active line and empty pool when no machine matched. [schedule_utils.py](ga/schedule_utils.py) returned a ready time and no selected machines; [fitness.py](ga/fitness.py) still advanced completion without reserving a resource.

**Why it matters.** Losing the last machine of a capability could produce fictitious production rather than an infeasibility result.

**Next steps.** Reject positive work without physical capacity and report the affected order and capability. Independently validate exported schedules for operation coverage, eligibility, non-overlap, precedence, releases, and finite times.

**Follow-up status.** The instance and fitness guards are implemented, with regression coverage in [test_ga_infeasibility.py](tests/test_ga_infeasibility.py). The low-level empty-pool helper contract remains unchanged. The broader independent schedule validator is not implemented.

**Effort/payoff.** Quick guard and focused regression tests; critical payoff.

## 3. Worker assignment does not establish staffing feasibility

**Issue and evidence.** [decision_engine.py::_assign_operators](agent/decision_engine.py) uses accumulated busy minutes rather than time reservations, checks shift availability at start only, and can return fewer workers than machines from an undersized nonempty pool.

**Why it matters.** A person can receive overlapping jobs or remain assigned beyond their shift. Global headcount does not demonstrate simultaneous availability by skill, factory, and time. Efficiency-aware selection alone does not solve this.

**Next steps.** Track worker intervals and handovers. Require sufficient qualified workers throughout each segment; repair timing/machine count and re-evaluate, or reject. A matching stage coupled to decoding can enforce staffing without worker genes. Add efficiency preferences after calendars and eligibility are valid.

**Effort/payoff.** Moderate restructuring; critical payoff. A validator fits the FYP, comprehensive joint resource scheduling is production work.

## 4. Local patches need immutable row identity and concurrency control

**Issue and evidence.** [apply_local_patch](agent/impact_assessor.py) wraps recipient and donor writes in a transaction, but identifies rows by order, operation, and asset rather than a unique lot/schedule-row ID. Multiple lots can reuse an asset. It lacks version preconditions, expected-row-count checks, and transactional slot revalidation. Machine/timestamp changes do not establish worker, factory, or successor feasibility.

**Why it matters.** Patches can move several rows, apply stale assessments, conflict with another handler, or delay an operation beyond its successor. Tentative booking inside one assessment does not prevent concurrent conflicts.

**Next steps.** Carry row IDs and versions; lock or compare-and-swap the version; revalidate resources and overlaps inside the transaction. Check recipient/donor successors, workers, line/factory, setup, and remaining work. Keep actual execution history separate from revised plans.

**Effort/payoff.** Quick identity safeguards plus moderate repair/concurrency restructuring; critical payoff.

## 5. Monitoring needs durable event processing

**Issue and evidence.** [monitor.py](agent/monitor.py) remembers a transition before handling it; failure then leaves no new transition to retry. Full scheduling blocks polling. Startup establishes a baseline rather than reconciling already-broken machines. The handler's NO_ACTION branch skips reassignment even when an alternate is the reason impact is negligible; the prior postmortem already documents this unresolved finding.

**Why it matters.** A possible recovery can be logged as completed while the committed schedule still names the broken asset. Transient failures can lose an event.

**Next steps.** Persist event IDs, status, retries, and deduplication. Separate detection from scheduling execution and reconcile at startup. Distinguish no change needed from a zero-delay replacement that must actually commit. Coalesce simultaneous disruptions against one fresh snapshot.

**Effort/payoff.** Moderate restructuring; very high payoff. Error status and no-action semantics are FYP-realistic.

## 6. Artifact and infrastructure failures need explicit safe behavior

**Original issue and evidence.** [decision_engine.py](agent/decision_engine.py) continued with randomly initialized weights when the checkpoint was absent; that branch did not call eval. [run_final_pipeline.py](run_final_pipeline.py) starts by reseeding, and the seeder truncates tables. Some handlers log errors and return, allowing UI success messages. Pooling and pre-ping exist, but engine creation alone does not demonstrate database readiness.

**Why it matters.** Missing artifacts could produce arbitrary recommendations; failed recovery can look successful; a demonstration command can destroy operational data.

**Next steps.** Separate simulation, shadow, and production modes and credentials. Use clear error contracts. Separate seeding from daily operation. Validate missing/corrupt artifacts, DB failures, write failures, and restart recovery.

**Follow-up status.** Missing/corrupt/incompatible checkpoint behavior now fails loudly. A valid loaded model calls eval before inference. [test_checkpoint_guard.py](tests/test_checkpoint_guard.py) covers those cases. The other failure/recovery recommendations remain open.

**Effort/payoff.** Quick checkpoint guards and error contracts; critical payoff.

## 7. Six factories need real physical boundaries

**Issue and evidence.** Worker sourcing and alternate-machine lookup widen company-wide. GA eligibility does not enforce an order-specific factory or inter-factory transport. [scheduler.py](ga/scheduler.py) calculates work using average active-line efficiency, rather than the selected line's efficiency.

**Why it matters.** A resource at another branch is not instantly available. Company-wide capacity can hide local shortages and physically impossible transfers.

**Next steps.** Default to factory-scoped orders, workers, and reserves. Model authorized transfers with time, WIP/material availability, and cost. Verify actual calendars, maintenance, crew capacity, and changeovers with each factory. Pilot one branch first.

**Effort/payoff.** Moderate scoping changes; critical payoff. Transfer optimization and detailed calendars are larger production extensions.

## 8. Daily ingestion and progress need an explicit contract

**Issue and evidence.** [api/main.py](api/main.py) is empty; the dashboard is an internal test tool. Material-shortage handling logs and reschedules without recording a hold or material-ready date. Remaining work is a fraction of rows not Done, and the scheduler's max(1, ...) retains one unit even when the fraction is zero.

**Why it matters.** Shortages do not constrain optimization. Unequal lot quantities and partial production make row-count progress inaccurate. Finished operations can be rescheduled. Without daily updates, factory state becomes fictional.

**Next steps.** Add validated, idempotent imports with stable external IDs and reconciliation reports. Record quantities started, completed, accepted, rejected, and reworked with lot/resource/time identity. Add material/quality holds. Exclude zero-remaining work while retaining completed predecessor facts.

**Effort/payoff.** Quick completed-work/hold fixes and moderate ingestion implementation; very high payoff. CSV imports can support an FYP; ERP/MES integration follows later. These logic changes are not part of the authorized follow-up.

## 9. Synthetic actual timestamps do not form a constrained execution run

**Issue and evidence.** [seed_database_fixed.py](data_generation/seed_database_fixed.py) adds a newly sampled repair to planned end_t and computes a separate perturbed actual_end_t. Machine/worker availability, the heap, and routing prev_end advance with planned end_t, not actual_end_t.

**Why it matters.** The planned reference already includes the sampled breakdown repair. Actual overruns do not cascade into later machine jobs, worker jobs, or routing successors; actual timestamps can overlap downstream execution. They are synthetic timing annotations, not observed outcomes or a fully resource-constrained actual simulation.

**Next steps.** Preserve a clean reference plan and simulate execution with separate actual resource calendars and predecessor finishes. Inject downtime into execution and propagate consequences. Validate actual timelines and version the simulator/dataset rather than silently changing earlier benchmark meanings.

**Effort/payoff.** Moderate-to-major redesign; very high scientific payoff. Correct wording now; demonstrate propagated overruns on a small scenario before a full dataset replacement.

## 10. All 1,029 seeded records are resampled synthetic orders

**Issue and evidence.** The seeder computes TOTAL_ORDERS from 139 genuine PO groups plus 890 StyleId+Color+BuyMonth proxy groups. Its order loop independently samples dates, quantity, product, and lead time and fabricates PO metadata. The groups calibrate count; their original tuples are not preserved. Start dates are sampled from the row-level distribution, not the shorter source order-level distribution.

**Why it matters.** Correct marginal statistics and counts do not preserve quantity/style/deadline/season relationships. Row sampling overweights source orders represented by more rows.

**Next steps.** State this provenance explicitly and retain source/proxy/synthetic identifiers. Where appropriate, resample complete tuples. Keep source order-level statistics distinct from generated database statistics.

**Effort/payoff.** Quick wording corrections, now applied; moderate future reconstruction; high examiner-facing payoff.

## 11. Duration calibration relies on an unresolved Output interpretation

**Issue and evidence.** Seeder comments say PPC Output caps at 100 for 72.7% of rows and resembles a percentage, but the calibration divides WorkHrs by Output as a unit count. A global factor does not independently establish the conversion from line throughput to individual-operation work. The GA also omits the seeder's calibration factor and 30–480-minute clamps.

**Why it matters.** The 2.51 value is computed from real cells under an uncertain interpretation, not established physical minutes per garment. This threatens absolute capacity and timing claims.

**Next steps.** Obtain company definitions for Output, WorkHrs, SMV, and worker capacity. Validate one line/day example dimensionally. Reconcile duration models and report clamp incidence and sensitivity. Label unresolved assumptions clearly.

**Effort/payoff.** Low coding effort, potentially moderate stakeholder effort; extremely high payoff.

## 12. The DGP ceiling is not a final diagnosis

**Issue and evidence.** [delay_dgp_ceiling.py](scratch/delay_dgp_ceiling.py) multiplies planned elapsed duration by variance. With productive duration d, repair r, and multiplier m, the seeder uses d*m+r but the diagnostic uses (d+r)*m: the mismatch is r*(m-1). Current 24-hour timing is straight addition, so no current overnight-calendar mismatch should be asserted. The diagnostic also rebuilds the split instead of loading saved validation IDs, uses unseeded Monte Carlo, and evaluates one realized outcome vector.

**Why it matters.** A useful approximate oracle does not prove that no exploitable signal remains. The earlier repair caveat and overlapping uncertainty intervals do not establish equivalence to a correct oracle.

**Next steps.** Persist productive work, repair, runtime-since-repair, scenario seed, and generator version. Reuse exact outcome logic; distinguish a retrospective explanatory oracle from pre-decision prediction. Evaluate paired GNN/oracle differences with order-level resampling and independent scenarios. Independent per-lot multiplier CDFs can also provide an analytic operation-level probability conditional on fixed timing/events.

**Effort/payoff.** Moderate bounded experiment; high scientific payoff. Explicitly deferred: no diagnostic changes were authorized or made.

## 13. Delay experiments should target remaining information, not repeat weighted BCE

**Evidence.** [DELAY_LABEL_FIX_PROPOSAL.md](DELAY_LABEL_FIX_PROPOSAL.md) records the completed pos_weight=8.5252 experiment: AP 0.1723 to 0.1698 and ROC-AUC 0.5579 to 0.5497, with other-head regression. Decisive-lot breakdown features already exist.

**Next steps.** Correct the oracle first. Then test decisive/near-decisive lot queue exposure, finish gaps, durations, and number of plausible last finishers. Leave exposure merits testing only if variable and available at prediction time. Try a compact tabular baseline before another expensive GNN architecture.

Threshold tuning against alert budgets can improve decision recall, not AP/AUC. Weighted BCE changes precision/recall trade-offs [1]. Focal loss is low priority because hard positives may be irreducible noise. If useful to the business, predict severity, quantiles, or a frozen milestone miss; the current relative operation-delay label is not customer lateness. Do not rebalance labels merely to improve F1.

**Effort/payoff.** Quick threshold analysis; moderate feature/oracle experiments; uncertain modest payoff. Broad architecture/loss sweeps are weak deadline priorities.

## 14. GNN branches share trainable representation and training controls

**Issue and evidence.** [models/gnn.py](models/gnn.py) has one op_type_embedding used by both branches. [models/train.py](models/train.py) uses one optimizer, one total-validation-loss LR scheduler, and one combined-loss checkpoint selection. Separate projections/graph encoders do not imply zero shared parameters.

**Why it matters.** Reweighting delay can affect completion/priority through the embedding, LR decisions, and selected epoch. The weighted-loss regression does not contradict this actual architecture.

**Next steps.** Correct isolation claims, as done in the follow-up. A future isolated experiment should freeze or separate the embedding and other branch and use independent optimization/selection controls.

**Effort/payoff.** Quick wording changes, moderate controlled experiment; high explanatory payoff. No architecture or training changes are included in this follow-up.

## 15. GNN-to-GA contribution is narrower than described

**Issue and evidence.** Runtime mappings pass delay and priority, not completion, to the GA. Completion labels are planned spans; priority labels are deterministic slack percentiles. The inspected training path does not use GA-generated teacher schedules. The fitness delay penalty sums a fixed delay score once per operation, making it constant across candidates. The adopted buffer multiplier is zero. Export has a separate risk reroute.

**Why it matters.** A GA advantage over heuristics is not proof of GNN benefit. Completion-head accuracy does not establish improved scheduled durations. A constant risk term cannot guide GA ranking.

**Next steps.** State actual head usage. Compare predicted priority against exact slack and constant priority under identical budgets/snapshots. Identify the constant term and validate any export-time reroute. Candidate-conditioned learning is a later redesign.

**Effort/payoff.** Quick claim corrections and moderate ablations; very high research payoff. GNN-value ablation is explicitly deferred and untouched.

## 16. Prediction-time features and live-learning labels need separation

**Issue and evidence.** Assignment edges and dispatch/decisive-lot features depend on an existing seeded schedule. New orders lack them; predictions remain fixed while GA assignments change. Missing outcomes default to negative delay labels; partial outcomes can omit the eventual last finisher. Active-order queries exclude completed order statuses. Priority ranks change with the population.

**Why it matters.** Avoiding outcome columns does not prove that features exist before the scheduling decision. Fine-tuning directly on live active orders would confuse missing/censored work with observed outcomes and omit completed history.

**Next steps.** Define prediction-time snapshots and reproduce them in evaluation. A preliminary heuristic plan could supply consistent FYP context. Build immutable historical training views with per-head validity/maturity masks and provenance. Separate real and synthetic evaluation; use temporal/factory/scenario holdouts and an untouched final test. Compare fine-tuning with simple real-data alternatives.

**Effort/payoff.** Moderate data/evaluation restructuring; critical payoff. No label or feature logic changes are included in this follow-up.

## 17. Complete contention cliques create avoidable quadratic cost

**Issue and evidence.** [edge_builder.py](graph/edge_builder.py) materializes all pairs in company-wide type pools, including distant release dates. The source documents roughly 17.8 million directed edges for one dataset. SAGEConv defaults to mean aggregation [2].

**Next steps.** A clique neighbor mean can be computed as (pool sum minus this node)/(pool size minus one), per layer. Preserve root transforms and relation combination and verify forward/gradient equivalence before replacing explicit edges. Cache static data and separate inference building from labels. Factory/time-scoped contention is a later model change requiring retraining.

**Why it matters.** This offers a concrete latency/memory improvement without assuming an entirely new GNN. It does not resolve missing new-order assignment context by itself.

**Effort/payoff.** Moderate restructuring; potentially high payoff.

## 18. Production needs service, monitoring, authorization, and artifact contracts

**Issue and evidence.** Runtime uses one horizon-limited optimize call; research uses rolling horizons and polish. Forced rush orders can fall outside the release-selected horizon. Graph reads are separate snapshots. Checkpoints and validation IDs are separate mutable files. Overrides are per-handler memory. The test dashboard has no role/factory authorization in its mutation path. DB user defaults to root and requirements are unpinned.

**Next steps.** Unify runtime and evaluation; include affected/forced orders; freeze one revision/time origin and reject stale publication. Measure queue, DB, graph, inference, GA, validation, and total p95 latency. Agree separate acknowledgment, repair, and full-planning targets.

Version model weights, feature schema, taxonomy, scales, data/simulator versions, validation manifest, and dependencies together; promote atomically. Monitor stale inputs, pending events, infeasibility, overrides, mature prediction errors, and actual delivery outcomes. Retraining produces a challenger for approval.

Add authenticated factory-scoped roles, durable overrides with actor/reason/expiry, least-privilege credentials, tested backups, and reproducible installation. Acceptance tests should include empty capacity, overlapping workers, concurrent patches, stale snapshots, outages, and restart recovery.

**Effort/payoff.** Moderate restructuring; critical payoff. Basic instrumentation/restricted access fit the FYP; full authorization/recovery precedes production expansion.

## Deadline recommendation

Retain the GA and disabled confidence buffer. Existing negative optimizer experiments do not justify another rewrite. Prioritize feasibility/error guards, honest provenance and simulator claims, corrected ceiling analysis, and a bounded GNN-value ablation when separately authorized. Preserve existing benchmark numbers as results under the tested synthetic model, not observed factory outcomes. A supervised single-factory shadow trial is the appropriate next deployment step.

## Follow-up verification

- Fix 1 scoped suite: 133 passed.
- Fix 2 scoped suite: 32 passed.
- Final complete suite: 530 passed, zero failures, in 137.86 seconds.
- The 363 warnings were PyTorch/PyG dependency deprecations on Python 3.14.
- Python AST comparisons confirmed wording edits did not alter executable logic; one seeder progress-message string was reworded.
- Actual project files were read back after writing. Diffs were compared against a temporary baseline preserving the pre-existing working-tree edits.
- The delay-ceiling diagnostic, authoritative comparison entry point, models/train.py, and graph/graph_builder.py are byte-for-byte unchanged.
- This verification does not constitute a live-database scheduling run or new benchmark measurement.

## External references

1. PyTorch, [BCEWithLogitsLoss](https://docs.pytorch.org/docs/stable/generated/torch.nn.BCEWithLogitsLoss.html): positive weighting and precision/recall trade-offs.
2. PyTorch Geometric, [SAGEConv](https://pytorch-geometric.readthedocs.io/en/2.8.0/generated/torch_geometric.nn.conv.SAGEConv.html): default mean aggregation and root/neighbor transforms.
3. scikit-learn, [Threshold tuning](https://scikit-learn.org/stable/auto_examples/model_selection/plot_tuned_decision_threshold.html) and [Probability calibration](https://scikit-learn.org/stable/modules/calibration.html).
