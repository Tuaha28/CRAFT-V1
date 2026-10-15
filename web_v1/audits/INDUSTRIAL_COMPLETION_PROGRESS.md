# CRAFT industrial completion ledger

Updated 2026-10-15. Production activation DISABLED. Industry readiness NOT ESTABLISHED.

| Subsystem | State | Verified evidence / next gate |
|---|---|---|
| Operation-level physical scheduling | PARTIAL, cohort gates PASS | Original F4 25/25 and F6 10/10 pass; F6 expanded 25/25 now passes six windows across three checkpointed processes. Strict TEST contracts, replay, physical/scope and assembled validation preserved. Real crew/productivity/continuation evidence still missing. |
| Adaptive windows | VALIDATED controlled Django recovery | Opt-in V2 retry ledger now connects source-admitted Factory1 cycles to smaller-window retries. Real budget-exhaustion/restart/approval test passed: eight operations, three accepted windows, two cycles. Not a generalized six-factory admission service. |
| Persistent recovery | PARTIAL, controlled gates PASS | Immutable file checkpoints, fresh-process resume and completed-checkpoint idempotency verified on Factory6. Governed Factory1 V2 physically revalidates restored prefixes and retries rejected windows. Eight-operation fault/restart/approval and 12+13 approved-outside gates passed. Power-loss and deployment concurrency unverified. |
| Approximately 200-operation cycles / six factories | PARTIAL | Cycle accounting exists; full physical factory-wide completion not demonstrated. Do not bypass admission to increase counts. |
| Priority and disruption decisions | PARTIAL | Existing controlled manager-priority/browser evidence; source-wide urgent repair and fuzzy orchestration not newly verified. |
| Django/React governance | PARTIAL | Prior controlled browser and approval evidence retained. No new deployment/readiness certificate. |
| FIFO/EDD/SPT evaluation | NOT_STARTED for corrected cohorts | Require equivalent physical domains and starting commitments. |
| Security / operational acceptance | BLOCKED for production | Factory-approved requirements, actual execution integration, intended deployment concurrency/security and factory acceptance tests required. |

Factory6 checkpoint-limited 25-operation expansion is complete: all 25 accepted,
six windows; 184.42s summed in-run wall time; 537.10 MiB maximum sampled RSS.
Earlier assignments and all three captured holds preserved. Full factory not proven.

Adaptive retries now integrate with the opt-in Factory1 V2 governed cycle path,
with independent attempt accounting and unchanged source admission. The default
V1 path still terminates on failed windows. The 12+13 validation gate now passed;
extend verified TEST execution contracts before expanding admitted workloads.
Never interpret a checkpoint as manager approval.

New recovery evidence and commands: `checkpoint_recovery_v1/report.md`.
Tests: 58 focused + 1 actual CP checkpoint adversarial test + 3 post-hardening
Django cycle tests + 1 additional terminal failure regression passed (63 distinct
tests); no failures in these final invocations.

Historical continuation targets (implemented by the V2 milestone below;
retained here to explain the changes, not as a new work queue):

1. `crafter/production/cycles.py`: existing V1 accounting prohibits repeated
   attempted identities. Adaptive retries require an explicit, versioned attempt
   ledger distinct from accepted operation accounting; retain V1 compatibility.
2. `web_v1/audits/factory1_realistic_neighborhood_v1/run_bounded.py` and
   `validate_bounded.py`: currently fixed window partition/order is independently
   checked. Any adaptive governed path must validate its retry trace and source
   route closure, not just disable the fixed-partition check.
3. `web_v1/backend/integrations/craft/factory1_cycles.py` and governance services:
   currently rejected windows terminate. Preserve active approvals, fingerprint
   freshness and bounded worker invocations while implementing explicit retry,
   terminal deferral and safe selection of independent work.
4. Reuse newly verified checkpoint physical revalidation and immutable store.
   Do not silently migrate older unsealed diagnostic files into trusted restores.

Previous verified milestone and commands:
`factory46_operation_schedule_v1/adaptive_recovery_20261014.md`.

Safety: source captures and historical benchmarks are not edit targets; no legacy
or existing Django database writes. No production activation authorized.

## 2026-10-14 governed adaptive recovery milestone

Implemented `adaptive_retries: true` for existing source-admitted Factory1 cycle
requests, retaining V1 default behavior. V2 records attempts separately from
accepted identities; splits rejected windows to two, then one; defers terminal
singletons explicitly; blocks unresolved successors and can continue independent
work. One attempt per worker invocation retains the 180-second subprocess limit.
The independent source validator reconstructs retry order instead of disabling
window-accounting checks. Restored accepted prefixes are physically revalidated.

