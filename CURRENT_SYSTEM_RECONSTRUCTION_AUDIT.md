# Current System Reconstruction and Read-Only Technical Audit

**Project:** An Agent-Based AI-Driven System For Real-Time Scheduling & Delivery Optimization In Garment Management  
**University:** Bahria University Lahore Campus  
**Supervisor:** Muhammad Mudassar  
**Team:** Muhammad Anus Saleem, Muhammad Tuaha Farooq, Moaz Sarwar  
**Factory context:** Needle Craft Jordan  
**Audit date:** 2026-09-16 (Asia/Karachi)  
**Audit mode:** Read-only reconstruction. Apart from creating this requested report, no source, configuration, database, model, test, or authoritative artifact was changed.

Status terms: **VERIFIED AND ACTIVE**, **VERIFIED BUT NEEDS STRONGER TEST**, **PARTIAL**, **BYPASSED**, **REGRESSED**, **SUPERSEDED**, **EXPERIMENT-ONLY**, **PLANNED / NOT IMPLEMENTED**, and **NEW ISSUE**.

The working tree was already heavily modified and contained untracked audit artifacts. The current filesystem, not Git HEAD, was treated as the implementation under review. Existing changes were preserved. Evidence priority was current executable code, current validated database/artifacts, current tests, the integration ledger, the handoff, comments, historical logs, and old papers.

## 1. Executive Summary

The core data and scheduling path is substantially more correct than the historical system. The two-phase simulator freezes the complete planned schedule before actual execution is simulated; full-scale evidence shows that changing only the actual seed leaves all 176,187 planned rows unchanged. The authoritative GA phenotype is worker-aware, uses same-worker pause/resume with explicit productive segments, carries worker reservations between rolling windows, and is shared by fitness and export. The independent validator rejects segmented schedules without segment evidence. Baselines pass through the same decoder and objective as GA.

The corrected graph has 3,000 machine nodes, 9,000 worker nodes, and 8,211 active operation nodes. The latest 150-epoch GNN training completed on 2026-09-15. Checkpoint, preprocessing, split, schema, and graph fingerprint are internally consistent. Checkpoint SHA-256 is `039f1ab3ae39c7b76585fadd321db89429c216a728cafb8c822eae6446d95c6d`; graph fingerprint is `3ea855a9b5e2995986cb7ee974f2ef69525eb1b603e08b933ac73cc72500e744`. The best checkpoint was last written during epoch 133, although best epoch is not persisted explicitly; 133 is a timestamp/log inference.

Current held-out GNN results on the corrected synthetic DGP are: delay AP 0.9962, ROC-AUC 0.9728, precision 0.9931, recall 0.9058, F1 0.9474; completion MAE 1,130.22 minutes and R2 0.4993; priority MAE 0.06639 and R2 0.9169. These are predictive results on simulated/generated targets, not proof of real-factory accuracy or scheduling benefit.

Only priority affects GA candidate ranking in a candidate-dependent way. Delay adds a candidate-invariant term while risk buffering is disabled by value; completion is unused. It is unsafe to claim that all three heads improve scheduling.

The system is not at Integration Freeze. Main blockers are a possibly stale comparison snapshot; no cryptographic model-to-live-dataset UUID binding; breakdown NO_ACTION potentially leaving work on a broken machine; local patches bypassing authoritative worker segments and independent validation; non-durable manager priority; absent maintenance-resource scheduling; a stub FastAPI/no .NET portal; and no final current-formulation multi-seed optimizer comparison.

## 2. Current End-to-End Architecture

**Status: PARTIAL end-to-end; core offline path active.**

Company spreadsheets/configuration -> 139 genuine PO groups expanded to a 1,029-order mixed company/proxy book -> Phase A frozen planning -> Phase B simulated actual execution -> staging validation and journaled promotion -> database queries -> heterogeneous graph and order-grouped split -> split-encoder multi-task GraphSAGE -> scheduler state -> worker-aware rolling GA and baselines -> export and independent validation -> monitoring and fuzzy disruption triage -> no action/local patch/full reschedule -> Streamlit monitoring.

The GNN does not build the schedule. GA is the schedule constructor and optimizer. FastAPI/.NET deployment is incomplete.

Evidence: `data_generation/seed_database_fixed.py`, `graph/graph_builder.py`, `models/gnn.py`, `ga/fitness.py`, `ga/rolling_horizon.py`, `run_authoritative_comparison.py`, `agent/`, `validation/schedule_validator.py`, `frontend/dashboard.py`, `api/main.py`.

## 3. Data and Dataset Provenance

**Status: VERIFIED AND ACTIVE.**

- Live database: `garment_fyp_db`
- Live dataset UUID: `9fa2915b-5f28-4463-8e4f-d21b5b57dc8d`
- Origin staging DB: `garment_fyp_db_seed_v2_20260914_142905`
- Staging UUID: `b72b7231-c2bf-4e82-b952-5609fdf853ec`
- Promotion journal: `94d02cf41c924991a588`
- Backup: `promotion_backup_94d02cf41c924991a588`
- 1,029 orders; 176,187 schedule rows; 3,000 machines; 9,000 employees; 6 factories

Different staging/live UUIDs are expected because promotion records a new live identity.

