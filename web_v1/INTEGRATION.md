# Isolated scheduling integration — not live admission

The default API on port 8000 remains a mock demo. Opt-in governance on port
8001 persists application records in `backend/craft_web_db.sqlite3` (SQLite
development database, **not** a provisioned production MySQL `craft_web_db`).
There is no legacy database alias and no migration path to `garment_fyp_db`.

React → authenticated DRF request → persistent QUEUED run → separate worker
process → CRAFT subprocess → canonical solve/materialization/replay/physical
and scope validation → source quantity reconciliation → candidate → manager
review → atomic **TEST-scope** promotion.

Three separate application TEST scopes are admitted: the deterministic synthetic
residual fixture (`TEST`) and a restricted, captured-source Factory-1 simulated
continuation fragment (`TEST_SIM`), plus an independently isolated two-order
priority fixture (`TEST_PRIORITY`). The Factory-1 fragment schedules order 665's two
residual tails and unstarted successor, carries two hypothetical in-flight
machine/worker holds, and requires both rolling windows plus an independent
assembled check. It does **not** admit all 2,879 factory operations or resolve
the 82 real partial operations. An authorization string in simulation evidence
is not a substitute for operational confirmation. Both workers invoke the
existing solver, not simulated scheduling success.

## Start from repository root in PowerShell

Set `DJANGO_SECRET_KEY` from your local secret store in every integration backend
terminal. Do not put it in source control. Then:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
# Set $env:DJANGO_SECRET_KEY securely before running the following commands.
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py check
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py migrate
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py init_craft_test_scope
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py init_craft_sim_scope
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py init_craft_priority_scope
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py createsuperuser
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py runserver 127.0.0.1:8001
```

These migrations affect only the local application SQLite file. A non-admin
manager additionally needs `governance.request_run`, `governance.promote_schedule`
and a `FactoryAccess` row for each permitted scope. Browser role labels confer no permissions.

Run the original demo API in a separate terminal, with its original settings:

```powershell
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py runserver 127.0.0.1:8000 --settings=config.settings
```

Run the worker in a third terminal (same secure secret/settings environment):

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
$env:CRAFT_WORKER_PYTHON = (Resolve-Path 'venv\Scripts\python.exe').Path
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py craft_worker
```

Each invocation handles one queued job. A development supervisor can repeat
this command; production queue/service deployment is not implemented. The
subprocess has a 180-second timeout. Cancellation prevents admission of a late
result; it does not immediately kill its subprocess. Worker startup expires
PREPARING runs older than ten minutes; retry requires a new idempotency key.
Do not run concurrent development workers until deployment-specific load and
concurrency testing is complete.

```powershell
cd web_v1/frontend
npm.cmd run dev
```

Open `http://127.0.0.1:5173/craft/governance`. The Vite proxy sends governance
requests to port 8001 and existing demo requests to port 8000. Session login
uses CSRF protection. Never expose these development servers to a factory network.

After a validated TEST run, open `http://127.0.0.1:5173/schedule` and switch
from **Illustrative demo** to **Validated TEST schedules**. This read-only view
loads persisted candidate operations through the governance API, displays
prior-active changed positions as outlines, and withholds bars if the stored
validation evidence is incomplete. It is not a live production Gantt. Manager
approval and rejection remain on the governance page.
Selecting `TEST_SIM` in governance queues the source-derived Factory-1 partial
chain; the page and Gantt label it as simulated continuation. Selecting `TEST`
retains the earlier synthetic fixture proof. These scopes have independent
candidate histories and active TEST versions.
Selecting `TEST_PRIORITY` shows two explicitly synthetic competing orders,
their current manager priority, and a controlled priority form. Run the
baseline first and approve it in TEST, record order B as urgent, then queue
another run. The target influences CRAFT's delivery objective but cannot
override physical constraints. Comparison reconstructs completion, tardiness,
and makespan from persisted assignments. Priority changes invalidate earlier
unapproved candidates through the source fingerprint; audit history records
the request and approval. Priority changes require the additional
`governance.change_priority` permission.

## API

All below are `/api/v1/governance/`: `session`, `login`, `scopes`, `runs`,
`runs/{id}/cancel`, `scopes/{id}/priority`, `events`, `candidates`, `candidates/{id}/compare`,
`candidates/{id}/promote`, `candidates/{id}/reject`.
Lists are paginated and factory-scoped. POST run returns 202 without importing
the scheduler. Candidate payload hashes and exact validation statuses are
rechecked before promotion. Compare-and-swap base-version/source checks and a
partial unique constraint prevent multiple active TEST versions. Rejection,
failure, cancellation and computational limits preserve the active version.

## Verification

