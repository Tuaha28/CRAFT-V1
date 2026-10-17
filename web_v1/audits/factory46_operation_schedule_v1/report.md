# Factory 4 / Factory 6 operation-level TEST scheduling

Run date: 2026-10-14. These are bounded, captured-input TEST experiments, not production schedules. Each factory was loaded and solved separately. The source snapshot and normalized snapshot hashes were checked against the pinned readiness manifests before building any scheduling input.

## Input and assumptions

Pinned capture: `factory_source_integration_v1/capture_20261012T223420800478Z`.

| Factory | Operations | Completed / excluded | Partial / production-blocked | Unstarted in fully unstarted routes | Raw source SHA-256 prefix | Normalized snapshot hash prefix | TEST scenario hash prefix |
|---|---:|---:|---:|---:|---|---|---|
| 4 | 94 | 28 | 3 | 63 | `101e8d598596` | `7c4b5d10cd91` | `2bad06d2a519` |
| 6 | 85 | 19 | 10 | 56 | `491eab8556c2` | `68832344d670` | `4cd708abeabb` |

No crew-size requirement is authoritative in these captures. The TEST-only execution contract therefore explicitly assumes one worker, one machine, one source-skilled worker, a same-batch-line worker/machine pair, a 900-second setup, no machine-count speedup, and productive duration `quantity × SMV × 60 / worker efficiency`. Worker availability follows the captured batch rotation/rest-day rule. Machines are treated as available only when their captured status is Active; there is no authoritative machine shift/maintenance calendar in this snapshot. These assumptions are versioned and hashed per factory. They are not evidence of factory policy.

All operations with partial execution were excluded. Captured fixed holds were conservatively quarantined to the bounded TEST horizon: one hold in Factory 4 and three in Factory 6. This does not claim those holds last that long in the factory. Completed operations were excluded and not rescheduled.

The remaining partial-operation rows do not all carry a complete worker/machine hold ledger. Their unknown resource occupancy cannot be reconstructed from this snapshot. Therefore validation proves consistency against the holds actually captured in the TEST overlay, not against unknowable real in-flight occupancy; this is a further reason production admission remains blocked.

The capture has no approved future schedule export. The runs therefore protect captured fixed holds and assignments from earlier accepted TEST windows, but cannot prove conflict closure against an absent approved-schedule state or reserve rights for unscheduled future demand. `outside_uncommitted_future_conflict_closure` remains false. “Unstarted in fully unstarted routes” means eligible only for this explicitly hypothetical TEST admission; source readiness still reports those operations as requiring evidence for production.

## Results

| Run | Selected | Accepted | Deferred after failed windows | Windows accepted / rejected | Total wall time | Sampled RSS high-water | Calendar regimes | Generated configurations | CP model build / solve |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| F4, 10-operation breadth-first route-prefix cohort | 10 | 10 | 0 | 8 / 0 | 9.16 s | 419.0 MiB | 538 | 80 | 0.032 / 0.186 s |
| F6, 10-operation breadth-first route-prefix cohort | 10 | 6 | 4 | 4 / 2 | 19.19 s | 482.6 MiB | 825 | 144 | 0.623 / 0.311 s |
| F4, progressive 25-operation cohort | 25 | 9 | 16 | 3 / 5 | 39.23 s | 484.5 MiB | 1,699 | 267 | 0.787 / 0.523 s |

The RSS value is the runner's sampled process RSS at the beginning and end of the run, not an OS-recorded continuous peak. CP model/solve figures sum the solver stage telemetry saved in each artifact. Overall wall time includes resource preparation, calendar generation, CP-SAT, replay, and validation. The runner does not yet expose a separately timed calendar-generation duration.

### Factory 4

The first 10-operation attempt used two five-operation serial prefixes and was rejected. It is retained as `factory4_10_execution_contract_v1.json`; the CP diagnostics reported `physical_infeasibility_proven: false` and an incomplete resource domain. The test cohort was then changed to breadth-first, precedence-closed route prefixes across all eight wholly unstarted captured orders, admitted only under the explicit TEST contract. This selection-only change produced 10/10 accepted operations in eight windows.