Actual Django integration PASS: intentionally exhaust the real CP-SAT search
budget (UNKNOWN, not a fabricated solver output), persist rejection, simulate
abandoned worker, resume through normal fresh subprocesses, validate all eight
operations, and manager-approve only the final TEST candidate. Four attempts,
three accepted windows, two cycles; 69.788s summed internal runner time, 172.760s
whole test, 613.63 MiB maximum sampled RSS. Fault injection is not a performance
benchmark. Evidence: `governed_adaptive_recovery_v1/integration_result_20261014.json`.

64 focused scheduler tests and five Django admission tests passed. Tests prove
finite retry accounting, stale rejection, duplicate prevention and terminal
deferral persistence. The new adaptive 12+13 approved-outside integration and
default V1 cycle regression are still running; do not mark these gates complete
until their results are recorded. No existing application DB was used: Django
created/destroyed its disposable test database. No legacy connection was opened.

Remaining: full-factory admission and six-factory scalability, production data
contracts, deployment concurrency/crash certification, source-wide priority and
disruption/fuzzy integration, fair baselines, monitoring and factory acceptance.
Industry readiness remains NOT_ESTABLISHED and production remains DISABLED.

## 2026-10-15 continuation

The two unfinished 2026-10-14 process sessions no longer exist. Their final
outcomes are UNKNOWN, not PASS. The adaptive 12+13 approval regression is being
rerun alone with immutable per-attempt JSON in
`governed_adaptive_recovery_v1/approval_20261015/`.

The broader regression exposed `EXISTING_PARTIAL_CHAIN_CANDIDATE_DIFFERS`:
the TEST_SIM worker compared fresh solver output against a historical candidate
file. Corrected only output ownership: application invocations now write their
candidate/smoke/two-window evidence beside the caller's isolated output path;
standalone historical reproduction retains its original comparison guards.
No replay, physical, quantity or assembled validation was removed. The actual
TEST_SIM worker-to-manager-approval regression now passes (26.556 seconds).

Additional verified 2026-10-14 results: 70 unique scheduler/recovery tests,
16 frontend tests, lint and production build PASS; two security-header tests PASS.
All 14 entries of the six-factory capture manifest match. This is not a blanket
frozen-benchmark integrity certification. Anti-framing middleware is now enabled;
`check --deploy` still reports four HTTPS/cookie/HSTS warnings for the local HTTP
profile. TLS/reverse-proxy and deployment acceptance remain unverified.

Browser and broad governance reruns are in progress. Do not infer their success
from component tests. The adaptive job display shows attempts, next window,
explicit deferrals and checkpoint-not-approval labeling. CRAFT web skill guidance
kept this in the existing run register without a visual redesign.

### 2026-10-15 resumed: integer work-duration correction

Broad governance regression completed: 37 tests PASS (270.731s). The adaptive
12+13 regression did NOT pass: first 12 approved, second request accepted four
and deferred nine. Saved attempts are in `approval_20261015/` under the recovery
audit. Exact replay correctly rejected operation 469_1744: converting its
already-rounded 32052 productive seconds to 534.2 minutes and back with ceil
produced 32053 seconds. This was an encoding defect, not proven infeasibility.

Added an exact integer-seconds/minutes calendar codec for source-authoritative
configurations only; native legacy minute semantics and replay checks remain
unchanged. 32 targeted duration/source-domain/execution-contract tests PASS.
The corrected offline diagnostic accepts both original windows with exact replay
PASS (`replay_diagnostic_corrected_20261015.json`). It is NOT an application
approval. A new real 12+13 approval run is underway, saving separately in
`approval_corrected_20261015/`; its outcome remains unverified until completion.

Browser rerun: initial scheduling/Gantt/TEST approval passed; urgent workflow
was invalidated by implementation changes during execution (the freshness
guard correctly rejected it). Clean browser rerun is required with code frozen.

Clean browser rerun subsequently PASS: both initial scheduling/Gantt/approval
and synthetic urgent-order rescheduling/comparison/approval, 97.488s total.
Evidence: `fyp_completion_20261010/browser_e2e_20261015_015121_795572.json`.
Latest focused suites: 31 duration/source/calendar/accounting tests PASS (15.81s)
and 40 replay/execution-contract/domain/physical-restore tests PASS (98.96s).
The two lists are disjoint (71 tests); earlier overlapping runs are not added.
All 14 source capture manifest entries were rechecked and still match.