```powershell
venv\Scripts\python.exe web_v1/audits/industrial_readiness_v1/run_fixture.py
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance
venv\Scripts\python.exe -m pytest web_v1/backend/integrations/craft/tests -q -p no:cacheprovider
cd web_v1/frontend
npm.cmd test
npm.cmd run lint
npm.cmd run build
```

The governance end-to-end test authenticates with CSRF, requests through the
API, launches the actual subprocess, retrieves/comparisons the candidate and
promotes into a disposable TEST database. Browser tests are separate and must
not be represented as live-factory acceptance.

Run the isolated Edge browser workflow from the repository root. It creates a
disposable application SQLite database, starts loopback-only Django/Vite
services, runs the real CRAFT worker, checks the controlled Gantt bars, and
performs TEST-only promotion. It never connects to the legacy factory DB:

```powershell
web_v1\backend\.venv\Scripts\python.exe web_v1/audits/fyp_completion_20261010/run_browser_e2e.py
```

This requires the installed Edge browser and `npm.cmd install` in
`web_v1/frontend` (Playwright is a dev dependency). See the timestamped
`browser_e2e_*.json` evidence and `browser_controlled_gantt.png` in that audit
directory. The browser proof covers the restricted TEST_SIM fragment and a
separate synthetic two-order urgent-rescheduling flow, not all Factory-1
operations or a production deployment.

The Factory-1 simulation proof can also be reproduced without Django:

```powershell
venv\Scripts\python.exe web_v1/audits/fyp_completion_20261010/run_partial_chain_smoke.py
venv\Scripts\python.exe web_v1/audits/fyp_completion_20261010/validate_partial_chain.py
venv\Scripts\python.exe -m pytest web_v1/audits/fyp_completion_20261010/test_partial_chain.py -q
```

Its source and scenario hashes are pinned; the script refuses to overwrite
different existing evidence. Two accepted windows are documented in
`audits/fyp_completion_20261010/two_window_partial_chain_v2.json`.

## Stage-A TEST resource-state proof

`TEST_RESOURCE` is a separate, synthetic application-owned scope. Its run
snapshot seals source identity, explicit time-zone label, operation quantity
ledger, calendar/eligibility revisions, fixed holds, active schedule version,
outside approved assignments, route boundaries, and selected operation keys.
The existing `craft_web_db` ScheduleVersion remains the approval authority;
the snapshot does **not** create an observed factory execution record.

Initialize only an application-owned development database, then grant a
manager `FactoryAccess` and `request_run`/`promote_schedule` permissions through
the existing governance process:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
# Set DJANGO_SECRET_KEY from your local secret store first.
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py migrate
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py init_craft_resource_scope
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_global_resource_state
```

The last command uses a disposable test DB and exercises the actual CRAFT
subprocess twice: approve operation A; export its ACTIVE assignment; schedule
operation B with A as an outside reservation; validate and approve B. It also
tests overlap, stale-state, quantity/route evidence, worker failure, and
factory isolation. A TEST-only service, `select_test_resource_work`, changes
which synthetic operation enters the next bounded window. There is no live
admission endpoint or automatic production promotion.

The pinned Factory-1 simulation can be rechecked separately:

```powershell
venv\Scripts\python.exe web_v1/audits/global_resource_state_stage_a/run_factory1.py
```

It writes only into `audits/global_resource_state_stage_a/`. Its snapshot
binds all 35 **hypothetical** fixed holds and the 25 selected identities; the
independent assembled validator still supplies exact source-calendar, route,
and quantity checks. The global snapshot's broad 30-day calendar envelope is
only an additional conflict gate, not a substitute for that exact validator.
The capture has **zero** application-approved Factory-1 outside assignments,
so it cannot establish complete factory-wide conflict closure or legal-domain
coverage. No comparison of this run's memory/time against the older run is a
controlled speedup experiment.

### Application-approved Factory 1 TEST continuation

The separate `TEST_FACTORY1` integration test closes the application-approval
gap for the pinned simulation. It runs a first 12-operation CRAFT region through
the background worker and actual manager `promote` service, exports that ACTIVE
version's 12 assignments, then schedules the remaining 13 source-derived
operations against those assignments and all 35 hypothetical in-flight holds.
Both regions use bounded rolling windows. The second run checks its saved
materialization with per-window exact replay, physical and scope validation,
the independent captured-source assembled validator, and the sealed global
conflict validator. A second candidate from the same state is rejected after
the first is promoted. Both approvals and the prior-version history exist in
the disposable Django TEST database during the test; no live schedule is
activated.

Reproduce from the repository root:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_integration'
$env:DJANGO_SECRET_KEY = 'test-only-stage-a-secret'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_factory1_governed --verbosity 1
```