Before each window's commitments were carried forward, the candidate plus all previously accepted windows was independently replayed and physically checked against the captured TEST holds. For the 10-operation result: exact replay PASS, physical validation VALID, scope SCOPE_VALID, and global resource-conflict validation VALID. All eight windows passed. This is a valid TEST-only bounded result, not a complete factory schedule.

Because the 10-operation gate passed, a 25-operation cohort was attempted. It scheduled nine operations in three accepted windows; five route windows were rejected and 16 selected operations remain explicitly deferred. The assembled nine-operation prefix independently passed replay, physical validation, scope accounting, and global resource-conflict validation. Failed windows reported `RESOURCE_DOMAIN_EXPANSION_LIMIT`, with physical infeasibility unproven. No further Factory 4 scale-up was attempted.

10-operation keys: `153_1698`, `188_3905`, `188_3906`, `209_2651`, `304_393`, `304_394`, `393_632`, `479_1375`, `638_888`, `925_2288`.

25-operation accepted keys: `153_1698`, `153_1699`, `153_1700`, `209_2651`, `209_2652`, `209_2653`, `304_393`, `304_394`, `304_395`.

### Factory 6

The breadth-first 10-operation cohort covered six wholly unstarted captured routes and was admitted only under the TEST assumptions above. Six operations in four windows were accepted; two route windows (four operations) were deferred after bounded expansion. The six-operation assembled prefix passed exact replay, physical validation, scope validation, and global resource-conflict validation. The failed windows reported `RESOURCE_DOMAIN_EXPANSION_LIMIT`; `physical_infeasibility_proven` was false. Later independent route windows were still evaluated, so the failures did not silently drop unrelated eligible work.

Accepted keys: `274_343`, `274_344`, `429_2981`, `429_2982`, `263_2751`, `915_2288`.

Deferred keys: `817_2694`, `817_2695`, `527_3191`, `527_3192`.

Factory 6 did not complete its 10-operation gate; no 25-operation run was started.

## Domain and validation interpretation

The adaptive controller started with four machine and worker alternatives per operation and made one bounded expansion round. In failing windows it reached a cap of five while source-eligible alternatives remained. The final tested domains are incomplete; a feasible schedule only proves feasibility for the configurations exposed to that attempt. An `INFEASIBLE` CP-SAT result here is not factory-wide or full-domain infeasibility. The earlier failed F4 serial-prefix diagnostic also remained `physical_infeasibility_proven: false`.

In the selected TEST domains, F4 operations exposed 16-22 active capability-matched machine candidates and 29-36 skilled, non-leave worker candidates; F6 exposed 14-17 machines and 32-36 workers. Generated configurations were 80, 144, and 267 for the three runs, respectively. These are candidate pool sizes under the stated TEST line policy, not proof that the plant's full legal resource domain is represented.

The source-authoritative generator and independent contract validator checked factory ownership, operation capability, source skill membership, worker efficiency, contract quantity, selected worker/machine pairing, setup, duration, line membership, calendars, and conflicts. A regression test was added for explicit legal worker-machine pairs so illegal Cartesian cross-pairs are discarded instead of aborting valid alternatives. Resource alternatives remain bounded and not proven complete.

The feasibility model is not an objective-quality experiment. Makespan improvement, delivery performance, utilization, and avoidable idle time are **NOT MEASURED**. `OPTIMAL` in the saved feasibility stages means the CP-SAT satisfaction model found/proved a feasible model status; it is not an optimization-quality claim.

## Implementation and tests

- `run_factory_test.py`: pinned-file-only, per-factory TEST runner; verifies raw and normalized hashes; refuses MySQL connection attempts; requires execution contracts; carries fixed holds and accepted worker/machine reservations; independently checks each assembled accepted prefix before carrying it forward; explicitly defers rejected identities and continues independent route windows.
- `tests/test_factory46_operation_schedule.py`: checks source-route prefix closure, round-robin selection, and complete-route behavior.
- `crafter/repair/resource_configs.py`: source generator now filters contract lines and rejects disallowed explicit worker-machine pairs locally while retaining other legal pairs.
- `tests/test_stage_b_source_authoritative.py`: exhaustive small-domain explicit-pair regression.

