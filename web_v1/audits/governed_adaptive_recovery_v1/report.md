# Governed adaptive checkpoint recovery — 2026-10-14

Classification: controlled pinned Factory1 SIMULATED TEST, not factory actuals.
Production activation DISABLED. Overall industrial completion PARTIAL.

## Implemented and executed

The existing `cycles.py` facade retains V1 by default and dispatches opt-in
`adaptive_retries: true` to `adaptive_cycles.py`'s V2 attempt ledger. Rejected
attempts are distinct from accepted identities and reserve nothing. Failed
multi-operation windows split to two operations, then singletons. Failed
singletons are explicitly deferred. Unresolved predecessors block successors;
independent queued work may continue. Finite partitioning prevents retry loops.

The existing Factory1 runner and governed adapter consume this ledger. One
attempt per subprocess retains the worker's 180-second cap. No solver constraint
was removed. The source validator independently reconstructs retry order and
continues checking quantities, durations, eligibility, calendars, precedence,
occupancy, source hashes and simulated/approved external holds. Restored accepted
prefixes pass that validator before entering the next solve.

Django persists each checkpoint through its existing atomic/CAS admission path,
with snapshot freshness and monotone revision checks. Terminal partial results
retain the latest deferral ledger, not the previous checkpoint. Rejected attempts
are no longer labeled `VALIDATED_WINDOW`. Checkpoints never activate schedules.

The portal's existing run register now displays next-attempt size, attempt count,
accepted/remaining work and explicit deferral reasons. It labels checkpoints as
not manager approval. Frontend/browser test results are recorded separately below.

## Real recovery evidence

`integration_result_20261014.json` records an actual CP-SAT run and Django
workflow, not mocked scheduling output. A test-only probe imposes a one-microsecond
CP search budget to obtain UNKNOWN. Physical inputs and constraints are unchanged.
This is recovery fault injection, not a throughput benchmark or infeasibility proof.

- Eight source operation identities, two orders, two cycles.
- Four attempts: one rejected four-operation attempt, followed by accepted 2, 2, 4.
- All eight operations accepted; no remaining or duplicate identities.
- All accepted windows: replay PASS, physical VALID, scope SCOPE_VALID.
- Independent assembled and global validation PASS; all 35 simulated holds checked.
- Abandoned-worker recovery and idempotent request verified.
- Candidate appears only after full validation; explicit manager TEST approval passes.
- 69.788 seconds sum of internal runner times; 172.760 seconds whole Django test.
- Maximum sampled RSS 613.63 MiB, not continuous peak memory.
- Full resource-domain completeness and future-demand closure remain unproven.

Pinned raw source SHA256:
`010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`.
Scenario hash:
`3c04126f8d1e24ebc41c4286ebc57132f59a542bb7a9f552a6a6a19ee87c36b3`.

## Reproduction

From repository root, use a test-only `DJANGO_SECRET_KEY` in the process environment.
Do not migrate or open the existing application database. Django's test command
creates and destroys a disposable test database. The scheduler interpreter is
`venv/Scripts/python.exe`; the backend uses its separate virtualenv.

```
venv\Scripts\python.exe -m pytest tests/test_adaptive_cycle_recovery.py tests/test_factory_cycles.py tests/test_tentative_checkpoints.py tests/test_factory46_operation_schedule.py tests/test_operation_execution_contract.py web_v1/backend/integrations/craft/tests/test_factory1_cycle_failure.py -q
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_adaptive_cycles --settings=config.settings_integration --noinput
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_stage_b_factory1.StageBFactory1GovernedTests.test_checkpointed_adaptive_windows_preserve_approved_outside_work governance.test_factory1_cycles --settings=config.settings_integration --noinput
```

Optionally set `CRAFT_ADAPTIVE_EVIDENCE_PATH` to a new, nonexistent JSON path.
The recovery integration test uses exclusive creation, never overwrites evidence.
For the 12+13 approval regression, set `CRAFT_CYCLE_TEST_EVIDENCE_DIR` to a
new directory. It writes immutable per-attempt evidence and, only after all
promotion assertions succeed, a separate `*_approval_PASS.json`. Keep scheduler
implementation files unchanged during these runs: changing them must trigger
freshness rejection. Capture test stdout/stderr in a new log to survive a client
session interruption. A saved VALIDATED candidate is not proof of promotion.

## Safety and limitations

No legacy database connection or mutation was used. Only disposable Django test
records were created. Source captures and historical Factory2 evidence are not
edit targets; a repository-wide frozen-manifest audit has not been rerun, so no
blanket integrity certification is claimed. Browser output now uses unique paths
instead of replacing an earlier Gantt screenshot.

This is not power-loss certification, multi-worker concurrency certification, a
production deployment, or whole-factory scheduling. Factory1 admission remains
limited to the independently admitted source region. Real unresolved partials
remain blocked. TEST staffing/productivity assumptions are not factory policy.
Six-factory completion, factory-wide priority/disruption/fuzzy integration, fair
FIFO/EDD/SPT evaluation and factory acceptance remain open.

