# Auto-planning, Manager Email, Priority Orders and Demo Write-back: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CRAFT re-plans by itself after floor disruptions, new orders or Priority changes. It emails the managers, a manager approves, and approved plans are written into demo copies of the factory databases.

**Architecture:** Every new behaviour changes CRAFT's *input* only, through new keys in the what-if copy built by `integrations/craft/plan_scenario.py`. CRAFT code is never touched. A watcher inside the existing `planning_worker` loop turns stored triggers into Quick disruption runs, with debounce and guards. Email and write-back are side effects in the worker and in `decide`, and each failure is recorded as a `PlanEvent`, never raised.

**Tech Stack:** Django 5 + DRF, SQLite (app DB), MySQL via `pymysql` (`LegacyExecutionRepository`), React 19 + TanStack Query + Vite, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-16-auto-planning-design.md`. Read it first; this plan argues from it.

## Global Constraints

- CRAFT code (`crafter/`, `scratch/run_craft_v2.py`, V3 settings in `planning/jobs.py MODES`) is not modified.
- The original databases (`garment_fyp_db`, `garment_fyp_db_seed_v2_data2_load{30,60,90}_…`) are never written. Writes go only to `<original>_demo`, through the writer account, for (1) an approved plan's schedule and (2) machine status.
- Every new behaviour is opt-in. With auto-planning off, no Priority set and `CRAFT_WRITEBACK` unset, the app behaves exactly as today and every existing test passes unchanged.
- No automatic approval, ever. Automatic runs are always `mode='QUICK'`.
- Client UI wording is CRAFT only: no FIFO/EDD/SPT/CP-SAT/V3/paper/experiment words.
- Secrets (email password, writer password) only in `web_v1/backend/.env` (gitignored). Tests use Django's locmem email backend and mocked DB connections, never real credentials.
- Settings defaults: `AUTO_PLAN_QUIET_MINUTES=15`, `AUTO_PLAN_MAX_WAIT_MINUTES=60`, `AUTO_PLAN_NEW_ORDER_CHECK_MINUTES=15`, `AUTO_CAPTURE_HOUR=2`, `CRAFT_DEMO_SUFFIX='_demo'`.
- Backend tests: `cd web_v1/backend && DJANGO_SECRET_KEY="test-$RANDOM" CRAFT_WEB_DB_PATH="" .venv/Scripts/python.exe manage.py test --settings=config.settings_integration planning assistant erp floor api governance.tests governance.test_security_headers`. Never run the full `governance` suite: it starts real CRAFT runs.
- Frontend tests: `cd web_v1/frontend && npx tsc -b && npx vitest run`.
- Commits are path-scoped (`git commit -- <paths>`), with author `-c user.name="Muhammad Tuaha" -c user.email="muhammadtuaha094@gmail.com"`, and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- One CRAFT run at a time on this laptop. Live proof runs use Factory 6 with 3 orders.

## Review Focus

1. **A burst of reports** (5 machine-down reports in 3 minutes) must produce exactly one automatic run and one email, not five. Pinned in Task 7 (`test_burst_of_triggers_makes_one_run`).
2. **An issue resolved inside the quiet window** must not produce a run when it was the only trigger. Pinned in Task 7 (`test_resolved_issue_inside_window_makes_no_run`).
3. **A plan approved while write-back fails** (MySQL down) must stay approved and show "Not written to the demo data". Pinned in Task 10 (`test_writeback_failure_keeps_approval_and_records_event`).
4. **No manager has an email address**: the run still finishes and the run page gets the "nobody was told" event. Pinned in Task 8 (`test_no_recipients_records_event`).
5. **The material chosen in a material-short report is used by no order in the plan**: the issue is created, no trigger, and a clear message. Pinned in Task 5 (`test_material_used_by_no_planned_order_makes_no_trigger`).

---

### Task 1: Only three data sources (hide "Factory database")

**Files:**
- Modify: `web_v1/backend/planning/models.py` (`Dataset`)
- Create: `web_v1/backend/planning/migrations/0003_dataset_active.py`
- Modify: `web_v1/backend/planning/views.py` (`datasets` view)
- Test: `web_v1/backend/planning/tests.py`

**Interfaces:**
- Produces: `Dataset.active: BooleanField(default=True)`. Every dataset list the UI reads is filtered `active=True`. Older runs that reference a hidden dataset still load.

- [ ] **Step 1: Write the failing test** `test_hidden_dataset_is_not_listed_but_its_runs_still_load` in `planning/tests.py`. Create two datasets, set `active=False` on the one with `database='garment_fyp_db'`, then GET `/api/v1/planning/datasets`. Assert its id is absent and the other is present. Assert GET `/api/v1/planning/runs/<id>` of a run on the hidden dataset returns 200.
- [ ] **Step 2: Run it.** Expected: FAIL (`Dataset` has no field `active`).
- [ ] **Step 3: Implement.** Add the field. The migration adds it, then a `RunPython` sets `active=False` where `database='garment_fyp_db'` (reverse: no-op). Filter the `datasets` view with `.filter(active=True)`. Also exclude inactive datasets in `planning/datasets.py discover()` registration, so a re-discover doesn't re-activate it (`update_or_create` must not overwrite `active`).
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `planning: hide the v1 factory database; keep 30/60/90 load`.

### Task 2: Demo copies — name rule, setup document, read switch

**Files:**
- Modify: `web_v1/backend/integrations/craft/legacy_repository.py:15` (`SOURCE_DATABASE_PATTERN`)
- Modify: `web_v1/backend/erp/db.py` (add `live_database`)
- Modify: `web_v1/backend/config/settings_integration.py` (`CRAFT_DEMO_SUFFIX`, `CRAFT_USE_DEMO_COPIES`)
- Create: `web_v1/docs/demo_copies.md`
- Test: `web_v1/backend/erp/tests.py`

**Interfaces:**
- Produces: `erp.db.live_database(dataset) -> str`. It returns `dataset.database + settings.CRAFT_DEMO_SUFFIX` when `settings.CRAFT_USE_DEMO_COPIES` is true, else `dataset.database`. Every ERP view and capture calls this instead of reading `dataset.database` directly.
- Produces: `SOURCE_DATABASE_PATTERN = r"garment_fyp_db(?:_seed_v2_data2_load\d+_\d{8}_\d{6})?(?:_demo)?"`.

- [ ] **Step 1: Write the failing tests** in `erp/tests.py`:
  - `test_demo_suffix_matches_source_pattern` asserts that `re.fullmatch(SOURCE_DATABASE_PATTERN, 'garment_fyp_db_seed_v2_data2_load30_20261018_083214_demo')` matches and `'garment_fyp_db_demo_x'` does not.
  - `test_live_database_uses_copy_only_when_switched_on` checks both values of `CRAFT_USE_DEMO_COPIES` with `override_settings`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Change the pattern.
  - Add `live_database`.
  - Add the settings: `CRAFT_USE_DEMO_COPIES = os.environ.get('CRAFT_USE_DEMO_COPIES') == '1'`, plus the same key read from `.env` the way `ASK_AI_ENV_FILE` is read.
  - Replace each `read_only(dataset.database)` call site in `erp/views.py` with `read_only(live_database(dataset))`.
  - Write `web_v1/docs/demo_copies.md`. It holds, for each of the three load databases, the `CREATE DATABASE <name>_demo`, `mysqldump <name> | mysql <name>_demo`, `CREATE USER 'craft_writer'@'localhost' IDENTIFIED BY '<you choose>'`, and `GRANT ALL PRIVILEGES ON <name>_demo.* TO 'craft_writer'@'localhost'` (all rights on the three copies only, which the reset in Task 10 needs; no rights on any original) lines, then the `.env` keys `CRAFT_WRITER_USER`, `CRAFT_WRITER_PASSWORD` and `CRAFT_USE_DEMO_COPIES=1`. The user runs these, not the implementer.
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `erp: read demo copies when switched on; setup guide for the copies and writer account`.
- [ ] **Step 6: Hand-off.** Tell the user that `demo_copies.md` is ready to run. Tasks 3–9 don't need the copies; Task 10's live check does.

### Task 3: Scenario keys — priority, stopped lines, delayed release (and prove priority)

**Files:**
- Modify: `web_v1/backend/integrations/craft/plan_scenario.py`
- Create: `web_v1/backend/integrations/craft/tests/test_plan_scenario.py`

**Interfaces:**
- Produces: `plan_scenario.apply(source: dict, factory_id: int, scenario: dict) -> dict`, a pure function holding today's mutation code from `build()`. `build()` calls it, then hashes and writes as before.
- New scenario keys:
  - `priority_orders: list[str]` sets each listed order's `delivery_deadline` to `source['reference_time'][:10]`. An unknown id raises `SystemExit('PRIORITY_ORDER_NOT_FOUND:<id>')`.
  - `lines_stopped: list[int]` sets every machine of the factory with that `line_id` to `current_status='Breakdown'`. An unknown line raises `SystemExit('LINE_NOT_IN_FACTORY:<id>')`.
  - `release_after: dict[str, str]` maps order id to an ISO date and sets that order's `scheduled_start_date = max(current, date)`. An unknown id raises `SystemExit('RELEASE_ORDER_NOT_FOUND:<id>')`.

- [ ] **Step 1: Write the failing tests** in `test_plan_scenario.py`. Each one builds a minimal `source` dict: 2 orders, 3 machines on lines 5 and 6, `reference_time '2026-09-10T00:00:00'`.
  - `test_priority_sets_due_to_plan_start`: the order's `delivery_deadline == '2026-09-10'`, and the other order is unchanged.
  - `test_line_stopped_breaks_every_machine_on_it`: both line-5 machines are `'Breakdown'`, and the line-6 machine is unchanged.
  - `test_release_after_never_moves_a_start_earlier`: a start of `'2026-09-20'` with `release_after '2026-09-15'` stays `'2026-09-20'`, and `'2026-09-25'` moves it.
  - `test_unknown_ids_are_rejected`: `assertRaises(SystemExit)` for each key.
  - `test_existing_keys_unchanged`: `machines_down` and `workers_absent` give the same result as before the refactor.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe manage.py test --settings=config.settings_integration integrations.craft.tests.test_plan_scenario`. Expected: FAIL (`apply` not defined).