Focused regression command covered operation contracts, Stage B source/adaptive domains, Factory 1 admission, calendar regimes, replay, production controller, configuration independence, route-aware crew expansion, and the new Factory 4/6 selector/database tripwire: **70 passed, 0 failed** (two pre-existing PyTorch deprecation warnings).

No Factory 2 benchmark was rerun or overwritten. The scheduling runner reads only the pinned JSON captures and writes new artifacts in this directory. PyMySQL connections are replaced with a fail-closed exception, and a SQLAlchemy `do_connect` listener rejects any connection attempt. The import log saying the application engine was initialized only reflects lazy engine construction; no connection/query or application database write was performed. Live activation remains disabled.

## Artifacts and next gate

Each JSON artifact records source and scenario hashes, selected/scheduled/deferred identities, per-window CP statuses, replay/physical/scope outcomes, resource-domain rounds, calendar-regime counts, and timing. Successful runs are:

- `factory4_10_execution_contract_v1_20261013T194535Z.json` (final run with SQLAlchemy and PyMySQL connection tripwires)
- `factory6_10_execution_contract_v1_20261013T193915Z.json`
- `factory4_25_execution_contract_v1_20261013T194034Z.json`

Earlier serial-prefix failures are retained alongside them, as are the later-test retry artifacts. No existing Factory 1 or Factory 2 evidence was overwritten.

**Readiness:** Factory 4 is ready for another controlled TEST cohort no larger than 25 operations, provided the same TEST assumptions remain explicit; its full 25-operation cohort is not yet validated. Factory 6 is not ready to scale beyond the 10-operation diagnostic until its two deferred route windows are understood or safely expanded. Neither factory is ready for real production scheduling: authoritative crew requirements and machine calendars are missing, outside approved schedules are absent, domains are incomplete, and unresolved partial operations remain blocked. Do not advance either factory to 200-operation cycles.

## Deferred-operation diagnosis and bounded recovery (2026-10-14)

The original selected IDs, source snapshots, scenario hashes, TEST assumptions, route grouping, 30-day extension beyond the latest selected due/release date, and window sizes were held constant. New runs are additive and do not overwrite the earlier artifacts.

### Factory 4: 25-operation cohort

The original 25-ID cohort was rerun after increasing the finite adaptive expansion limit from one round to four. The policy exposed caps 4, 5, 6, 7, and 8, preserving prior candidates. It accepted the same 9 operations in 3 windows and deferred the same 16 operations in 5 windows. Runtime was 127.22 seconds and the runner's sampled RSS high-water was 545.1 MiB. The new artifact is `factory4_25_execution_contract_v1_20261013T195717Z.json`; source, normalized snapshot, and TEST scenario hashes match the original cohort.

All deferred IDs map to five rejected route windows, not 16 independently proven impossible operations:

| Route/window | Deferred operations | Best diagnostic at cap 8 | Evidence and classification |
|---|---|---|---|
| Order 188 / 1 | `188_3905`, `188_3906`, `188_3907`, `188_3908` | Full exposed-domain probe INFEASIBLE; machine-, worker-, and joint-overlap-relaxed probes INFEASIBLE; routing-relaxed probe UNKNOWN | Domain still truncated (112 configurations across this route). Not a timeout in the canonical final CP stage; root constraint family unresolved because the routing ablation did not finish within its 2-second diagnostic budget. |
| Order 479 / 4 | `479_1375`, `479_1376`, `479_1377` | Full and overlap-relaxed probes INFEASIBLE; routing-relaxed probe UNKNOWN at cap 8 | Domain still truncated (104 configurations). No physical infeasibility claim. Earlier cap-5 routing relaxation was OPTIMAL, suggesting route/domain interaction, but that is not a sufficient acceptance diagnosis. |
| Order 925 / 6 | `925_2288`, `925_2289`, `925_2290` | Full and overlap-relaxed probes INFEASIBLE; routing-relaxed probe UNKNOWN at cap 8 | Domain still truncated (88 configurations). Constraint root unresolved; no physical infeasibility claim. |
| Order 393 / 7 | `393_632`, `393_633`, `393_634` | Full and overlap-relaxed probes INFEASIBLE; routing-relaxed probe OPTIMAL | Domain still truncated (104 configurations). Routing/calendar/domain interaction is demonstrated, not an individual operation contradiction; further legal alternatives remain. |
| Order 638 / 8 | `638_888`, `638_889`, `638_890` | Full and overlap-relaxed probes INFEASIBLE; routing-relaxed probe UNKNOWN | Domain still truncated (88 configurations). Constraint root unresolved; no physical infeasibility claim. |