Company-derived/structurally grounded inputs include PO/order-book material, routing/process relationships, quantities and SMV-related values, factory/line/workstation structure, machine configuration, workforce configuration, and operational relationships. Sources include `grid.xls`, `Plan.xlsx`, `PPC-1.xlsx`, and `WorkStation.xlsx`.

Generated/simulated content includes 890 proxy/resampled groups, generated non-preserved attributes, planned lot dispatches where generated, all actual execution timestamps, variance, queue propagation, breakdown realization, repair time, actual segments/status, and GNN labels. The 1,029 records are not 1,029 distinct historical orders.

**NEW ISSUE:** the current seeder source hash differs from the hash recorded at promotion. This does not invalidate existing rows, but current source is not byte-identical to the generation source. Inspected input spreadsheets remained hash-matched.

Evidence: `data_generation/seed_database_fixed.py`, `DATA_AUDIT.md`, `outputs/integration_audit/authoritative_staging_provenance.json`, `outputs/promotions/94d02cf41c924991a588.json`, live DB metadata/counts.

## 4. Production Simulator

**Phase A - VERIFIED AND ACTIVE.** `build_frozen_planned_schedule()` assigns identities, resources, planned times, and planned productive segments using planning state/RNG. Duration is:

`duration_calibration_factor * stage_quantity * SAM / (worker_efficiency / 100)`

The factor is derived from PPC output/work-hours evidence and checked against scheduler configuration.

**Phase B - VERIFIED AND ACTIVE.** `simulate_actual_from_frozen_plan()` uses `random.Random(actual_seed)` and never reassigns Phase-A identities. Readiness is max(actual machine availability, employee availability, predecessor completion, frozen planned_start), so planned start is an intentional hard floor.

Execution multiplier is clipped normal: mean 1.0, SD 0.15, range [0.65,1.6]. Prior breakdown count adds 1% to mean per count, capped at 8%. Leave could add 10% but is inactive in current data. Queue delay widens SD up to 50%. Dynamic breakdown occurs after 1,200 runtime minutes with probability 0.15; repair is 120-360 minutes and consumes no technician.

Same planning seed 42 with actual seeds 42 and 999 yielded 176,187 identical planned rows, about 176,138 changed actual ends, and about 176,145 changed actual segments. Exact Phase-B replay had no first divergence.

The active graph has 7,491 positive delay labels of 8,211 operations (91.2313%). This is an operation-level synthetic stress regime dominated by cascading readiness/queues, not observed factory lateness and not customer-order lateness.

Evidence: seeder/calendar code and `outputs/integration_audit/full_staging_plan_independence.json`, `seed_plan_actual_invariance_proof.json`, `dgp_replay_first_divergence.json`, `dgp_planned_start_floor_counterfactual.json`, `dgp_delay_sanity_audit.json`.

## 5. Worker and Shift Semantics

**VERIFIED AND ACTIVE.**

- Morning: 06:00 <= time < 14:00
- Evening: 14:00 <= time < 22:00
- Night: time >= 22:00 OR time < 06:00

Weekly shift is `(whole weeks since Monday 2024-01-01 + rotation_offset) mod 3`. Rest weekday is `(weeks since epoch + rest_day_offset) mod 7`, Monday=0. A crew advances one shift and one rest weekday weekly. Offsets are round-robin. Batch calendars override personal schedules; unbatched workers fall back to Work_Schedules.

The decoder uses same-worker pause/resume. The same worker/crew remains assigned; work occurs only in common valid windows; the machine stays occupied continuously across pauses; successors wait for completion. No handover exists. Multi-worker crews require a complete eligible crew and identical segments are reserved per employee. Future reservations carry between windows.

Idle qualified workers are considered subject to factory, skill, calendar, reservation, and common-window constraints. Ranking uses efficiency and busy minutes, with priority affecting the trade-off. A soft idle-labor term rewards utilization against the machine-limited ceiling (`IDLE_LABOR_WEIGHT=480`), not blindly against all workers.

Further improvement is possible because worker identity is greedily decoded rather than evolved. Extra labor helps only when it changes crew availability; surplus beyond usable machines cannot reduce makespan.

Evidence: `utils/shift_rotation.py`, `utils/work_calendar.py`, `ga/worker_allocation.py`, `ga/fitness.py`, worker/shift/carry tests, and `worker_segment_carry_forward.json`.

## 6. Machine and Maintenance Semantics

Machines are restricted by capability/type, factory/line ownership, usable status, machine-count gene, availability, and commitments. Selection is deterministic. The machine is reserved continuously across setup, work, and worker pauses.

Simulator breakdown inserts downtime and propagates queues. Live breakdown handling persists state, assesses factory-scoped alternatives, applies fuzzy triage, and selects no action, local patch, or full reschedule.

**PLANNED / NOT IMPLEMENTED:** no maintenance technician is selected/reserved; repair consumes no worker capacity. A Mechanic role is not wired into repair. No complete breakdown -> technician task -> repair segments -> health/status recovery lifecycle exists.

Machine graph features are status one-hot plus normalized RPM. Health/efficiency-like DB fields exist, but HealthScore/Avg_Efficiency_Rate are not active across scheduler eligibility, breakdown probability, fuzzy logic, and repair updates. Runtime/RNG drives simulated breakdown; fuzzy logic uses slack and impact.

**NEW ISSUE:** breakdown NO_ACTION can leave affected future work assigned to a broken machine.