- [ ] **Step 3: Implement** `apply` (move the existing body, add the three keys). Keep `build()`'s output byte-identical for the old keys.
- [ ] **Step 4: Run it.** Expected: PASS. Then run the backend suite: all pass.
- [ ] **Step 5: Commit** `scenario: priority orders, stopped lines and delayed release in the what-if copy`.
- [ ] **Step 6: Priority proof (live, gate for Task 6).** Run two Quick plans through the API on dataset 30% load, Factory 6, orders 1–3: one plain, one with `scenario={'priority_orders': ['5636']}`, the order that is on time in the plain plan. Use a temporary management command `prove_priority`, not committed; delete it after. Run them one at a time.

  Pass when the priority order's `finish_day` in `digest.json` `order_rows` is ≤ the plain plan's. Then repeat with `'4187'`, which is 4 days late in the plain plan. Pass when its finish is earlier or its lateness is lower. Record both numbers in the commit message of Task 6. If either check fails, stop and report to the user; do not change CRAFT.

### Task 4: Disruption requests carry all five kinds

**Files:**
- Modify: `web_v1/backend/planning/services.py` (`request_disruption`)
- Modify: `web_v1/backend/planning/models.py` (`PlanRun.trigger`, `decision` values)
- Create: `web_v1/backend/planning/migrations/0004_planrun_trigger.py`
- Test: `web_v1/backend/planning/tests.py` (`WhatIfTests`)