The source is the same pinned Factory 1 capture, not a legacy DB read. The
12/13 split is deliberate: the earlier 8/17 split could approve a feasible
but late first route segment, making the successor region's restricted domain
infeasible. That computational/domain result was not classified as physical
infeasibility. This integration still uses a bounded, incomplete legal
resource domain; it does not prove factory-wide competitor closure or all
2,879 operations schedulable. The full proof and command results are in
`audits/global_resource_state_stage_a/factory1_approved_integration_report.md`.

### Checkpointed TEST Factory 1 cycles (partial integration)

An isolated `TEST_FACTORY1` scope may now include `resource_policy:
stage_b_adaptive`, `cycle_target`, and `window_size` in its application-owned
`resource_state`. The scope's `selected_keys` must remain within the pinned
25-operation admission set. A scheduling request snapshots these options,
the sealed global resource state, and the implementation hash. One invocation
of `craft_worker` advances at most one bounded CP-SAT window. A validated
window is saved in the run's `CHECKPOINTED` result, marked
`TENTATIVE_NOT_APPROVED`; it is not an active schedule. Invoke the worker again
to resume until the run becomes `VALIDATED` or reaches a terminal limit.
Only a final independently validated candidate may be promoted by an
authorized manager. Cancelling or failing a run leaves the active TEST version
unchanged. The worker retains its 180-second limit per invocation.
The configured window ceiling is 25, while the proven demonstration below
uses windows of two operations. Raising a numerical ceiling is not evidence
that a 25-operation CP-SAT window will validate. An identity outside the
pinned admission set is rejected with a reason; the 2,879-entry global ledger
alone does not supply a verified per-operation crew requirement or full
resource/calendar execution contract.

The disposable Django test below provisions the controlled scope, executes
four two-operation windows over two cycles, simulates worker-lease recovery,
checks independent/global validation, and performs manager TEST approval.
It requires the repository scheduler virtual environment and writes only to
Django's isolated test database:

```powershell
$env:DJANGO_SECRET_KEY = 'isolated-cycle-test-key'
web_v1\backend\.venv\Scripts\python.exe web_v1/backend/manage.py test governance.test_factory1_cycles --settings=config.settings_integration --noinput
```

See `audits/factory_cycles_v1/report.md` for measured results and known
limitations. There is not yet a manager-facing cycle-configuration API or
general 200-operation admission policy. A failed window stops safely; the
default V1 controller does not schedule unrelated work past that failure.

### Opt-in adaptive retry checkpoints (TEST only)

An admitted `stage_b_adaptive` cycle recipe may additionally set
`adaptive_retries: true`. V2 checkpoints distinguish attempts from accepted
windows: rejected groups split to two operations and then singletons. A failed
singleton is explicitly deferred; unresolved successors cannot bypass it, while
independent pending work may proceed. Every restored accepted prefix is
independently physically revalidated. The worker advances one attempt per
invocation, with the existing 180-second limit. A partial terminal checkpoint
is not a promotable schedule. No checkpoint, including a successful intermediate
one, is manager approval.

Evidence and reproducible commands are in
`audits/governed_adaptive_recovery_v1/report.md`. The eight-operation injected
search-limit/restart/approval test passed, as did the separate 12+13
approved-outside regression (25 consolidated TEST assignments, seven windows,
both manager approvals). Default V1 two-cycle recovery also passed. This does not expand the
25-key source admission boundary or establish full legal-domain coverage.

## Remaining release gates

- Authoritative remaining-work and transfer/commitment evidence for live partial operations.
- Full-state admission of all 35 simulated holds and 82 partials. The bounded
  order-665 proof covers only two holds and cannot establish factory-wide
  precedence/resource conflict closure.
- Live neighborhood conflict closure, authoritative production approved outside
  schedules, and source freshness ingestion. Stage A now proves an actual
  application-approved Factory-1 **TEST** outside version, not a live one.
- Production application DB, worker supervisor, secrets, TLS, login rate limiting,
  backup/restore drills and actual concurrent promotion/recovery stress tests.
- Live production Gantt integration and factory-scale acceptance budgets. The
  controlled TEST Gantt was visually reviewed in an isolated browser run.
- A conflict-closed multi-window Factory-1 neighborhood with all legal resource
  alternatives. The accepted order-665 two-window fragment carries two
  hypothetical holds but remains a restricted-domain test.
- Factory-source order priority override and conflict-closed repair remain
  unproven. The working priority workflow is deliberately limited to an
  isolated synthetic fixture; historical orders are not changed.
- ERP/MOM modules remain demo-only; this is not a completed ERP deployment.

Back up the application DB and immutable run evidence together. Do not roll
back by editing active flags or restoring only one table. Stop workers before
development DB restore, verify hashes and source freshness, and require review
before any scheduling resumes. Production backup/restore remains unvalidated.
