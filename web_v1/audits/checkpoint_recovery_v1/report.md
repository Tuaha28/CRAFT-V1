# Tentative checkpoint recovery — 2026-10-14

Status: controlled recovery validated; industrial readiness NOT ESTABLISHED.

## Changes

- `crafter/production/checkpoints.py`: checksummed JSON, immutable publication,
  file flush/fsync, fail-closed identity verification. No pickle or approval.
- Factory 4/6 capture runner: `--resume` and `--max-new-attempts`; reconstruct
  machine reservations, worker segments and exact route ends from independently
  revalidated rows. Identity pins source/snapshot/scenario, selected keys,
  runner implementation and search-policy settings.
- Added missing-source-predecessor rejection to assembled-prefix admission.
  An adversarial omission previously passed because scope was derived only from
  supplied rows. Correctly generated prior cohorts already contained predecessors.
- Existing Factory 1 runner: independent source/ledger/calendar/routing validation
  of restored prefixes BEFORE adding their reservations or invoking CP-SAT.
- Existing governed cycle adapter exposes restore validation; manager approval
  and existing TEST schedule lifecycle are unchanged.

## Evidence

Factory 6 original 10-operation cohort:

- Stop after first real accepted window: 2 operations, 2.19 seconds.
- Separate-process resume: restored 2 assignments unchanged; completed 10/10,
  8 accepted windows, 2 rejected-and-recovered attempts, 201.83 seconds,
  507.07 MiB start/end sampled RSS.
- Replay PASS, physical VALID, scope SCOPE_VALID; no remaining identities.
- Restore completed checkpoint: 10/10 unchanged, no new window/solve, 1.29 seconds.

Artifacts in `../factory46_operation_schedule_v1/`:

1. `factory6_10_execution_contract_v1_20261013T232725951775Z.json`
2. `factory6_10_execution_contract_v1_20261013T232800442764Z.json`
3. `factory6_10_execution_contract_v1_20261014T013703938518Z.json`

After predecessor-omission hardening changed the runner fingerprint, a fresh
bounded stop/resume check passed: 2 accepted then 4 accepted, 2.11 + 3.21 seconds:
`factory6_10_execution_contract_v1_20261014T014041073836Z.json` and
`factory6_10_execution_contract_v1_20261014T014124512593Z.json`.
Older checkpoints intentionally fail runner-identity verification after code edits.

## Tests

- Focused checkpoints, runner, cycle accounting, execution contracts and replay:
  58 passed, zero failed, 31.05 seconds.
- Actual Factory 1 CP-SAT prefix with corrupted/resealed checkpoint:
  1 passed, zero failed, 74.43 seconds; next solver invocation prohibited by test.
- Existing Django three-test cycle suite before restore hardening:
  3 passed, 342.98 seconds, disposable in-memory database destroyed afterward.
- Post-hardening Django suite: 3 passed, zero failed, 231.77 seconds. Four real
  worker windows across two cycles, restore validation PASS on each resumed
  window, independent/global acceptance, abandoned-worker recovery, stale-state
  rejection and manager-authorized TEST approval all passed. Test DB destroyed.
- Final cycle/checkpoint/terminal-worker-failure regression: 8 passed, zero
  failed, 12.86 seconds (seven overlap prior suites). Total distinct tests in
  these successful final invocations: 63. Dependency deprecation warnings only.

The controlled stop test is not an OS power-loss test. Unit tests simulate failure
during checkpoint publication and confirm the previous file remains usable.
Checksums detect corruption; they are not signatures or authorization.
The store is not a database lease or a multi-manager concurrency service.
Filesystem power-loss durability and production database concurrency remain
deployment acceptance gates. Reference: https://docs.python.org/3/library/os.html
(file flush/fsync and filesystem primitives).

## Reproduction

```
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 6 10 --max-new-attempts 1
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 6 10 --resume <new-checkpoint-path> --max-new-attempts 1
venv\Scripts\python.exe -m pytest tests/test_tentative_checkpoints.py tests/test_factory46_operation_schedule.py tests/test_factory_cycles.py tests/test_operation_execution_contract.py tests/test_replay_semantics.py -q
venv\Scripts\python.exe -m pytest tests/test_factory1_checkpoint_physical.py -q
```

PowerShell, disposable application DB only:

```
$env:DJANGO_SECRET_KEY='disposable-recovery-test-only'
$env:CRAFT_WEB_DB_PATH=':memory:'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_factory1_cycles --settings=config.settings_integration --noinput --verbosity 1
```

No legacy database connection or write is part of these runs. Capture runners
install connection tripwires. Django uses its isolated TEST database; no existing
application records are changed. No frozen benchmark output is overwritten.
Production activation remains disabled. Repository-wide frozen-manifest integrity
was not newly certified.

## Subsequent scaling gate: Factory 6, 25 operations

After recovery and governed integration gates passed, the same pinned Factory 6
source and unchanged TEST assumptions were used for 25 eligible operations.
Three distinct processes each ran at most two new attempts, restoring the prior
checkpoint. Final result: **25/25, six accepted windows, zero rejected windows**.
Window sizes: 5, 4, 4, 4, 4, 4. Final independent assembled replay PASS, physical
VALID, scope SCOPE_VALID, captured-boundary conflicts VALID, missing predecessor
set empty. Every accepted assignment from earlier segments was compared with
the final rows and remained exactly unchanged. Source/snapshot/scenario hashes,
assumptions and selected identities match across all three segments.

Versioned outputs in `../factory46_operation_schedule_v1/`:

- `factory6_25_execution_contract_v1_20261014T014330178062Z.json`: 9 accepted; 11.30s.
- `factory6_25_execution_contract_v1_20261014T014407794727Z.json`: 17 accepted; 122.82s.
- `factory6_25_execution_contract_v1_20261014T014643662677Z.json`: 25 accepted; 50.30s.

Sum of measured in-run wall times: 184.42 seconds; this excludes Python startup,
shell orchestration and pauses between runs. Maximum start/end sampled RSS:
537.10 MiB, not a continuously measured peak. The slow middle segment is retained
as evidence; no causal speedup or baseline superiority is claimed.

All 10 unresolved real partials remain blocked, 19 completed operations excluded,
and three captured resource holds quarantined under the unchanged TEST policy.
This is not a complete Factory 6 schedule: only the selected cohort is complete.
Legal configuration and calendar domain completeness remain unproven. No new
production records, manager approvals or fabricated fixed future demand were
created by this offline experiment.

Command pattern:

```
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 6 25 --route-ready-objective --max-new-attempts 2
venv\Scripts\python.exe web_v1/audits/factory46_operation_schedule_v1/run_factory_test.py 6 25 --route-ready-objective --max-new-attempts 2 --resume <previous-segment-final-checkpoint>
```

## Remaining work (not silently waived)

The governed Factory 1 service still stops on a rejected window; adaptive retry
is currently validated in the Factory 4/6 runner. This is NOT a completed
generalized adaptive backend or a six-factory service. Before expanding that API,
preserve approved-state fingerprints and independent source admission. Full
calendar/domain coverage, factory-approved staffing and productivity rules,
actual continuation evidence, six-factory workloads, FIFO/EDD/SPT comparisons,
operational monitoring and factory acceptance remain incomplete.