**Interfaces:**
- Produces: `request_disruption(user, *, parent_id, machines_down=(), workers_absent=(), lines_stopped=(), release_after=None, priority_orders=None, new_order=None, mode='QUICK', trigger=None) -> PlanRun`. It merges each key into the inherited scenario the way `machines_down` is merged today: lists are unioned and sorted, and in `release_after` the later date wins per order. It raises `ValidationError('Choose at least one change to plan around')` when every input is empty. `trigger` is stored on the run.
- Produces: `PlanRun.trigger = JSONField(default=dict, blank=True)` with shape `{'source': 'AUTO'|'MANUAL', 'reason': str, 'kinds': [str], 'issue_ids': [int], 'priority': {'on': [str], 'off': [str]}, 'new_order_ids': [str]}`.
- Produces: `decision` may also be `'SUPERSEDED'`.

- [ ] **Step 1: Write the failing tests.**
  - `test_disruption_merges_lines_release_and_priority` asserts `run.scenario == {'machines_down': [], 'workers_absent': [], 'lines_stopped': [86], 'release_after': {'11': '2026-09-15'}, 'priority_orders': ['12']}`.
  - `test_disruption_with_nothing_is_rejected` asserts `ValidationError`.
  - Keep the two existing `WhatIfTests` disruption tests passing unchanged. Their scenario dicts gain no new keys when the new inputs are empty: the empty keys are omitted.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The event message names every change in plain words: "line 86 stopped", "order 11 waits for material until 15 Sep", "order 12 marked Priority".
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `planning: disruption runs for stopped lines, late material and priority`.

### Task 5: Floor reports for the new disruptions