Evidence: `database/queries.py`, `utils/machine_reserve.py`, `ga/schedule_utils.py`, `ga/fitness.py`, `agent/event_handler.py`, `agent/impact_assessor.py`, machine feature code.

## 7. Heterogeneous Graph

**VERIFIED AND ACTIVE.**

| Node | Count | Feature width |
|---|---:|---:|
| machine | 3,000 | 5 |
| worker | 9,000 | 4 |
| operation | 8,211 | 12 |

Operation schema v2: `sam_norm`, `qty_norm`, `machine_parallelism_norm`, `deadline_budget_norm`, `decisive_lot_breakdown_count_norm`, `mean_queue_delay_norm`, `routing_step_norm`, `steps_remaining_norm`, `pool_occupancy_norm`, `mean_breakdown_count_norm`, `has_dispatch_context`, `has_assignment_history`.

Edges: 7,253 precedence; 4,038,356 factory-scoped contention; 164,578 each for machine->operation, operation->machine, worker->operation, and operation->worker relations.

Split: 6,568 training operations/767 orders; 1,643 validation operations/191 orders; seed 42; 20% validation; split by whole order. Fingerprint: `3ea855a9b5e2995986cb7ee974f2ef69525eb1b603e08b933ac73cc72500e744`.

Evidence: `graph/feature_schema.py`, `node_features.py`, `edge_builder.py`, `graph_builder.py`, `outputs/integration_audit/graph_profile_new_staging.json`, `outputs/split_manifest.json`.

## 8. GNN Architecture, Targets and Current Model Status

**VERIFIED AND ACTIVE.** The heterogeneous multi-task GraphSAGE uses two branches: a dedicated delay projection plus three GraphSAGE layers, and a separate completion/priority projection plus three layers. Heterogeneous aggregation is sum. Hidden width is 64, dropout 0.3, operation-type embedding width 4. Each head is 64->32->1. Delay returns logits; completion uses Softplus and a 1,000-minute scale; priority uses sigmoid.

### Delay head

Target is positive when:

`max(actual_end across lots) - max(planned_end across lots) > 0.20 * mean(planned lot elapsed duration)`

Only fully observed operations are valid. Current data has all 8,211 valid. Loss is BCEWithLogitsLoss with training-only `pos_weight=510/6058=0.0841862000660284`; below one is correct because positives are the majority. Evaluation includes AP, ROC-AUC, precision/recall/F1, confusion, thresholds, probability distribution, and calibration/prior correction.

Scheduler wiring: delay adds `delay_probability * 50`, averaged over operations. With risk buffer disabled, the same fixed operation set gives the same sum regardless of sequence/assignment. It is candidate-invariant.

### Completion head

Target is planned operation elapsed span: latest planned lot end minus earliest planned lot start, in minutes. It is not actual execution completion. Missing spans are masked; current fallback count is zero. Loss is Huber(delta 0.4) after scaling both sides by 1,000. **BYPASSED:** GA does not consume this head.

### Priority head

Target is `1 - percentile(deadline slack)`, using operation planned start and falling back to release date. A 101-point curve is derived only from training operations and frozen in the split manifest. Loss is MSE. Priority enters the position-weighted GA reward and worker ranking; it is candidate-dependent because sequence position changes.

### Training/artifact identity

- 150/150 epochs completed at 2026-09-15 00:46:11.
- Epoch 150 validation loss 0.4349 (delay 0.0480, completion 0.3799, priority 0.0070).
- Best checkpoint: `outputs/checkpoints/gnn_multitask_latest.pth`.
- Best epoch: inferred as 133 from write time/log alignment; not explicit checkpoint metadata.
- SHA-256: `039f1ab3ae39c7b76585fadd321db89429c216a728cafb8c822eae6446d95c6d`.
- Preprocessing binds that hash, schema v2/width 12, breakdown scale 1.459524381455681, and pos_weight.
- Split manifest binds checkpoint hash and graph fingerprint.

**NEW ISSUE:** the package does not cryptographically bind live dataset UUID `9fa2915b-...`; the link is evidential through the matching corrected graph profile.

### Current held-out metrics

Validation: 1,643 operations; 1,433 positive/210 negative; prevalence 0.872185.

| Metric | Value |
|---|---:|
| Delay accuracy | 0.9124 |
| Delay precision | 0.9931 |
| Delay recall | 0.9058 |
| Delay F1 | 0.9474 |
| Delay Average Precision | 0.9962 |
| Delay ROC-AUC | 0.9728 |
| Completion MAE | 1,130.22 min |
| Completion RMSE | 1,455.41 min |
| Completion R2 | 0.4993 |
| Completion train-mean baseline MAE | 1,680.76 min |
| Priority MAE | 0.06639 |
| Priority RMSE | 0.08748 |
| Priority R2 | 0.9169 |
| Priority train-mean baseline MAE | 0.26977 |

Metrics were recomputed read-only through `models/evaluate.py`; the August `evaluation_report.txt` is stale.

Evidence: `models/gnn.py`, `train.py`, `evaluate.py`, `graph/labels.py`, checkpoint preprocessing JSON, split manifest, `logs/gnn_training.log`, `gnn_output_wiring.json`.

## 9. Genetic Algorithm and Objective

