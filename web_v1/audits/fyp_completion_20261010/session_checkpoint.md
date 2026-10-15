# FYP completion continuation checkpoint — 2026-10-10

This is controlled TEST evidence, not live-factory admission or a production release.

## Verified vertical slice

- Captured Factory-1 source fingerprint `010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438`; source contains 2,879 operation identities (171 completed, 82 partial, 2,626 unstarted). The scenario remains explicitly simulated; 82 live partials remain blocked.
- Source-derived order-665 fragment schedules residuals `665_2677` and `665_2678` and successor `665_2679`, with two fixed hypothetical in-flight holds. One bounded three-operation solve and two rolling windows were accepted. Actual exact replay, physical validation and scope validation passed for each accepted result. The assembled independent check passed quantity, fixed hold, worker/machine, calendar, identity and precedence checks. See `partial_chain_smoke.json`, `two_window_partial_chain_v2.json`, and `partial_chain_independent_validation.json`.
- Django's isolated `TEST_SIM` scope queues the real CRAFT subprocess, persists its validation-gated candidate, compares it and permits authorized TEST-only promotion. Governance suite: 25 passed. The original `TEST` synthetic fixture also remains supported.
- Playwright Edge ran the authenticated React→Django→background worker→CRAFT→candidate→controlled Gantt→manager TEST promotion path in a disposable application SQLite DB: `browser_e2e_20261010_035741.json` PASS, 1 browser test, 43.22 s runner wall time. The test opened an operation drawer and verified worker identity. `browser_controlled_gantt.png` shows visible validated operation bars at day zoom. No legacy DB connection is used by the isolated proof.
- Final targeted regressions: CRAFT integration 89 passed plus 24 subtests, 2 warnings; continuity 59 passed, 3 deselected, 2 warnings; frontend 12 passed with chart-size warnings in jsdom; Django governance 25 passed; frontend lint and build passed; Django check and migration check passed. Base demo API has no tests (`Ran 0 tests`), so it is not counted as tested.

## Protection and test environment caveats

- The first sandboxed continuity run had 53 passes and six setup errors caused by `Access is denied` on the existing Windows pytest temp directory; its permitted rerun passed 59. Sandboxed Vite test/build were blocked reading `vite.config.ts`; permitted reruns passed. No test assertions were changed to remove these failures.
- Read-only comparison against the earlier 7,640-entry protection manifest found **zero modified files that existed in the manifest** under `outputs/`, but 148 added and 22 removed output-tree paths. The removals include old `pytest_basetemp` files. Therefore the full output tree is not byte-for-byte identical to that older manifest, and `FROZEN_ARTIFACTS_MODIFIED=0` must not be claimed without narrowing and re-verifying the frozen benchmark scope. No file in `outputs/` was deliberately edited in this session.
- Playwright installation reported three npm audit advisories (one moderate, two critical); these require dependency review before release. No `npm audit fix --force` was run.

## Incomplete, ordered next milestones

1. Expand the restricted fragment into a conflict-closed Factory-1 neighborhood with all legal resource alternatives, relevant outside commitments and multiple partial tails. The present proof covers only 2 of 35 simulated holds and 3 of 2,879 operation identities.
2. Implement and prove a governed urgent-order request whose priority actually affects the CRAFT objective, followed by a second validated candidate, comparison, and TEST manager decision. Current demo priority badges do not change governed scheduling.
3. Run progressive Factory-1 bounded windows with recorded preparation/solver/replay timings, memory, complete identity accounting and independent assembled validation. Do not claim full 2,879-operation scalability yet.
4. Resolve global demo-shell versus controlled TEST labeling where the shared top bar can be misleading, extend browser tests to rejection/stale/error paths, and review dependency advisories.
5. Obtain authoritative factory continuation evidence and operational acceptance before any live scheduling. Keep production activation disabled.

Use [checklist.md](checklist.md) for DONE/IN_PROGRESS/BLOCKED status and [../../INTEGRATION.md](../../INTEGRATION.md) for startup commands.

## Next completion phase (same date, later checkpoint)

- Added an isolated `TEST_PRIORITY` scenario with two synthetic, resource-contending orders and an authorized, revisioned manager priority request. A baseline and B-URGENT run use identical physical state and the existing CP-SAT delivery objective. B's completion changed from 3,120 to 900 seconds, A's from 1,020 to 3,120 seconds; makespan stayed 3,120 seconds. This is an explicit tradeoff, not proof of general improvement. Both candidates passed exact replay, physical/scope and quantity gates. Details: `../priority_rescheduling_v1/report.md`.
- The background worker, Django application-owned priority records, comparison, audit history, stale-candidate rejection, React form, measured comparison, and TEST-only promotion are integrated. The page now filters runs/candidates/events to the selected scope. No Factory-1 historical order priority has been admitted.
- Latest regressions: governance 27 passed; CRAFT/integration targeted suite 132 passed, 24 subtests, 2 dependency warnings; frontend 13 passed (including cross-scope display isolation), lint and production build passed; Django migration check detected no changes. Final Edge browser rerun passed both scenarios in 91.52 seconds: `browser_e2e_20261010_142956.json`. An intermediate timeout caused by not reselecting the scope after navigation is retained as `browser_e2e_20261010_142205.json`; browser test fixed.
- Factory-1 conflict-closed neighborhood, full legal resource alternatives, all 35 simulated holds, complete 2,879-operation scheduling, and live production evidence remain unproved. Priority implementation is limited to synthetic TEST data. The next engineering milestone is still source-consistent conflict closure and independently validated bounded Factory-1 scheduling.

## 2026-10-13 checkpoint: tentative multi-cycle TEST integration

- Added a sealed, source-bound tentative cycle checkpoint contract and one-window-per-worker Factory-1 path. The disposable Django integration test scheduled eight source-derived operations in four validated windows across two cycles; a simulated abandoned worker lease resumed from an accepted checkpoint. Independent assembled and global-conflict validation passed, and an authorized manager promoted only the final candidate to the TEST active version. No tentative checkpoint itself was approved.
- Regression results: focused CRAFT 46 passed; rejected-window negative test 1 passed; Stage A/Stage B Django suite 13 passed in 623.977 s; separate stale-resource checkpoint test 1 passed; general governance 27 passed in 145.001 s; frontend 13 passed, lint and build passed. The earlier accepted eight-operation test took 206.025 s including Django setup and four worker subprocesses. Browser automation was not rerun in this checkpoint.
- The sealed global TEST state has 2,879 operation identities and 35 hypothetical holds, but source-admissible scheduling selection remains capped to the pinned 25-operation set. The saved 35-operation adaptive attempt accepted only 28; window eight reported a resource-domain expansion limit. This is not physical infeasibility. No 50/100/200-operation cycle is proven, and manager priority is not yet wired into Factory-1 cycle selection.
- A rejected window now terminates as a computational limit instead of becoming a falsely resumable checkpoint. Scheduling unrelated ready work after such a failure, full-factory admission, approved-outside work together with the new cycle path, and browser verification remain next gates. Full details and reproduction commands: `../factory_cycles_v1/report.md` and `../../INTEGRATION.md`.