**Files:**
- Modify: `web_v1/backend/floor/models.py` (`Issue.KINDS`; fields `material_id`, `arrival_date`)
- Create: `web_v1/backend/floor/migrations/0002_issue_line_stopped_material.py`
- Modify: `web_v1/backend/floor/services.py` (`report_issue`, `replan_after`)
- Modify: `web_v1/backend/erp/queries.py` (add `orders_using_material`)
- Modify: `web_v1/backend/floor/views.py` and the serializer for the new fields
- Test: `web_v1/backend/floor/tests.py`

**Interfaces:**
- Consumes: `request_disruption(...)` from Task 4.
- Produces: `Issue.KINDS` gains `('LINE_STOPPED', 'Line stopped')`. New fields: `material_id = PositiveIntegerField(null=True, blank=True)` and `arrival_date = DateField(null=True, blank=True)`.
- Produces: `erp.queries.orders_using_material(cursor, material_id: int, order_ids: list[str]) -> list[str]`, the ids among `order_ids` whose product's `bill_of_materials` contains `material_id`.
- Produces: `floor.services.disruption_inputs(issues: list[Issue], plan_order_ids: list[str]) -> dict` with keys `machines_down`, `workers_absent`, `lines_stopped` and `release_after`, ready for `request_disruption`. The watcher in Task 7 reuses it.

- [ ] **Step 1: Write the failing tests.**
  - `test_line_stopped_needs_a_line`: a report with no `line_id` raises `ValidationError`.
  - `test_material_short_needs_material_and_arrival`: missing either raises `ValidationError`.
  - `test_replan_after_accepts_all_four_kinds`: with `orders_using_material` mocked to return `['11']`, `run.scenario['release_after'] == {'11': '<arrival>'}` and `run.scenario['lines_stopped'] == [<line>]`.
  - `test_material_used_by_no_planned_order_warns`: with the mock returning `[]`, the issue is created and the report response has `warning == 'No order in the current plan uses this material'`. Task 7 adds the trigger half of this check.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The `MATERIAL_SHORT` rule changes from "order required" to "material and arrival date required"; an `order_id` is optional. `replan_after` drops the "only machine and worker" check and calls `disruption_inputs`, then `request_disruption(..., trigger={'source': 'MANUAL', ...})`.
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `floor: line-stopped and late-material reports, re-plan around all four`.

### Task 6: Priority orders (only after the Task 3 proof passed)

**Files:**
- Modify: `web_v1/backend/planning/models.py` (`OrderPriority`)
- Create: `web_v1/backend/planning/migrations/0005_orderpriority.py`
- Modify: `web_v1/backend/planning/services.py` (`set_priority`, `priority_orders`; apply in `request_schedule` and `request_disruption`)
- Modify: `web_v1/backend/planning/views.py`, `planning/urls.py`
- Modify: `web_v1/frontend/src/lib/api.ts`, `src/pages/erp/Orders.tsx`, `src/pages/erp/OrderPage.tsx`, `src/pages/RunPage.tsx`, `src/components/DataTable.tsx` (badge)
- Test: `web_v1/backend/planning/tests.py`, `web_v1/frontend/src/lib/format.test.ts`

**Interfaces:**
- Produces: `OrderPriority(dataset FK, factory_id, order_id CharField(20), active bool, reason TextField(blank), set_by FK user, set_at auto_now)`, unique on `(dataset, factory_id, order_id)`.
- Produces: `services.set_priority(user, *, dataset_id, factory_id, order_id, on: bool, reason='') -> OrderPriority`, which needs `request_plan`, writes a `PlanEvent` `PRIORITY_ON`/`PRIORITY_OFF`, and records an `AutoTrigger` (Task 7; until then a no-op hook `_trigger(...)`).
- Produces: `services.priority_orders(dataset_id, factory_id) -> list[str]`, the active ones.
- Produces: `POST /api/v1/planning/priority` with body `{dataset, factory, order_id, on, reason}`, and `GET /api/v1/planning/priority?dataset=&factory=` returning `{'orders': [str]}`.
- Produces (frontend): `api.priority(dataset, factory)` and `api.setPriority(dataset, factory, orderId, on, reason)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_priority_orders_reach_new_plans`: after `set_priority(on=True)` for `'11'`, `request_schedule(...).scenario['priority_orders'] == ['11']`.
  - `test_priority_off_removes_it`.
  - `test_priority_needs_request_plan`: a floor supervisor gets `PermissionDenied`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement the backend.** `request_schedule` sets `scenario['priority_orders']` only when the list is non-empty, so the existing tests are unchanged. `request_disruption` adds the current priority list to its merged scenario.
- [ ] **Step 4: Implement the frontend.**
  - A "Priority" chip: add `/priority/i` to the `Chip` tone regex as `warn`.
  - A "Mark as Priority" / "Remove Priority" button with a reason field on `OrderPage`.
  - The chip in the Orders list and the plan tables.
  - `RunPage` "Against the current plan" names priority orders first.