**VERIFIED AND ACTIVE.** Chromosome is OS (operation sequence) + LA (line assignment) + MC (machine count). One authoritative decoder enforces routing, release, ownership, line/factory consistency, machine capability/status/commitments, worker factory/skill/calendar/commitments, full crew availability, same-worker segments, continuous machine occupancy, and successor readiness. Fitness and export consume the same phenotype; post-export rerouting is retired.

Lower-is-better objective:

- makespan hours
- plus mean `delay_probability * 50`
- minus mean `priority * sequence_position_weight * 100`
- plus line-load standard deviation hours
- plus `3 * average additional tardiness hours`
- plus `24 * fraction of scored orders late`
- plus `480 * (1 - utilization versus engageable-machine ceiling)`

Weights: ALPHA/GAMMA/DELTA/EPSILON = 1; ZETA=3; service level=24; idle labor=480.

Optimized tardiness is schedule-added tardiness where pre-origin deadlines are clamped. It is not total customer tardiness; reporting must separately use original deadlines.

Authoritative runner settings: population 50, generations 50, crossover 0.7, mutation 0.2, elite fraction 0.04, immigrant fraction 0.10, 400 polish evaluations. The scheduler method's local default of 20 generations is overridden by the runner. Authoritative rolling comparison uses lookahead=commit, currently 300-order chunks/four windows for 1,029 orders. Wider lookahead is a surrogate-score mode.

Worker-aware benchmark is about 2.02 seconds per 2,580-operation evaluation, about 16.23x old worker-blind speed. Full multi-seed research is offline-feasible, not strong evidence for literal full-scale real time.

Evidence: `ga/fitness.py`, `scheduler.py`, `chromosome.py`, `worker_allocation.py`, `rolling_horizon.py`, `run_authoritative_comparison.py`, phenotype parity and evaluator benchmark artifacts.

## 10. Baseline Schedulers

**VERIFIED AND ACTIVE in code; final results incomplete.**

Baselines are FIFO, EDF, Critical Ratio, Urgency Tiered, and Random Repaired. All use the same work-aware line assignment and machine-count policy so the comparison isolates sequencing. They pass through the same worker-aware phenotype, calendars, commitments, objective, and export as GA.

No current final winner may be claimed. Existing reports predate at least one of the corrected DGP, final model, or current runner state; the stale snapshot risk must be closed first.

Evidence: `ga/heuristics.py`, `ga/rolling_horizon.py`, `run_authoritative_comparison.py`, baseline parity tests, `authoritative_arm_parity.json`.

## 11. Fuzzy Logic

**VERIFIED AND ACTIVE for disruption triage.**

Inputs:

1. deadline slack in days;
2. estimated added impact in hours;
3. crisp `manager_forced` override above the fuzzy rules.

Slack anchors come from derived current-dataset quantiles, approximately p25=-12, p50=27, p75=88 days. Urgent is a left shoulder full through p25 and zero at p50; moderate is triangular p25/p50/p75; relaxed is a right shoulder zero at p50 and full from p75. Negative slack is not clamped.

Impact is clamped non-negative. Negligible is full at 0 and zero at 4 hours; moderate is a triangle with nominal support -4 to 36 and peak 16 (effective support starts at 0); severe is zero through 24, rises to full at 48, then stays full.

Rules:

1. negligible impact -> NO_ACTION
2. moderate impact AND relaxed slack -> LOCAL_PATCH
3. moderate impact AND moderate slack -> LOCAL_PATCH
4. moderate impact AND urgent slack -> FULL_RESCHEDULE
5. severe impact -> FULL_RESCHEDULE

AND is min; same-consequent aggregation is max. The output is categorical, not centroid-defuzzified. Conservative tie order is FULL_RESCHEDULE, LOCAL_PATCH, NO_ACTION. Manager forced bypasses inference to FULL_RESCHEDULE. The same rule base is used rather than per-disruption-type fuzzy systems.

Evidence: `agent/fuzzy_logic.py`, `tests/test_fuzzy_logic.py`, `outputs/derived_constants.json`.

## 12. Disruption Agent and Rescheduling

**PARTIAL.** Breakdown monitoring persists state, assesses factory-scoped alternatives, and maps fuzzy tier to no action, local patch, or full reschedule. Critical persistence/lookup failures abort rather than silently continuing.

**NEW ISSUE:** NO_ACTION can preserve future assignments on a broken machine. **NEW ISSUE:** local patch directly edits schedule rows without passing through authoritative worker-segment construction and independent validation.

Rush/priority events can invoke the decision engine and combine with in-memory manager priority, but no durable priority domain model exists. Worker absence event semantics are shallower than machine breakdown; current seeded Phase B sets leave false, so absence is not current GNN evidence. Material shortage remains shallow: event logging plus broad/full rescheduling, not time-phased material feasibility.

Evidence: `agent/monitor.py`, `event_handler.py`, `impact_assessor.py`, `decision_engine.py`, factory-scope/disruption tests and artifacts.

## 13. Independent Validation

**VERIFIED AND ACTIVE in code.** `validation/schedule_validator.py` independently checks operation coverage, duplicates/unexpected operations, timestamps, releases, ownership, line/factory consistency, machine capability/factory/overlaps/commitments, worker count/factory/skill/calendar, explicit segments, productive minutes, worker overlaps, routing precedence, sequence-based completion, and tardiness reconstruction. Missing explicit segments invalidates segmented execution. It does not call the GA decoder.

