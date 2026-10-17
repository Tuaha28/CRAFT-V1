# Factory-1 execution boundary adapter result

The adapter and independent rejection gate are implemented. Factory-1 is **not admitted to CRAFT**: all 82 partially executed operations lack a proven complete committed residual plan. The classification defect is corrected without changing CRAFT or the frozen workload.

Database: `garment_fyp_db`. Reference: `2026-09-10T00:00:00`. Database writes: zero. The schema is unchanged. No solver, scheduling experiment, ERP implementation, or schedule promotion ran.

## Whole-factory accounting

| Normalized state | Operations | Treatment |
|---|---:|---|
| FULLY_COMPLETED | 171 | Historical immutable completion evidence |
| PARTIALLY_EXECUTED | 82 | Non-optimizable, BOUNDARY_UNRESOLVED |
| UNSTARTED | 2,626 | Potential decisions, subject to release/precedence and admission gate |
| Total | 2,879 | Complete historical operation identity retained |

Lot accounting: 4,912 completed, 35 in flight, 52,385 not started. Seventeen operations contain in-flight lots; 65 additional partial operations contain completed work and an unstarted tail. Freezing only currently Running operations would miss those 65 too.

## Requested operation cases

All nine normalize to PARTIALLY_EXECUTED, are non-optimizable, preserve resource associations for each in-flight lot, and retain every residual lot and quantity. None has a certified whole-operation completion boundary. Successor execution remains withheld where that unresolved boundary matters.

| Operation | Completed lots | In-flight lots | Not-started lots | Completed units | Remaining units | Previous classification |
|---|---:|---:|---:|---:|---:|---|
| 40_902 | 54 | 4 | 2 | 2,700 | 300 | Optimizable |
| 40_903 | 41 | 5 | 14 | 2,050 | 950 | Optimizable |
| 40_905 | 18 | 4 | 38 | 900 | 2,100 | Optimizable |
| 212_2584 | 58 | 1 | 1 | 1,934 | 66 | Optimizable |
| 212_2586 | 52 | 2 | 6 | 1,734 | 266 | Optimizable |
| 411_2598 | 3 | 1 | 14 | 60 | 305 | Optimizable |
| 411_2599 | 2 | 1 | 15 | 40 | 325 | Optimizable |
| 979_952 | 35 | 1 | 21 | 700 | 440 | Optimizable |
| 212_2585 | 56 | 1 | 3 | 1,867 | 133 | Running/fixed |

`audited_operations.json` provides every lot state, exact line/machine/worker hold, observed start/segments, uncommitted projections, residual IDs, release constraints and predecessor/successor references for these cases. The snapshot retains the same details for all operations.

## Difference from the frozen experiment

The full live snapshot retains order 790. That is intentional source accounting, not a change to the frozen experiment. Its nine operations comprise four partial and five unstarted operations.

Within the exact frozen membership of 2,699 state operations (order 790 excluded, 171 completed omitted), the corrected partition is:

- 78 partial operations protected from optimization.
- 2,621 unstarted operations potentially optimizable.
- No missing frozen state operation.

The old frozen membership had seven running/fixed and 2,692 optimizable operations. Of those 2,692, **71 are now correctly protected as partially executed**. This includes the eight audited in-flight Delayed cases and additional completed-prefix operations. The seven previously fixed operations are also rejected for live handoff until their entire residual tail is justified.

The 171 completed operations are retained only as immutable historical evidence in the new complete snapshot. Every comparison is recorded in `frozen_workload_comparison.json`; no frozen file is overwritten.

## Why the blocker remains

The generator stores future simulated actual end times and segments. They are useful diagnostics but are not an approved physical carry-forward plan. Existing scheduled rows have no verified immutable commitment/approval provenance. Therefore the adapter will not reserve only a currently running lot and silently discard its future tail, infer remaining duration, or freeze every future simulated endpoint as if validated.

The independent validator reports only `BOUNDARY_UNRESOLVED` for the real snapshot (82 operations). Quantity accounting, captured resource identities, source membership, segment preservation and the conservative precedence gates otherwise pass. No physical infeasibility is claimed.

The actual handoff rejection was exercised. It raised `BoundaryUnresolved` without importing the scheduler. The real preparation smoke test was **not run**, as required when admission invariants do not pass.

Next step: obtain or define an authoritative, physically validated residual commitment plan covering every unfinished lot of each partial operation, then test its representation in existing CRAFT boundary structures. No core modification is established as necessary, and no lot optimizer was introduced. Until then, `SAFE_TO_RESUME_V2_STEP_2 = no`.

Test results are recorded in `test_results.json`. Source/frozen-file hashes and deterministic snapshot hashes are recorded in the JSON evidence alongside this report.

## Resumed residual-tail investigation

No database recapture, scheduler solve, ERP changes or core modifications were
needed. The captured source and original normalized payload are preserved.
`residual_tail_assessment.json` adds an explicit assessment for **all 82** partial
operations, including exact residual lot IDs, quantities and known resource IDs.
It is independently reproducible through `run_audit.py --from-capture`.

Result: **0 RESOLVED_COMMITTED_BOUNDARY; 0 RESOLVED_SPLITTABLE_RESIDUAL;
82 BOUNDARY_UNRESOLVED**. There are 17 operations with in-flight lots and 65
completed-prefix/unstarted-tail operations. All nine named cases remain protected
but none has a proven finite completion boundary. Their individual assignments
remain in `audited_operations.json` and the new assessment; no assignment is
silently discarded or replaced.

The existing calendar machinery can execute a known productive-work budget with
a fixed crew and reserve machines across pauses. It cannot establish that budget
from the presence of future generated actuals. The carry-forward diagnostic tests
a supplied segmented plan, not reconstruction of live lot progress. `split_operation_work`
means setup versus productive time, not splitting an operation's residual demand.
The full evidence/source map is now in `EXECUTION_BOUNDARY_CONTRACT.md`.

The smallest remaining issue is an authoritative residual commitment producer:
it must supply measured remaining work for in-flight lots plus a resource- and
calendar-validated continuation for every unstarted tail lot. If no measured
progress or trusted committed plan exists, additional source evidence is required;
a Boolean approval flag or future actual end is insufficient. Case B is not
supported by current validated operation-level semantics.

The fail-closed contract is implemented, but the requested live integration gate
is **not closed**. A smaller scope must not be made safe merely by omitting
unresolved shared-resource boundaries. Real CRAFT preparation remains NOT RUN.
`SAFE_TO_RESUME_V2_STEP_2 = no`.

Final checks: execution-boundary suite **42 passed / 24 subtests passed**;
offline planning/replay/controller/worker-continuity regressions **55 passed,
3 Window-14 tests deselected**. An initial broader run reported **102 passed,
1 failed**: the existing database-backed reservation test expected 143,456
snapshot rows and read 144,665. The cause of that count difference was not
established. Its read-only query accessed the legacy GA snapshot database;
the Factory-1 captured source was not recaptured. No test expectation or source
database was changed to hide this failure. Exact results are in `test_results.json`.

Two normalizations with reversed retrieval ordering produced the same complete
snapshot hash and identical residual classifications/resource associations.
The normalized snapshot hash is unchanged from the previous run. Evidence
finalization is reproducible with `python web_v1/audits/execution_boundary_v1/finalize_evidence.py`.