- [ ] **Step 5: Run the backend suite, then the frontend tests.** Expected: all pass.
- [ ] **Step 6: Commit** `priority orders: mark an order and CRAFT plans it first`, with the Task 3 proof numbers in the body.

### Task 7: Auto-planning watcher

**Files:**
- Modify: `web_v1/backend/planning/models.py` (`AutoPlanSetting`, `AutoTrigger`)
- Create: `web_v1/backend/planning/migrations/0006_autoplan.py` (also creates the `craft_auto` user with an unusable password, in the planner group needed for `request_plan`)
- Create: `web_v1/backend/planning/autoplan.py`
- Modify: `web_v1/backend/planning/management/commands/planning_worker.py` (call `autoplan.tick` every 60 s)
- Modify: `floor/services.py` (`report_issue`), `planning/services.py` (`set_priority`, `request_promise` with `priority=True`) to record triggers
- Modify: `planning/views.py`, `planning/urls.py` (setting endpoint)
- Test: `web_v1/backend/planning/test_autoplan.py`

**Interfaces:**
- Consumes: `request_disruption` (Task 4), `disruption_inputs` (Task 5), `priority_orders` (Task 6), `floor.plan.approved_plan(dataset_id, factory_id)`.
- Produces:
  - `AutoPlanSetting(dataset FK, factory_id, enabled bool default False, set_by FK, set_at)`, unique `(dataset, factory_id)`.
  - `AutoTrigger(dataset FK, factory_id, kind CharField(20), ref CharField(40), created_at auto_now_add, consumed_at null, run FK PlanRun null)`. `kind` is one of `ISSUE`, `PRIORITY`, `URGENT_ORDER` or `NEW_ORDERS`.
- Produces: `autoplan.record(dataset_id, factory_id, kind, ref) -> AutoTrigger | None`, which returns None when auto-planning is off for that factory.
- Produces: `autoplan.tick(now=None) -> list[PlanRun]`, the runs it queued.
- Produces: `GET`/`POST /api/v1/planning/autoplan?dataset=&factory=` with body `{enabled}`, which needs `request_plan`.

**`tick` behaviour (the spec pins these; the code shape is free):** for each factory with `enabled=True` and an approved plan, the triggers are its unconsumed `AutoTrigger`s.
1. Netting:
   - An `ISSUE` whose issue is RESOLVED is dropped.
   - A `PRIORITY` whose current state equals the state at the approved plan's `decided_at` is dropped. In other words, a priority that was switched on and then off again counts as no change.
   - If nothing remains, mark them consumed with `run=None` and continue.
2. Debounce: queue only if `now - newest.created_at >= AUTO_PLAN_QUIET_MINUTES`, or if `now - oldest.created_at >= AUTO_PLAN_MAX_WAIT_MINUTES`.
3. Guards:
   - Skip if any `PlanRun` for that dataset and factory is QUEUED or RUNNING.
   - Skip if a CAPTURE run is QUEUED or RUNNING.
4. Supersede: a PENDING automatic run for that factory (`trigger.source == 'AUTO'`) gets `decision='SUPERSEDED'` and a `PlanEvent` `PLAN_SUPERSEDED`. Its triggers join the new run.
5. Queue: `request_disruption(craft_auto, parent_id=approved.pk, mode='QUICK', trigger={...}, **disruption_inputs(...))`. Mark the triggers consumed with `run`, and set `Issue.replan`.

- [ ] **Step 1: Write the failing tests** in `test_autoplan.py`, using `now=` injection and no real CRAFT:
  - `test_off_by_default_records_nothing`.
  - `test_burst_of_triggers_makes_one_run`: 5 issues at t, t+1…t+4 min; `tick(t+10)` gives `[]`; `tick(t+20)` gives 1 run, whose `scenario['machines_down']` has all 5.
  - `test_max_wait_caps_a_trickle`: one trigger every 10 min from t; `tick(t+61)` queues.
  - `test_resolved_issue_inside_window_makes_no_run`.
  - `test_priority_on_then_off_nets_to_nothing`.
  - `test_skips_while_any_run_is_active`.
  - `test_pending_auto_run_is_superseded`: the old run has `decision == 'SUPERSEDED'`, the new run's `trigger['issue_ids']` contains both, and only one PENDING auto run exists.
  - `test_rejected_auto_run_is_not_retried`: after `decide(REJECTED)`, `tick` gives `[]`.
  - `test_material_used_by_no_planned_order_makes_no_trigger`: with `orders_using_material` mocked to return `[]`, `AutoTrigger.objects.count() == 0`.
  - `test_no_approved_plan_means_no_run`.