Current collection is exactly 862 tests. Orientation did not rerun the expensive full suite. Historical pre-reseed JUnit records 862 passed, 0 failed, 0 skipped in 373.468 seconds. Pytest cache failure IDs are stale, not current failure evidence.

Evidence: validator code, authoritative validator/phenotype/pause/carry tests, `outputs/integration_audit/pre_reseed_full_suite.xml`.

## 14. Database Promotion / Recovery

**VERIFIED AND ACTIVE.** Promotion supports staging validation, dry run, prepared schema construction, locking, atomic table rename, journal, rollback/recovery, and post-activation validation. Promotion `94d02cf41c924991a588` activated the named staging DB, created backup `promotion_backup_94d02cf41c924991a588`, and recorded live UUID `9fa2915b-...`. Live and backup validation passed at activation.

Evidence: `database/promote_dataset.py`, `PROMOTION.md`, promotion tests, journal JSON, and dataset validation directories.

## 15. API and Portal Integration

**PARTIAL.** MySQL integration is real. A Streamlit dashboard exists. `api/main.py` is not a substantive production FastAPI surface. No .NET solution/project/C#/Razor files were found. Manager override is Streamlit session/in-memory state, not durable across restarts. The monitor is a polling prototype, not a proven production service.

The defensible claim is an offline/research scheduler with Python monitoring and Streamlit decision support, not a completed FastAPI + .NET deployment.

Evidence: `api/`, `frontend/dashboard.py`, repository inventory, `agent/monitor.py`.

## 16. Verified Historical Fixes

1. **VERIFIED AND ACTIVE:** Phase A excludes actual clocks/RNG/outcomes; Phase B preserves frozen identities/plans.
2. **VERIFIED AND ACTIVE:** full-scale independence over 176,187 planned rows.
3. **VERIFIED AND ACTIVE:** same-worker segmented pause/resume.
4. **VERIFIED AND ACTIVE:** fitness/export use one phenotype; post-export reroute retired.
5. **VERIFIED AND ACTIVE:** productive worker reservations carry across windows.
6. **VERIFIED AND ACTIVE:** baseline semantic parity with GA.
7. **VERIFIED AND ACTIVE:** machine commitments enter rolling scheduling state.
8. **VERIFIED AND ACTIVE:** independent validator rejects missing segments/resource violations.
9. **VERIFIED AND ACTIVE:** disruption alternatives/reserves are factory-scoped.
10. **VERIFIED AND ACTIVE:** persistence/lookup failure aborts instead of rescheduling stale state.
11. **VERIFIED AND ACTIVE:** labels use actual-vs-planned timing, masks, training-only priority cutpoints, and checkpoint-bound preprocessing.
12. **VERIFIED AND ACTIVE:** live activation and rollback backup were journaled and validated.

The paired staffing replay remains valid historical evidence: segmentation reduced unfilled worker minutes to zero, while the reservation-key fix alone changed little. The apparent roughly 60% shortage was a continuity-rule artifact, not measured physical factory labor shortage.

## 17. Current Authoritative Results

Trustworthy within scope:

- Active dataset and promotion identity/counts.
- Two-phase independence over 176,187 rows.
- Exact Phase-B replay without divergence.
- Graph: 8,211 operations, 4,038,356 contention edges, fingerprint `3ea855...`.
- Split: 6,568/1,643 operations, order-grouped.
- Synthetic operation delay prevalence: 7,491/8,211 = 91.2313%.
- 150-epoch training completion.
- Internally consistent checkpoint/preprocessing/split/graph hashes.
- Current held-out GNN metrics in Section 8.
- Same-worker phenotype and fitness/export parity.
- Baseline semantic parity.
- Historical pre-reseed full-suite checkpoint: 862 passed.
- Promotion and backup validation at activation.
- Worker-aware evaluator runtime benchmark.

There is **no current authoritative optimizer-winner result** after all present corrections.

## 18. Superseded Results That Must Not Be Used

- Pre-corrected-DGP GNN metrics and the August standing evaluation report.
- Historical five-seed GA run `28d5edb4-7251-414e-9116-9afe27c527eb` as final evidence.
- Offspring audit `bcd26e4c-2be1-44f5-a446-4765487a7e4b` as universal evidence against crossover.
- Old ALNS comparisons as proof ALNS universally fails.
- Workload lower bounds as achievable improvement estimates.
- Roughly 60% unfilled minutes as real staffing shortage.
- Worker-blind baseline comparisons.
- Pre-commitment/ownership/final-worker-semantics optimizer results.
- Claims that all three GNN heads improve scheduling.
- Effective-deadline tardiness treated as customer tardiness.
- Any old-paper result not regenerated under current identities.

## 19. Current Open Problems

1. **NEW ISSUE:** comparison snapshot may be stale relative to corrected data/model.
2. **NEW ISSUE:** model package lacks cryptographic live dataset UUID binding.
3. **NEW ISSUE:** current seeder hash differs from promoted provenance.
4. **NEW ISSUE:** breakdown NO_ACTION may preserve work on unavailable machine.
5. **NEW ISSUE:** local patch bypasses authoritative phenotype/segments/validator.
6. **PARTIAL:** manager override is in-memory and not auditable/durable.
7. **PLANNED / NOT IMPLEMENTED:** maintenance technician scheduling.
8. **PARTIAL:** worker assignment is greedy; GA cannot explore worker identity directly.
9. **BYPASSED:** completion output has no operational consumer.
10. **BYPASSED:** delay term is candidate-invariant under current settings.
11. **PARTIAL:** worker absence and material shortage handling are shallow.
12. **PARTIAL:** API/.NET deployment is absent/stubbed.
13. **INCOMPLETE:** no full-suite record after all current working-tree changes.
14. **INCOMPLETE:** no final current multi-seed GA/baseline experiment.
15. **RUNTIME RISK:** worker-aware evaluation weakens literal real-time claims.