References consulted for unchanged solver semantics:
[OR-Tools CP-SAT statuses](https://developers.google.com/optimization/cp/cp_solver)
and [job-shop precedence and exclusivity](https://developers.google.com/optimization/scheduling/job_shop).
UNKNOWN is a search-limit result; splitting a bounded model preserves physical
constraints and carries validated reservations forward, not a relaxation.

## 2026-10-15 exact-duration and approval continuation

The first 12+13 adaptive regression failed safely: first 12 approved; only four
of the second 13 accepted. Operation 469_1744 had 32052 approved productive
seconds, but encoding as 534.2 minutes then applying floating-point ceil yielded
32053. Exact replay rejected the mismatch. This was not evidence of physical
infeasibility or insufficient worker alternatives. Failed evidence remains in
`approval_20261015/` and `replay_diagnostic_20261015.json`.

`calendar_minutes_from_integer_seconds` now encodes already-rounded source work
with the exact invariant `ceil(minutes * 60) == seconds`. Source configuration
generation uses this codec. Native-minute legacy semantics are unchanged, and
no replay tolerance or physical constraint was relaxed. Failure diagnostics now
identify replay/materialization/physical rejection rather than labeling every
rejection a resource/calendar limit.

31 focused tests passed (15.81s): integer oracle through 100000 seconds and large
boundaries, actual source configuration generation for two worker efficiencies,
calendar equivalence, adaptive attempt accounting and immutable checkpoints.
The preceding broader Django invocation passed 37 tests (270.731s).

Corrected saved candidate evidence in `approval_corrected_20261015/` proves
12 operations/3 windows and 13 operations/4 windows independently validated,
with global conflict PASS and 47 outside commitments for the second request
(12 approved assignments plus 35 hypothetical holds). Request wall times were
159.715s and 214.264s; these include subprocess/restore overhead and concurrent
test activity, not a controlled speed comparison. Final approval test output was
lost on interruption, so this evidence alone is not a PASS for both promotions.
A final rerun writes `*_approval_PASS.json` only after both promotions, active
version uniqueness, outside-assignment preservation and two audit events pass.
Its evidence directory is `approval_final_20261015/`.

**Final adaptive approval gate: PASS.** Durable proof:
`approval_final_20261015/97bdfe55-0a0f-4969-96cc-a8c714f114a3_approval_PASS.json`.
Both actual authorized TEST promotions completed; exactly one active version,
two promotion audit events, consolidated 25 operations, and the original 12
assignments unchanged. All seven windows pass replay/physical/scope checks;
assembled and global conflict validation PASS. Each request used two cycles
(target 8, window maximum 4), not a 200-operation run. No retry was needed in
this corrected cohort; actual failure/retry is established by the separate
eight-operation budget-exhaustion test above.

Final run measurements: first request 161.925s, second 310.929s (472.854s sum
of request wall times); seven internal runner invocations sum 247.815s.
Maximum sampled RSS 630.27 MiB. Accepted-window calendar time totals 40.623s;
combined solve/validation field totals 33.632s (NOT pure CP-SAT time).
Restart/normalization/restore and application overhead are not included in
those two stage sums. Concurrent regression activity makes these diagnostic
measurements unsuitable for a speedup claim. Domain completeness remains
unproven. Default V1 cycle regression is a separate gate in the durable log.

Final durable log `approval_final_20261015.log`: 4 tests PASS in 735.521s,
including adaptive approval and all three default V1 cycle regressions. The
default path validated eight operations/four windows/two cycles, simulated lease
recovery, idempotent request submission, source-based prefix restore and manager
TEST approval. Admission and stale-state negative tests passed. Django reported
OK and destroyed the disposable test DB. PowerShell's Tee wrapper returned 1
after treating normal native stderr as NativeCommandError; do not describe that
wrapper as exit-zero. Test outcomes are evidenced by Django's final summary and
the separate post-promotion JSON, not the shell wrapper status.

Latest disjoint test batches: 71 scheduler, 9 Django (4 above plus 5 admission/
security), 16 frontend, 2 browser = 98 passed. No test failures in these final
invocations. Earlier failed approval and contaminated browser runs are retained
above. Existing broad 37-test Django regression was also PASS but is not added
to the disjoint total.

Clean browser verification PASS: two real Edge workflows (initial schedule,
Gantt, manager TEST approval; and synthetic urgent-order rescheduling/comparison/
approval), 97.488s including service setup. Evidence:
`../fyp_completion_20261010/browser_e2e_20261015_015121_795572.json`.
An earlier browser attempt correctly rejected an implementation change during
execution; it is retained and superseded by the clean run, not hidden.

Production remains disabled. Source-admission and complete resource-domain
coverage limitations above remain in force.

### Next industrial gate (not a scheduling result)

Rechecked the immutable capture readiness reports; all 14 manifest files match.
These are source-evidence classifications, not controlled TEST scheduling counts:

| Factory | Completed | Blocked partials | Unstarted requiring evidence |
|---|---:|---:|---:|
| 1 | 171 | 82 | 2626 |
| 2 | 170 | 49 | 1905 |
| 3 | 80 | 45 | 1204 |
| 4 | 28 | 3 | 63 |
| 5 | 256 | 89 | 1943 |
| 6 | 19 | 10 | 56 |

Do not turn these unstarted operations into production-admissible work by
defaulting crew size to one. The next controlled implementation milestone is
to integrate the already validated Factory4/6 TEST execution-contract adapters
with governed resumable jobs, preserving their separate snapshot identities and
independent validation. Only then expand cohorts under explicit TEST contracts.
Factory-wide production admission additionally needs approved crew/productivity
rules and validated continuation evidence. Source-wide disruption/fuzzy policy,
fair FIFO/EDD/SPT evaluation, deployment concurrency and factory acceptance are
separate unfinished gates; existing synthetic urgency does not establish them.