- [ ] **Step 2: Run** `manage.py test ... planning.test_autoplan`. Expected: FAIL.
- [ ] **Step 3: Implement** the models, migration, `record`, `tick`, and the hooks.
  - Worker loop: `if time.monotonic() - last_tick >= 60: autoplan.tick()`, wrapped in `try/except Exception` that logs and continues. A watcher bug must never stop the worker.
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `auto-planning: CRAFT re-plans after disruptions, one run per burst, manager approves`.

### Task 8: Email the managers

**Files:**
- Create: `web_v1/backend/planning/notify.py`
- Create: `web_v1/backend/planning/templates/planning/plan_ready.txt`, `plan_ready.html`, `plan_failed.txt`
- Modify: `web_v1/backend/config/settings_integration.py` (email settings from env/`.env`)
- Modify: `planning_worker.py` (after `finish`, call `notify.run_finished(run)` when `run.trigger.get('source') == 'AUTO'`)
- Test: `web_v1/backend/planning/test_notify.py`

**Interfaces:**
- Produces: `notify.recipients(run) -> list[str]`, the emails of active users with `request_plan` whose floor `Profile.factory_id` is `run.factory_id` or is null, and who have a non-empty email.
- Produces: `notify.run_finished(run) -> str`. It returns `'SENT'`, `'NO_RECIPIENTS'` or `'FAILED'` and writes a `PlanEvent` of `EMAIL_SENT` (message lists the count, not the addresses), `EMAIL_NO_RECIPIENTS` ("No manager has an email address, so nobody was told") or `EMAIL_FAILED`. It never raises.
- Settings:
  - `EMAIL_BACKEND` is `'django.core.mail.backends.smtp.EmailBackend'` when `CRAFT_EMAIL_HOST` is set. Otherwise it is `'django.core.mail.backends.filebased.EmailBackend'`, with `EMAIL_FILE_PATH = PLANNING_RUNS_DIR/'outbox'`.
  - `EMAIL_HOST`, `EMAIL_PORT` (default 587), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `EMAIL_USE_TLS` (default True) map from the `CRAFT_EMAIL_*` keys.
  - `DEFAULT_FROM_EMAIL = CRAFT_EMAIL_FROM`.
  - `APP_BASE_URL` defaults to `'http://localhost:5173'`.
- Copy pinned by the spec:
  - Subject: `CRAFT has a new plan for Factory {n}: {reason}`. On failure: `CRAFT could not make the plan for Factory {n}`.
  - Body lines: the reason; "Late orders: {before} → {after}"; orders finishing later/earlier; last order finish date; "Planned from the factory data of {date}, as if {it} stays down for the whole plan."; requested and finished times; the link `{APP_BASE_URL}/runs/{id}`. There is no approve link.

- [ ] **Step 1: Write the failing tests** with `@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')`:
  - `test_ready_email_goes_to_managers_of_that_factory`: `len(mail.outbox) == 1`, recipients are exactly the two scoped managers, the subject starts with `'CRAFT has a new plan for Factory 6'`, the body contains `/runs/<id>` and does not contain `'approve?'`.
  - `test_failed_run_sends_failure_email`.
  - `test_no_recipients_records_event`.
  - `test_send_error_records_event_and_does_not_raise`: patch `send_mail` to raise `SMTPException`, assert the result is `'FAILED'` and the event exists.
  - `test_manual_runs_send_nothing`: the worker path with `trigger.source == 'MANUAL'` gives `mail.outbox == []`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** using the before/after numbers from `jobs.summarize` of the run and of its parent.
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Commit** `auto-planning: email managers when CRAFT's plan is ready or fails`.
- [ ] **Step 6: Hand-off.** Tell the user the Gmail app-password steps (Google Account → Security → 2-Step Verification on → App passwords → create "CRAFT") and the five `.env` keys. Never ask for or handle the password.

### Task 9: Screens for auto-planning

**Files:**
- Modify: `web_v1/frontend/src/lib/api.ts` (`autoplan`, `setAutoplan`; `Run.trigger`, `decision` union gets `'SUPERSEDED'`)
- Modify: `src/pages/Runs.tsx` (Auto-planning switch + email status line), `src/pages/RunPage.tsx` (Started-by-CRAFT banner, superseded state, email events), `src/pages/Dashboard.tsx` (review card), `src/pages/floor/Issues.tsx` (Line stopped; material picker from `erp.materials` + arrival date; warning text)
- Modify: `web_v1/backend/planning/views.py` (`_run` exposes `trigger`; status endpoint `email: 'SMTP'|'OUTBOX'`)
- Test: `web_v1/frontend/src/app.test.tsx`, `web_v1/frontend/e2e/client.spec.ts`