## 20. Planned Enhancements That Are Not Yet Implemented

### Manager priority override

**PARTIAL, not production-ready.** Existing `manager_forced_orders` is an in-memory/session prototype. A durable implementation should persist order ID, level, actor, reason, timestamp, active/version state; expose API/UI; load it into every snapshot; pass it to fuzzy triage and GA; and preserve it in run provenance.

### Better use of qualified workers

**PARTIAL.** Current ranking already considers eligible idle workers, efficiency, busy minutes, calendars, skills, and priority. Future work should first measure avoidable idle time conditional on machine availability/demand, then compare greedy crew assignment with bounded alternatives. Unavoidable machine-capped idle labor must not be mislabeled as scheduling failure.

### Maintenance technicians

**PLANNED / NOT IMPLEMENTED.** Repair should become a task requiring technician skill, factory ownership, shift/rest calendar, commitments, productive repair segments, and persistent machine downtime/health transitions. Agent choice could then compare wait, reroute, local repair, and full reschedule using severity, repair estimate, technician availability, alternative capacity, deadlines, manager urgency, and affected count.

## 21. Handoff-vs-Code Discrepancies

1. Handoff says FastAPI + .NET portal; repository shows Streamlit, FastAPI stub, no .NET project. **PARTIAL.**
2. Manager priority was described as planned; current working tree has an in-memory prototype. It is neither absent nor complete. **PARTIAL.**
3. Handoff last saw training near epoch 77; current logs prove 150 completed. **RESOLVED.**
4. Approximate graph values are confirmed exactly: 8,211 operations and 4,038,356 contention edges.
5. Long `graph/labels.py` comments contain older prevalence narratives inconsistent with the promoted 91.23% regime. **DOCUMENTATION DRIFT.**
6. Generic 24-hour constants in `utils/config.py` are not worker-shift truth; `shift_rotation.py`/`work_calendar.py` are authoritative.
7. A seeder comment suggests operator shift changeover, while frozen employee identity plus segments implement same-worker pause/resume. **DOCUMENTATION DRIFT.**
8. Model artifacts are internally bound but not live-UUID bound. **NEW ISSUE.**
9. The integration ledger predates newer model/status artifacts and is partly superseded.

## 22. Risks Before Integration Freeze

1. Freeze exact code tree, dataset UUID, graph fingerprint, split, checkpoint/preprocessing hashes, configuration, and scheduling snapshot.
2. Resolve stale snapshot ambiguity.
3. Make every breakdown tier feasibility-safe, especially NO_ACTION.
4. Route local patch through authoritative worker/machine/segment validation or disable it.
5. Add a manifest binding dataset UUID, graph, checkpoint, preprocessing, code/config, and snapshot.
6. Reconcile seeder source hash through archival evidence; do not rewrite history.
7. Run full suite on frozen tree and save JUnit/environment identity.
8. Add end-to-end disruption tests for persistence, broken-machine assignments, segments, ownership, and rollback.
9. Decide whether manager priority is excluded or completed before freeze.
10. Qualify/complete API and portal claims.
11. Profile evaluator without weakening semantics.

## 23. Risks Before Final Paper Submission

1. No final current-formulation multi-seed GA-vs-baseline experiment.
2. No ablation proves scheduling benefit from a GNN head.
3. Delay prevalence is synthetic operation-level stress, not factory/order lateness.
4. Completion target is planned span, not real execution completion.
5. Priority is constructed slack percentile, not manager priority.
6. Effective tardiness must be separated from customer tardiness.
7. Old GA/ALNS/lower-bound/GNN results must remain superseded.
8. Runtime supports offline research, not an unqualified full-scale real-time claim.
9. Provenance must distinguish 139 genuine groups, proxies, generated plans, and simulated outcomes.
10. Final tables must state hashes, seeds, windows, weights, and validation.
11. Use multiple seeds, paired comparisons, dispersion/confidence intervals, and effect sizes.
12. Fuzzy anchors are reasoned/current-dataset quantiles, not validated causal thresholds.
13. Deployment claims must match Streamlit/stub API/no-.NET reality.

## 24. Exact Answers to the 48 Questions

1. **Current end-to-end flow:** company inputs -> mixed real/proxy order book -> two-phase simulator -> validation/promotion -> heterogeneous graph/split -> multi-task GraphSAGE -> scheduler state -> worker-aware rolling GA/baselines -> export/independent validation -> monitoring/fuzzy triage -> no action/local patch/full reschedule -> Streamlit. FastAPI/.NET are incomplete.

2. **Exact shifts:** Morning 06:00-14:00, Evening 14:00-22:00, Night 22:00-06:00; end-exclusive.

3. **Weekly rotation:** `(weeks since 2024-01-01 + rotation_offset) mod 3`; one shift advance weekly per crew.