Both corrected 12+13 candidates validated in seven windows, but the interrupted
process lost final approval assertions. Do not infer promotion PASS from those
candidate files. Final verification now saves a separate post-promotion PASS
artifact and a durable log in `governed_adaptive_recovery_v1/approval_final_20261015*`.

Final adaptive 12+13 manager-approval gate PASS: durable
`approval_final_20261015/97bdfe55-0a0f-4969-96cc-a8c714f114a3_approval_PASS.json`.
Twenty-five consolidated operations, seven accepted windows (maximum four
operations/model), two cycles per request, two authorized TEST promotions,
one active version, original 12 assignments preserved, 35 hypothetical holds
protected; replay/physical/scope/assembled/global validation PASS. Request wall
times 161.925s + 310.929s; summed internal runner time 247.815s; sampled RSS
maximum 630.27 MiB. Concurrent tests: not a controlled speed comparison.
Five fresh Django stale/duplicate/deferral/security tests PASS (20.824s).
Frontend rerun 16 PASS (90.87s). Default V1 cycle regression remains running;
its final result must come from `approval_final_20261015.log`, not inference.

Final log now confirms **4 Django tests PASS, 735.521s**: adaptive 12+13
promotion plus all three default V1 cycle tests (two-cycle recovery/idempotency,
stale-state rejection and out-of-domain admission rejection). Django printed OK
and destroyed its disposable database. The PowerShell Tee wrapper reported exit
1 while converting normal stderr to NativeCommandError; this is recorded rather
than misrepresented as a clean wrapper exit. No Django test failed.
Latest disjoint completed batches: 71 scheduler tests, 9 Django tests, 16 frontend
tests, and 2 browser tests PASS (98 tests). Earlier overlapping suites are not
added. The prior broad 37-test governance PASS remains separate regression evidence.

Next implementation: governed Factory4/6 TEST execution-contract adapters and
recoverable jobs, retaining factory isolation and source admission. Do not
increase counts by bypassing crew/continuation evidence. Production-scale
monitoring, fuzzy integration, fair baselines, deployment and factory acceptance
remain unfinished. No production readiness claim is authorized.

## Six-factory governed milestone: engineering handoff

Status: IN_PROGRESS investigation; integration NOT_IMPLEMENTED; no new scaling
runs launched. User requests an explicit engineering/monitoring handoff.
**SWITCH TO ASTRA: ENGINEERING.** This is an integration dependency, not an
external data blocker for explicitly labeled controlled TEST work.

Verified seams requiring implementation:

- `backend/governance/management/commands/craft_worker.py:Command.handle`
  dispatches `FACTORY1_GOVERNED_BOUNDED_V1`; checkpoint forwarding is specific
  to that contract. Keep one bounded attempt per invocation and timeout recovery.
- `backend/integrations/craft/factory1_test_state.py:export_state` uses the
  pinned Factory1 base, hard-coded factory identity and its 25-key admission set.
  Do not reuse its source/eligibility/continuation assumptions for other factories.
- `backend/governance/services.py` request, snapshot, finish and promotion gates
  explicitly recognize TEST_FACTORY1/TEST_RESOURCE. A new adapter must enter ALL
  these gates, not just worker dispatch, including freshness and atomic approval.
- `factory46_operation_schedule_v1/run_factory_test.py:_run` already has bounded
  5/2/1 retries, file-checkpoint restore and independent assembled validation,
  but has no approved-outside snapshot parameter. `_prepare` creates captured
  TEST hold reservations; `_validate_assembled_trial` requires selected route
  predecessors among accepted rows. Generalized later requests must explicitly
  account for approved external predecessor completion, without weakening this
  check. Its CLI currently admits only factories 4 and 6.
- The reusable global-state module already verifies factory-qualified resource
  ownership, approved commitments, routing boundaries and candidate conflicts.
  Reuse it; do not create a second approval mechanism.

Next sequence: source/TEST-contract snapshot adapter per factory; approved-outside
reservation and routing adapter; governed checkpoint/result normalization; then
negative tests for cross-factory IDs, outside conflicts, missing predecessors,
stale snapshots and restore tampering. Run actual manager-approved successive
requests before 25/50/100/200 scaling. Preserve all admitted/excluded identities
and report targets exceeding eligible source-supported TEST work explicitly.

No scheduler, database or benchmark artifacts changed during this handoff
inspection. The only change is this progress entry. Previous 98-test milestone
remains historical evidence, not evidence for the unimplemented six-factory path.