**Interfaces:**
- Consumes: the endpoints from Tasks 6–8.
- Copy:
  - Switch label: "Auto-planning", with the help line "CRAFT makes a new plan after problems on the floor and emails the managers. A manager approves every plan."
  - Banner: "Started by CRAFT: {reason}".
  - Superseded: "Replaced by a newer CRAFT plan".
  - Home card: "CRAFT has a new plan ready for you to review".
  - Outbox line: "Email is not set up; messages are saved to the outbox".
  - Users page: a manager with no email address gets the hint "No email address: this person won't get CRAFT's plan emails" (`src/pages/Users.tsx`).

- [ ] **Step 1: Write the failing vitest** `renders the Started by CRAFT banner for an automatic run` in `app.test.tsx`. Mock a run with `trigger: {source: 'AUTO', reason: 'Machine 12 down'}` and assert the text `Started by CRAFT: Machine 12 down`.
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement the screens** in the existing dark-teal components (`PageHead`, `.panel.hero`, `Chip`). No new libraries.
- [ ] **Step 4: Run** `npx tsc -b && npx vitest run`. Expected: all pass. Then run e2e (`CRAFT_E2E_USER=demo_manager CRAFT_E2E_PASSWORD="$(cat <e2e_password.txt>)" npx playwright test --reporter=line`). Expected: all pass, with one new test that toggles Auto-planning on and off on the Plans page.
- [ ] **Step 5: Commit** `auto-planning screens: switch, Started-by-CRAFT banner, review card, new problem types`.

### Task 10: Write-back to the demo copies

**Files:**
- Create: `web_v1/backend/erp/writer.py`
- Modify: `web_v1/backend/planning/models.py` (`WriteBack`); migration `0007_writeback.py`
- Modify: `planning/services.py` (`decide` calls `writer.write_plan` after APPROVED, outside the decision transaction)
- Modify: `floor/services.py` (`report_issue` / `move_issue` call `writer.set_machine_status`)
- Create: `web_v1/backend/planning/management/commands/reset_demo_data.py`
- Modify: `RunPage.tsx` (write-back line), `Data.tsx` (admin "Reset demo data" button showing the command to run)
- Test: `web_v1/backend/erp/test_writer.py`

**Interfaces:**
- Produces: `writer.enabled() -> bool`, true when `settings.CRAFT_WRITEBACK` is on and the writer user is configured.
- Produces: `writer.write_plan(run) -> WriteBack`. In one transaction on `live_database(run.dataset)` it updates `production_orders.scheduled_start_date` / `scheduled_end_date`, and the planned rows in `job_shop_schedules` (line, machine, planned start and end), from the run's `digest.json`. Before writing, it checks with `SHOW GRANTS` that the account has no privileges on any database name without `CRAFT_DEMO_SUFFIX`, and refuses otherwise. It never raises; failure is recorded.
- Produces: `writer.set_machine_status(dataset, asset_id: int, status: 'Breakdown'|'Active') -> bool`, which updates `machinery_assets.current_status`.
- Produces: `WriteBack(run FK null, database, kind 'PLAN'|'MACHINE', rows int, ok bool, error TextField, created_at)`.
- Produces: `manage.py reset_demo_data --load 30|60|90`. It prints, and with `--run` executes, `mysqldump <original> | mysql <original>_demo` using the reader for the dump and the writer for the load, after a typed confirmation. It writes a `PlanEvent` `DEMO_RESET`.
- The implementer confirms the exact columns of `production_orders`, `job_shop_schedules` and `machinery_assets` on the `_demo` copy (`information_schema.COLUMNS`) before writing SQL, and records them in a comment at the top of `writer.py`.

- [ ] **Step 1: Write the failing tests** with a fake connection object that records SQL:
  - `test_disabled_by_default_writes_nothing`.
  - `test_refuses_when_account_can_touch_an_original`: grants include `garment_fyp_db_seed_v2_data2_load30_20261018_083214.*`, so the result is `ok=False` and no UPDATE runs.
  - `test_write_plan_updates_orders_and_lots_in_one_transaction`: asserts `START TRANSACTION`, then the UPDATEs, then `COMMIT`, with the expected row count.
  - `test_writeback_failure_keeps_approval_and_records_event`: the connection raises, `run.decision == 'APPROVED'`, `WriteBack.ok is False`, and a `PlanEvent` `WRITEBACK_FAILED` exists.
  - `test_machine_report_and_resolve_flip_status`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The writer connection reuses `LegacyExecutionRepository.from_config`, with `LEGACY_DB_USER` and `LEGACY_DB_PASSWORD` replaced by the `CRAFT_WRITER_*` values and `LEGACY_DB_NAME=live_database(dataset)`. Change the approval UI text to "the demo factory data is updated when you approve" when `writer.enabled()`.