4. **Rest days:** `(weeks since epoch + rest_day_offset) mod 7`; one rotating weekday off per crew, round-robin staggered.

5. **Is same-worker pause/resume active?** Yes, **VERIFIED AND ACTIVE**.

6. **Can another worker take over unfinished work?** No. Handover is not implemented.

7. **Multi-worker crews:** full eligible crew required; work advances only in common valid windows; identical segments reserved for each member; shortage rejects the phenotype.

8. **Are idle qualified workers considered?** Yes, within factory, skill, calendar, reservation, and common-window constraints.

9. **Is workforce utilization intentional?** Yes. Feasibility produces indirect use and a soft idle-labor objective explicitly rewards use against the machine-limited ceiling.

10. **Could idle workers be used better?** Yes. Worker identity is greedily decoded, not evolved; globally better crew choices may exist. Measure avoidable idle time first.

11. **Machine assignment:** line/machine-count genes plus deterministic selection among factory/line-owned, capability-compatible, usable, commitment/availability-feasible machines.

12. **When a machine breaks:** simulation inserts repair delay and propagates queues; live agent persists event/status, evaluates scoped alternatives, fuzzy-triages, then no action/local patch/full reschedule.

13. **Explicit maintenance technician today?** No.

14. **Does repair consume worker resource?** No.

15. **How does a repaired machine return?** Simulation waits elapsed repair delay then re-enters readiness. Live handling uses status/downtime persistence; no technician-completion workflow exists.

16. **Does machine health change?** No complete health degradation/recovery loop. Status/counts change in limited contexts; health is not comprehensively updated.

17. **Exact fuzzy inputs:** slack days, estimated added impact hours, crisp `manager_forced`.

18. **Memberships/breakpoints:** slack quantiles about -12/27/88 days: urgent left shoulder p25->p50, moderate triangle p25/p50/p75, relaxed right shoulder p50->p75. Impact: negligible 0->4h; moderate triangle nominally -4/16/36h after non-negative clamp; severe 24->48h.

19. **Rule base:** negligible->NO_ACTION; moderate+relaxed->LOCAL_PATCH; moderate+moderate slack->LOCAL_PATCH; moderate+urgent->FULL_RESCHEDULE; severe->FULL_RESCHEDULE.

20. **Fuzzy output:** categorical NO_ACTION, LOCAL_PATCH, or FULL_RESCHEDULE, with reason and rule strengths.

21. **Action conversion:** event handler logs/no-ops, calls `apply_local_patch`, or invokes decision-engine rescheduling. Manager forced bypasses to full reschedule.

22. **Can manager mark urgent now?** Partially. Streamlit/in-memory `manager_forced_orders` can; no durable DB/API implementation.

23. **How does it propagate?** Combined with priority orders, can force fuzzy full reschedule, and applies a scheduler priority multiplier. It is session/process scoped.

24. **Where should a durable override integrate?** Persisted order priority state, run snapshot/provenance, scheduler input, fuzzy triage, GA priority or must-go-first constraint, API, portal, and audit log.

25. **Rush orders:** event/decision-engine rescheduling and priority treatment, without a complete durable priority-state workflow or separate validated fuzzy system.

26. **GraphSAGE node types:** machine, worker, operation.

27. **Feature dimensions:** machine 5, worker 4, operation 12, plus learned operation-type embedding width 4 inside the model.

28. **GNN targets:** binary operation delay, planned operation elapsed span in minutes, and deadline-slack percentile priority.

29. **Completion target:** latest planned end minus earliest planned start over all operation lots, in minutes; missing spans masked.

30. **Priority target:** `1 - empirical percentile(deadline slack)` against a 101-point training-only curve; planned start reference, release-date fallback.

31. **Delay label:** positive when `max(actual_end)-max(planned_end)` exceeds 20% of mean planned lot elapsed duration, only for fully observed operations.

32. **Did 150 epochs complete?** Yes, 2026-09-15 00:46:11.

33. **Best checkpoint:** `outputs/checkpoints/gnn_multitask_latest.pth`, SHA-256 `039f1ab3ae39c7b76585fadd321db89429c216a728cafb8c822eae6446d95c6d`; best epoch inferred as 133.

34. **Final new GNN metrics:** delay AP 0.9962, ROC-AUC 0.9728, precision 0.9931, recall 0.9058, F1 0.9474, accuracy 0.9124; completion MAE 1,130.22 min, RMSE 1,455.41 min, R2 0.4993; priority MAE 0.06639, RMSE 0.08748, R2 0.9169.

35. **Are hashes consistent?** Checkpoint, preprocessing, split, schema, and graph are consistent. Live dataset UUID is not cryptographically bound, and current seeder source hash differs from promotion provenance. Therefore model package is internally consistent but end-to-end dataset binding is incomplete.

36. **Which heads affect GA?** Priority affects ranking; delay adds a scalar but is candidate-invariant under current settings; completion is unused.

37. **Where does each affect GA?** `ga/fitness.py`: delay mean penalty; priority position reward and worker ranking. `ga/scheduler.py` prepares predictions/manager multiplier. Completion has no consumer.

38. **Candidate-dependent?** Priority yes; delay no for fixed operation set with risk buffer disabled; completion no effect.

39. **Final GA objective:** committed-set makespan + mean delay penalty - position-weighted priority reward + line-load imbalance + average additional tardiness and late-order fraction + idle-labor penalty.