Across these groups, the final canonical stages ended INFEASIBLE rather than UNKNOWN, but this is only a result for the exposed configurations and bounded temporal model. For three groups the route ablation itself timed out; for one it found a solution only after removing routing constraints. No group establishes true factory infeasibility. The TEST model has no authoritative partial-operation continuation for the three open partials, and their unknown occupancy is not reconstructible; captured holds were quarantined and preserved, but that does not close the real global state.

### Factory 6: 10-operation cohort, complete captured TEST alternatives

The original 10 IDs were rerun with all source-captured active/capability-matched machines and active, skilled, non-leave workers exposed under the unchanged one-worker/one-machine TEST assumption and same-line pairing. This is complete only within those captured eligibility lists and TEST filters; it is not a claim of authoritative factory-domain completeness. Four operations remained accepted and the same two route windows (four operations) remained deferred. Artifact: `factory6_10_execution_contract_v1_20261013T200655Z.json`.

For `817_2694` / `817_2695` and `527_3191` / `527_3192`, CP-SAT returned INFEASIBLE on the complete captured TEST domain. The final models each contained 1,920 candidate configurations; calendar-regime counts were 2,512 and 2,752 respectively. Disabling machine overlap, worker overlap, or both did not make either route feasible. The routing-relaxed probe returned UNKNOWN at its 5-second limit. Thus ordinary resource overlap alone is not the demonstrated cause; the remaining localization is route/calendar interaction or another constraint retained by that ablation. These outcomes prove infeasibility only under the explicit TEST contract and bounded problem horizon, not physical infeasibility in the plant. Unknown in-flight occupancy for unresolved partials remains a separate production blocker.

Runtime was 232.81 seconds; sampled RSS high-water was 581.3 MiB. The previously accepted six-operation assembled prefix remains exact-replay PASS, physical VALID, scope SCOPE_VALID, and conflict VALID against captured holds and accepted TEST assignments. The two rejected windows were not included in the accepted prefix.

### Full-domain Factory 4 attempt and implementation changes

A full captured-domain attempt for the same Factory 4 25-ID cohort was started using the same TEST rules. It continued for approximately eight minutes; the observed process working set reached about 875 MB and CPU time about 460 seconds. It was interrupted at the bounded workstation budget before the runner's end-of-run artifact write. Therefore it produced no completed full-domain result and is not counted as a scheduling attempt with accepted/deferred outcomes. This exposes an important measurement/checkpoint gap: the runner currently persists only after a complete run, so interruption loses window-level evidence. The attempt did not modify its source capture or any database.

The only scheduling-core behavior used here is the existing `ResourceDomainPolicyV2` expansion and canonical CP-SAT/replay/validator path; no physical constraints were relaxed. The audit runner now permits up to four bounded expansion rounds and has an opt-in `--complete-test-domain` mode that exposes all captured candidate alternatives under the same assumptions. Focused runner/source-domain tests passed after these changes. No successful recovery was achieved: F4 remains 9/25 and F6 remains 6/10.

### Required next step

Do not increase cohort size. First make the runner persist a versioned progress checkpoint after each window, including the exact selected assignments and carried reservations. Then add a targeted route diagnostic that records whether each operation has a standalone calendar-feasible regime under the complete captured TEST domain and isolates route-edge assumptions without removing them from any accepted schedule. Use longer-but-finite diagnostic budgets only on the two Factory 6 route windows and five Factory 4 failed route windows. If a full captured-domain route is infeasible, report it as TEST-contract infeasibility and do not infer factory impossibility. Any later staffing/calendar-policy change requires factory-approved evidence, not a solver-driven assumption change.