- [ ] **Step 4: Run the backend suite.** Expected: all pass.
- [ ] **Step 5: Live check** (only after the user ran `demo_copies.md`). Approve a Factory 6 plan, then confirm with a read-only SELECT on the `_demo` copy that the 3 orders' `scheduled_end_date` match the plan. Confirm the original database's rows are unchanged (compare with a SELECT before and after).
- [ ] **Step 6: Commit** `write-back: approved plans and machine status go to the demo copies only`.

### Task 11: New orders and nightly capture

**Files:**
- Modify: `web_v1/backend/planning/autoplan.py` (`check_new_orders(now)`, `nightly_capture(now)`; both called from `tick`)
- Modify: `planning/services.py` (`request_capture(user, database=None)` passes `LEGACY_DB_NAME`)
- Modify: `planning_worker.py` / `planning/jobs.py` (the capture child gets `LEGACY_DB_NAME` in its env; the capture script already merges `os.environ`)
- Modify: `web_v1/audits/factory_source_integration_v1/capture_six_factories.py`: accept the `_demo` reader identity check unchanged (same `craft_reader` account; no code change expected; verify)
- Test: `web_v1/backend/planning/test_autoplan.py`

**Interfaces:**
- Produces: `autoplan.check_new_orders(now) -> int`. Every `AUTO_PLAN_NEW_ORDER_CHECK_MINUTES` per enabled factory, it runs one read-only query on `live_database(dataset)`: the `production_orders` ids of that factory with status Pending/Processing that are not in the latest capture's `factory{n}_source.json` orders. If any are found and no capture is active, it calls `request_capture(craft_auto, database=...)` and records `AutoTrigger(kind='NEW_ORDERS', ref=','.join(ids))`. It returns the count.
- Produces: `request_schedule(user, *, dataset_id, factory_id, orders, order_offset, mode, order_ids=None, trigger=None)`. When `order_ids` is given, it plans exactly those orders (via the runner's existing `--order-ids`) and ignores `orders`/`order_offset`. The existing tests pass unchanged.
- Produces: `autoplan.nightly_capture(now)`, which once per day after `AUTO_CAPTURE_HOUR` requests a capture per enabled dataset.
- When a capture triggered by `NEW_ORDERS` finishes, the new dataset is registered via `datasets.discover()`. The next `tick` builds the run on the **new** dataset, with the approved plan's orders plus the new ids, by calling `request_schedule` on the new dataset with `order_ids` (the runner's `--order-ids` already exists).

- [ ] **Step 1: Write the failing tests** with `read_only` mocked:
  - `test_new_orders_request_one_capture_and_record_trigger`.
  - `test_no_new_orders_no_capture`.
  - `test_check_runs_at_most_every_15_minutes`.
  - `test_nightly_capture_once_per_day`.
  - `test_never_captures_while_a_plan_runs`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the backend suite, the frontend tests and e2e.** Expected: all pass.
- [ ] **Step 5: Commit** `auto-planning: pick up new orders and refresh the data nightly`.

### Task 12: End-to-end demo and docs

**Files:**
- Modify: `web_v1/README.md` (Auto-planning, Priority, Email, Demo copies, Reset sections)
- Modify: `web_v1/backend/.env.example` (the `CRAFT_EMAIL_*`, `CRAFT_WRITER_*`, `CRAFT_USE_DEMO_COPIES`, `CRAFT_WRITEBACK`, `APP_BASE_URL` keys with empty values)

- [ ] **Step 1: Live demo** (one heavy run at a time; Factory 6; outbox email unless the user set SMTP):
  1. Turn Auto-planning on.
  2. Report machine down.
  3. Wait 15 min.
  4. The run is queued by `craft_auto` and finishes.
  5. The email appears in the outbox or inbox.
  6. Approve the plan.
  7. The write-back row is OK.
  8. The Line board shows the plan.

  Record the timings.
- [ ] **Step 2: Run all three test suites.** Expected: all pass.
- [ ] **Step 3: Commit** `docs: auto-planning, priority, email and demo write-back`.