40. **Objective weights:** makespan 1; delay 1 with factor 50; priority 1 with factor 100; load balance 1; average tardiness 3; late-order fraction 24; idle labor 480.

41. **Final GA parameters:** population 50, generations 50, crossover 0.7, mutation 0.2, elites 4%, immigrants 10%, polish 400 evaluations, authoritative lookahead=commit (300 orders currently).

42. **Baselines:** FIFO, EDF, Critical Ratio, Urgency Tiered, Random Repaired.

43. **Identical worker semantics?** Yes in the authoritative path: same decoder, calendars, commitments, objective, and export.

44. **Final/trustworthy results:** dataset/promotion identity, plan/actual independence and replay, graph/split/fingerprint, current DGP prevalence, completed model/metrics, phenotype/export parity, baseline semantic parity, validator behavior, and runtime benchmark.

45. **Superseded results:** old GNN metrics, old five-seed GA/offspring studies, ALNS trials, worker-blind baselines, misleading lower bounds, and pre-final-worker/DGP comparisons.

46. **Incomplete before Integration Freeze:** snapshot/data/model binding, disruption no-action/local-patch feasibility, provenance reconciliation, manager-priority scope decision, current full test run, end-to-end manifest, and deployment claim resolution.

47. **Incomplete before final paper results:** freeze, current paired multi-seed GA/baseline runs, GNN ablations, statistics, final validator evidence, runtime reporting, and regeneration of all tables/figures from bound outputs.

48. **Risks that could invalidate final experiment:** stale snapshot, data/model mismatch, unbound provenance, infeasible disruption patches, old artifact reuse, unequal arm state, wider-lookahead surrogate misuse, wrong tardiness definition, incomplete validation, or insufficient multi-seed evidence.

## 25. Recommended Next Actions, Ordered by Dependency

1. **Freeze identities, not behavior:** select exact code tree, live UUID, graph fingerprint, split, checkpoint/preprocessing hashes, configuration, and scheduling snapshot; record a read-only run manifest.
2. **Resolve stale snapshot risk:** verify or rebuild the comparison snapshot only after explicit implementation approval.
3. **Close disruption feasibility blockers:** ensure breakdown no-action cannot leave work on an unavailable machine and require local repair to use equivalent worker/machine/segment validation.
4. **Decide freeze scope:** explicitly exclude durable manager override and maintenance technicians as future work, or implement/test them before freezing.
5. **Strengthen provenance binding:** bind dataset UUID and validation/provenance artifact hashes to model/experiment manifests; preserve historical promotion records.
6. **Run the full suite on the frozen tree:** save JUnit, environment/dependency versions, code identity, and count; separately gate end-to-end disruption/database behavior.
7. **Perform a no-optimization dry validation:** load model/snapshot, reconstruct graph/scheduler state, and verify hashes/validator inputs without launching GA.
8. **Ablate GNN wiring:** compare no-GNN, priority-only, delay-only, and current wiring. Do not infer scheduling benefit from predictive metrics.
9. **Run final paired experiments:** same frozen state, seeds, windows, commitments, objective, decoder, and validator for GA and all five baselines.
10. **Report statistics and operations:** paired seed/window deltas, dispersion/confidence intervals, customer versus schedule-added tardiness, feasibility, runtime, and resource utilization.
11. **Regenerate paper assets only from final manifests:** every table/figure/claim should cite current identities and synthetic provenance.
12. **Only after owner review, implement enhancements:** durable priority API/UI, maintenance resources, stronger worker assignment search, performance work, and genuine FastAPI/.NET integration.

## Evidence Index

- Simulator/provenance: `data_generation/seed_database_fixed.py`, `data_generation/simulation_calendar.py`, `DATA_AUDIT.md`.
- Calendar/workers: `utils/shift_rotation.py`, `utils/work_calendar.py`, `ga/worker_allocation.py`.
- Graph/labels: `graph/feature_schema.py`, `graph/node_features.py`, `graph/edge_builder.py`, `graph/graph_builder.py`, `graph/labels.py`.
- Model: `models/gnn.py`, `models/train.py`, `models/evaluate.py`, `logs/gnn_training.log`.
- Scheduler/baselines: `ga/fitness.py`, `ga/scheduler.py`, `ga/rolling_horizon.py`, `ga/heuristics.py`, `run_authoritative_comparison.py`.
- Agent/fuzzy: `agent/fuzzy_logic.py`, `agent/event_handler.py`, `agent/impact_assessor.py`, `agent/decision_engine.py`, `agent/monitor.py`.
- Validation/promotion: `validation/schedule_validator.py`, `database/promote_dataset.py`, `PROMOTION.md`, targeted tests.
- Identity/artifacts: `outputs/split_manifest.json`, `outputs/checkpoints/gnn_multitask_latest.pth.preprocessing.json`, `outputs/integration_audit/`, `outputs/promotions/94d02cf41c924991a588.json`, `outputs/dataset_validations/`.
- Deployment: `api/`, `frontend/dashboard.py`, repository inventory.

## Stop Condition

This report ends the read-only orientation stage. No fixes, training, GA/ALNS/CP-SAT experiments, reseeding, migrations, promotions, artifact regeneration, fuzzy changes, manager-priority implementation, maintenance implementation, or workforce-utilization changes were performed. Further work requires an explicit new implementation goal from the project owner.
