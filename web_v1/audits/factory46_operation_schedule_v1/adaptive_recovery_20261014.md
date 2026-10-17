# Adaptive window recovery checkpoint

This work preserves the pinned cohorts and execution assumptions. No database
connection is permitted by the capture-only runner. Production remains disabled.

## Implementation

The runner begins with ordered route-prefix chunks of at most five operations.
A rejected chunk is retried in chunks of at most two, then singletons. Rejected
candidates never add reservations. A missing external predecessor explicitly
defers the dependent chunk. Accepted ends are passed through the existing
`committed_routing_completion` contract; worker segments and machine intervals
remain protected. Every accepted assembled prefix passes canonical replay,
scope validation, and independent execution-contract physical validation.

Per-attempt, uniquely named JSON diagnostic checkpoints retain accepted rows,
remaining identities, pending chunks, provenance, and validation evidence.
These are tentative TEST artifacts, not approvals. Restart loading is NOT yet
implemented or verified by these changes.

## Correction to earlier diagnostics

The earlier Factory 6 complete-configuration diagnostic does NOT prove physical
infeasibility, even under the TEST contract: calendar-domain completeness was not
established. Its failed route models exposed 568 configurations each, not 1,920;
1,920 was the configuration limit. Six operations, not four, were accepted.
INFEASIBLE applies only to the represented CP model. Wider configuration pools
alone cannot establish full time-domain coverage.

## Regression evidence

Executed command:

```
venv\Scripts\python.exe -m pytest tests/test_factory46_operation_schedule.py tests/test_operation_execution_contract.py tests/test_resource_domain_v2.py tests/test_resource_domain_v3.py tests/test_incremental_calendar_regimes.py tests/test_calendar_regime_coalescing.py tests/test_replay_semantics.py tests/test_rolling_horizon_integration.py -q
```

Result: 73 passed, zero failed, two dependency deprecation warnings, 31.39s.
The new 25 parametrized cases prove reduction preserves identity and order and
strictly decreases failed chunk size. Actual carry-forward is additionally
checked by the real cohort runs, not inferred from these unit tests.

## Reference and limits

OR-Tools job-shop documentation:
https://developers.google.com/optimization/scheduling/job_shop
Whole-operation precedence and resource no-overlap constraints remain unchanged.
This changes orchestration, not physical feasibility constraints.

Factory-wide scheduling, restart recovery, browser flows, fuzzy integration,
and comparative FIFO/EDD/SPT evaluation are not established by this milestone.
No new production-readiness claim is made.

## Completed execution evidence

| Experiment | Accepted operations | Accepted/rejected attempts | Runtime s | Sampled RSS MiB |
|---|---:|---:|---:|---:|
| Factory 6, adaptive split | 10/10 | 8/2 | 100.13 | 502.86 |
| Factory 4, adaptive split only | 20/25 | 9/10 | 203.81 | 521.85 |
| Factory 4, adaptive split + route-ready probes + existing objective | 25/25 | 9/1 | 98.26 | 467.06 |

Artifacts in this directory:

- `factory6_10_execution_contract_v1_20261013T204044877490Z.json`
- `factory4_25_execution_contract_v1_20261013T204217183111Z.json`
- `factory4_25_execution_contract_v1_20261013T204531941570Z.json`

The final Factory 4 and Factory 6 results both have assembled replay PASS,
physical VALID, scope SCOPE_VALID, and captured-boundary resource conflict VALID.
Their source SHA, normalized snapshot hash, scenario hash, assumptions, and
selected operation arrays were compared with their original 9/25 and 6/10
artifacts: all five comparisons match for each factory. No selected identity
remains deferred in the final results. All captured quarantined holds remain
in the input boundary. This does not resolve unknown uncaptured commitments.

The split-only Factory 4 run exposed a feasibility-only placement problem:
`479_1376` ran at seconds 29,205,787–29,505,599, leaving poor successor headroom.
The successful run reuses the existing lexicographic objectives (including
regional makespan) and Factory 1's additive exact route-ready calendar probes.
No solver constraint, execution assumption, or resource eligibility was relaxed.
This combined experiment does not isolate the contribution of each change or
establish globally earliest starts. No full-domain infeasibility is claimed.

Memory is start/end sampled process RSS, NOT a continuously measured peak.
Runs overlapped other diagnostic/test processes; timing is not a controlled
speedup comparison. Utilization and baseline superiority were not measured.

Additional regressions: runner + repair objectives: 56 passed in 17.24s (29
overlap with the 73-test run). Cache equivalence: one unittest passed in 9.862s.
Thus 101 distinct tests passed across the successful invocations. One earlier
pytest invocation had a package-relative-import collection error; rerunning
the cache test via its package unittest entry point succeeded. No test was
changed to bypass that error.

Reproduce:

```
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 6 10
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 4 25 --route-ready-objective
venv\Scripts\python.exe -m unittest web_v1.audits.factory1_realistic_neighborhood_v1.test_route_probe_cache -q
```

## Completion matrix and next step

- PASS: original Factory 4 25-operation and Factory 6 10-operation TEST cohorts.
- PASS: exact accepted-prefix and final assembled validation under declared TEST contracts.
- PARTIAL: adaptive orchestration is in the capture runner; not generalized into the persistent application coordinator.
- PARTIAL: uniquely named per-attempt diagnostic checkpoints; safe resume loader and interruption/idempotency tests still required.
- PARTIAL: legal configuration sampling and sparse calendar domains; completeness unproven.
- NOT REVALIDATED: Django/browser, manager-priority repair, all-six-factory scheduling, FIFO/EDD/SPT comparison.
- BLOCKED for production: authoritative crew/productivity/calendar rules and unresolved actual continuation/commitment evidence.

Next: integrate the proven recovery policy into the existing coordinator with
versioned tentative checkpoint restoration, stale-state checks, and adversarial
cross-window interruption tests before larger cycles. Preserve the working
governed approval path rather than creating a second activation mechanism.

Safety: all scheduling reads used pinned files and connection tripwires; no
database operations were performed. No Factory 2 files were targeted or edited.
A complete frozen-artifact hash-manifest audit was not rerun, so repository-wide
frozen integrity is not newly certified. No Django records or live activation
paths were changed.
