# Auto-planning, manager email and priority orders: design

Date: 2026-11-16. Owner: Muhammad Tuaha. App: `web_v1` (Django + React), scheduler: CRAFT V3.1.

## 1. Why

Today CRAFT only re-plans when a manager presses a button. On a real floor, disruptions
arrive all day (a machine breaks, a worker is absent, cloth arrives late, a client
escalates an order) and the plan goes stale until someone notices. The goal is that
CRAFT notices for them: it prepares a new plan by itself, tells the manager by email,
and the manager decides. A manager can also tell CRAFT "this order goes first".

Success looks like: a manager reports a machine down on the Problems board, walks away,
and some minutes later gets an email "CRAFT has a new plan for Factory 2: machine 12 is
down. 2 orders finish later, none become late. Review the plan." They open the link,
see the plan and its trade-offs, and approve or send it back.

## 2. Goals

1. **Auto-planning** for five disruption types (user chose all five):
   - A. machine down, B. worker absent, C. whole line stopped, D. material arrives late,
     E. rush order (a new urgent order, or an existing order marked Priority).
2. **A manager approves every plan** (user chose this). CRAFT never approves by itself.
3. **Real email** to the factory's managers when an automatic plan is ready (user chose
   real email, not a file).
4. **Priority orders**, simply on or off (user chose on/off, not levels): a manager marks
   an order Priority and CRAFT plans it first.

## 3. Non-goals

- No automatic approval, ever. The **original** demo databases (`garment_fyp_db` and the
  three `..._load30/60/90_...` databases) are never written: they are the paper's data.
  Write-back goes only to `_demo` copies (section 6.6).
- No change to CRAFT's scheduling algorithm (V3.1 is final and is what the paper reports).
  Every new behaviour is expressed by changing CRAFT's *input* (the what-if copy of a
  capture), never its code.
- No priority levels (High/Medium/Normal). No approve-by-email link.
- No customer portal here. User decision (2026-11-16): it is the **next** project, after
  this one, built on the downloaded `Needle_Craft_Client_Agent` (a customer-facing chat
  assistant) connected to the app's real orders and planned finish dates.
- No SMS / WhatsApp / push notifications.

## 4. Anti-goals (would be a failure even if it "works")

- An email or plan storm: one bad hour on the floor must not produce ten runs and ten
  emails, or freeze the laptop/server with parallel CRAFT runs.
- A plan the manager cannot explain: every automatic plan must say what triggered it.
- Research wording in the client UI (no FIFO/EDD/SPT/CP-SAT/V3/paper words).
- Secrets in git: the email password lives only in `web_v1/backend/.env` (gitignored),
  like `GROQ_API_KEY`.
- Silent failure: if a run fails or email can't be sent, the app shows it.

## 5. What exists today (verified in code, 2026-11-16)

- `floor.Issue` kinds: `MACHINE_DOWN`, `WORKER_ABSENT`, `MATERIAL_SHORT`, `QUALITY`, `OTHER`;
  statuses `OPEN`, `IN_PROGRESS`, `RESOLVED`; `Issue.replan` links to the re-plan run.
- `floor.services.replan_after(...)` → `planning.services.request_disruption(parent_id,
  machines_down, workers_absent, mode)`: a manual re-plan of the approved plan's orders.
  Only `MACHINE_DOWN` and `WORKER_ABSENT` are accepted today.
- `planning.services.request_promise(...)`: a what-if with one new order (order promise).
- `integrations/craft/plan_scenario.py` builds a **what-if copy** of a pinned capture
  (the base is never changed) and re-hashes it so CRAFT verifies it like a real capture.
  Supported today: `machines_down` (status → Breakdown), `workers_absent` (on leave),
  `new_order`. Disruptions last the **whole plan**: the capture model has no
  time-limited outages.
- Capture contents: `orders` (with `delivery_deadline`, `scheduled_start_date`), `lots`,
  `operations`, `resources.machines` (with `line_id`), workers, schedules. **No
  materials / bill of materials** in a capture; materials per order come from the ERP
  read-only queries (`erp` app; the order page already lists them).
- CRAFT's runner reads each order's release from `scheduled_start_date` and its due date
  from `delivery_deadline` (`scratch/run_craft_v2.py:67,95-96`), so both levers in 6.1 and
  6.2 reach CRAFT through the capture copy without touching CRAFT code.
- `PlanRun` has `purpose` (PLAN / DISRUPTION / PROMISE), `parent`, `scenario`,
  `requested_by` (FK, PROTECT), `decision` (PENDING / APPROVED / REJECTED), and
  `PlanEvent` is the audit log.
- `planning_worker` management command runs queued runs one at a time and writes
  `planning_runs/worker.alive` every loop (`services.worker_beat()`).
- Quick mode on the smallest factory (Factory 6, 3 orders) took 11 minutes end to end on
  the laptop (2026-11-16). Larger factories take much longer (UI estimate: ~1.5 h for 10
  orders on Factory 1).

## 6. Design

### 6.1 Disruption → scenario mapping (all through `plan_scenario.py`)

| Disruption | How the manager reports it | What changes in the what-if copy |
|---|---|---|
| A. Machine down | Problems board, `MACHINE_DOWN` (exists) | machine status → Breakdown (exists) |
| B. Worker absent | Problems board, `WORKER_ABSENT` (exists) | worker on leave (exists) |
| C. Line stopped | Problems board, new kind `LINE_STOPPED` (line required) | every machine with that `line_id` → Breakdown |
| D. Material late | Problems board, existing `MATERIAL_SHORT`, plus two new fields: material and expected arrival date | every order in the plan that needs that material (ERP read-only lookup) gets `scheduled_start_date` = max(current, arrival date) |
| E. Rush order | (a) mark an existing order Priority; (b) a new urgent order via the existing order-promise form with a "Priority" tick | (a) see 6.2; (b) `new_order` (exists) plus priority |

New scenario keys: `lines_stopped: [line_id]`, `release_after: {order_id: date}`,
`priority_orders: [order_id]`. All validated like the existing ones (unknown id →
`SystemExit` with a clear code), recorded in `scenario.json` and the readiness file.

Limitation kept as-is: every disruption lasts the whole plan horizon (capture model).
The email and plan page say so in plain words ("planned as if machine 12 stays down").
When the problem is resolved, the manager can re-plan (manual) to bring it back.

### 6.2 Priority orders (on/off)

- Stored per (dataset, factory, order_id) in a new model `planning.OrderPriority`
  (`set_by`, `set_at`, `reason`, `active`). Changes are written to `PlanEvent`.
- Who: users with the existing `request_plan` permission (production manager).
- Effect: when a plan or re-plan is built, priority orders are passed as
  `priority_orders`; the what-if copy sets their `delivery_deadline` to the plan's
  reference date (the earliest possible due date), so CRAFT's due-date driven
  construction and improvement put them first. **Mechanism must be proven before any UI
  is built** (step 1 of the build plan): run Factory 6 with and without one order marked
  Priority and confirm that order finishes earlier and is not late where it can avoid it.
  If tightening the due date is not enough (e.g. an order already past due), the builder
  reports back instead of touching CRAFT's code.
- The plan page shows the cost: "Order 1043 (Priority) finishes 4 days earlier; orders
  1051 and 1060 finish 1 day later", from comparing with the current approved plan
  (the existing "Against the current plan" section).
- A Priority badge appears on the order page, orders list and plan tables.

### 6.3 Auto-planning watcher

- Runs **inside `planning_worker`**, checked every 60 s between runs (no new process).
- **Off by default.** Per (dataset, factory) setting `AutoPlanSetting(enabled, set_by,
  set_at)`; switch on the Plans page, only for `request_plan` users.
- Triggers for a factory with auto-planning on **and an approved plan**:
  1. An `OPEN` issue of kind A–D (and D has an arrival date) that has no `replan` yet.
  2. A Priority switched on or off since the approved plan was made.
  3. A new urgent order submitted with the Priority tick.
- **Debounce:** the watcher waits until 15 minutes have passed since the *newest* trigger
  for that factory (so a burst of reports becomes one re-plan), capped at 60 minutes
  from the oldest trigger so a steady trickle can't postpone it forever. Both numbers in
  settings (`AUTO_PLAN_QUIET_MINUTES=15`, `AUTO_PLAN_MAX_WAIT_MINUTES=60`).
- **Guards:** at most one automatic run queued or running per factory; skip (and keep the
  triggers pending) if any run for that factory is queued or running, manual or
  automatic; mode is always Quick; the run's orders are the approved plan's orders (as
  `replan_after` does today) plus a new urgent order if any.
- The run is a normal `PlanRun` with `purpose='DISRUPTION'`, `parent` = approved plan,
  `requested_by` = a system user `craft_auto` (unusable password, cannot sign in,
  created by a migration), and a new field `trigger` (JSON: issue ids, kinds, priority
  changes, plain-language reason). The issues get `replan` set to it, as today.
- **New orders in the factory database** (user decision 2026-11-16: include it, run on the
  server). Every 15 minutes the watcher runs one light read-only query on the factory's
  `_demo` database: orders whose id is not in the latest capture. If any are found, it takes a
  fresh capture (registered as a new `Dataset`, `origin='LIVE_CAPTURE'`). Then it queues an
  automatic plan of the approved plan's orders plus the new ones, with trigger kind
  `NEW_ORDERS`. A nightly capture (`AUTO_CAPTURE_HOUR`, default 02:00) gives each morning's
  plan today's data. Both obey the same guards and debounce, and both are off unless
  auto-planning is on for that factory. On a laptop the capture is heavy, so the builder
  keeps it to one capture at a time and never runs it while a CRAFT run is running.

### 6.4 Email to managers

- Sent when an automatic run **finishes** (`SUCCEEDED` → "plan ready"; `FAILED` →
  "CRAFT could not make the plan", so failures are never silent). Manual runs send no
  email (the manager is already looking).
- To: active users with role production manager scoped to that factory (floor
  `Profile.factory_id`, or all factories if unscoped) **who have an email address**.
  Users without an email simply get none; the Users page shows a hint.
- Content (plain text + simple HTML, no attachments, no factory data beyond the summary):
  subject `CRAFT has a new plan for Factory 2: machine 12 is down`; body: trigger reason,
  late orders before → after, orders finishing later/earlier, last-order finish date,
  "planned as if it stays down for the whole plan", a single "Review the plan" link
  `APP_BASE_URL/runs/<id>`. Approval happens only in the app after sign-in.
- One email per automatic run (debounce already merges triggers).
- Transport: Django SMTP backend. Settings read from env or `web_v1/backend/.env`:
  `CRAFT_EMAIL_HOST`, `CRAFT_EMAIL_PORT`, `CRAFT_EMAIL_USER`, `CRAFT_EMAIL_PASSWORD`,
  `CRAFT_EMAIL_FROM`, `CRAFT_EMAIL_TLS`, `APP_BASE_URL`. If not configured, the app uses
  Django's file backend into `planning_runs/outbox/` and the Plans page says "Email is
  not set up; messages are saved to the outbox".
- Sending happens in the worker after the run finishes; a send failure is logged as a
  `PlanEvent` (`EMAIL_FAILED`) and shown on the run page, and never fails the run.
- The user (not the builder) creates the mail account / app password and pastes it into
  `.env`. The builder never handles real credentials; tests use Django's locmem backend.

### 6.5 What the manager sees (client UI wording, CRAFT only)

- Plans page: "Auto-planning" switch per factory with one line of explanation; email
  status line.
- Run page: a "Started by CRAFT" banner with the reason ("Machine 12 down, worker 44
  absent"), the whole-plan limitation sentence, the existing decision box.
- Home: card "CRAFT has a new plan ready for you to review" when an automatic run waits
  for a decision.
- Problems board: new kind "Line stopped"; "Material short" asks for material and expected
  arrival; cards link to the re-plan they triggered (exists).
- Orders list / order page / plan tables: Priority badge and a "Mark as Priority" toggle
  (with optional reason).

### 6.6 Data sources and write-back to demo copies (user decisions 2026-11-16)

- **Only three data sources in the app: 30%, 60% and 90% load.** "Factory database"
  (`garment_fyp_db`, the older v1 data) is **hidden**, not deleted: add `Dataset.active`
  (default true, false for it) and filter lists by it. Older runs that reference it keep
  working, because `Dataset` is a PROTECT foreign key.
- **Demo copies.** For each of the three load databases there is a full MySQL copy named
  `<original>_demo`. The app's live pages, captures and write-back use the copies. The
  originals stay read-only for every account.
- **Two MySQL accounts.** The existing read-only reader, plus a new writer account with
  all rights on the three `_demo` databases only (needed for the reset) and no rights on
  any original. The user creates the account and its
  password and puts them in `web_v1/backend/.env`. The builder supplies the SQL
  (`CREATE DATABASE ... ; mysqldump | mysql` for the copies, `CREATE USER` / `GRANT` for
  the writer) as a document for the user to run, and never handles real credentials.
- **What is written, and when:**
  1. When a plan is **approved**, its schedule is written into the copy: the order rows'
     `scheduled_start_date` / `scheduled_end_date`, and the planned lot rows (line, machine,
     planned start and end), in one transaction. A `WriteBack` record (run, database, rows,
     time, result) is kept, and failure is shown on the run page.
  2. When a machine problem is reported or resolved, the copy's machine `current_status` is
     set to `Breakdown` or back to `Active`.
  3. Nothing else is written. The builder confirms the exact table and column names
     against the copy's schema before writing code.
- **Switch.** Write-back is on when the writer account is configured (setting
  `CRAFT_WRITEBACK=1`, the existing "production activation" made real for demo copies
  only). The UI wording changes from "the factory database never changes" to "the demo
  factory data is updated when you approve".
- **Reset demo data.** An admin-only action rebuilds a copy from its original
  (drop and copy again). It asks for confirmation, is logged as a `PlanEvent`, and is
  never run automatically.

### 6.7 Deployment (user decision: not decided yet, so design for both)

The design must work both **on a computer inside the factory** and **on a cloud server**
with a secure link to the factory database. That means:
- every address, account and path comes from settings or `.env`, never from code;
- the database connection uses TLS when the settings ask for it;
- `APP_BASE_URL` drives email links;
- the planning worker (with the watcher) and the web server are two processes started
  by the same service manager.

No hosting choice is made now.

## 7. Constraints

- CRAFT code (`crafter/`, `scratch/run_craft_v2.py`, V3 settings) is not modified.
- The original databases stay SELECT-only. Writes go only to the `_demo` copies, only
  through the writer account, and only for the two cases in 6.6.
- One CRAFT run at a time on this laptop (heavy). Quick mode only for automatic runs.
- Opt-in everywhere: with auto-planning off and no Priority set, the app behaves exactly
  as today and all existing tests pass unchanged.
- Tests: backend web suite (`planning assistant erp floor api governance.tests
  governance.test_security_headers`), frontend vitest, e2e. Never run the full governance
  suite casually (it starts real CRAFT runs).
- Commits path-scoped; nothing secret committed; author "Muhammad Tuaha".

## 8. Calls I made (the user did not decide these; flag if wrong)

- Debounce 15 min / cap 60 min.
- Disruptions last the whole plan (existing limitation, not widened).
- New-order check every 15 minutes and nightly capture at 02:00 (the user chose to
  include new orders; the timings are my call).
- Priority implemented as "due date = plan start" in the what-if copy, pending the proof
  run in build step 1.
- Failed automatic runs also email; manual runs never email.
- System user `craft_auto` as the requester of automatic runs.
- If email isn't configured, messages go to an outbox folder instead of failing.

### 8.1 Answers to the builder check (my calls)

1. **Baseline.** The parent of every automatic run is the *approved* plan, never a pending
   one. If an automatic run is still PENDING when new triggers fire, the next automatic
   run includes the old run's triggers plus the new ones, and the old pending run is marked
   `decision='SUPERSEDED'` (shown as "Replaced by a newer CRAFT plan"), so a manager only
   ever reviews one automatic plan per factory. If a manager **rejects** an automatic plan,
   its triggers count as handled. The issues stay linked to it, and CRAFT does not retry.
   The manager can still re-plan by hand.
2. **Trigger storage.** New table `AutoTrigger(dataset, factory, kind, ref, created_at,
   consumed_by_run)`, written when an issue of kind A–D is reported, when a Priority is
   toggled, or when an urgent order is submitted. Before queuing, the watcher re-reads
   every trigger:
   - An issue that is RESOLVED is dropped.
   - A priority toggled on and then off nets to nothing.
   - If nothing is left, no run is queued and the triggers are marked consumed with no run.

   `Issue.replan` is still set as today, for the link on the Problems board.
3. **Gating.** Only the Priority UI (step 4) waits for the proof run in step 1. Everything
   else proceeds. Several Priority orders all get the plan-start due date (a tie is
   acceptable), and CRAFT keeps their relative order by its own rules.
4. **Material (D).** The material is picked from the ERP materials list, not typed as
   free text. The order page already reads it. Orders are matched with the same read-only
   per-order materials query. If the chosen material affects zero orders in the approved
   plan, the report is accepted as a problem but creates no trigger, and the form says
   "No order in the current plan uses this material".
5. **Waiting behind long runs.** This is accepted: one worker, one run at a time. The quiet
   and cap clocks run from trigger times. Once they pass, the run is queued and waits its
   turn like any run. The email states when the plan was requested and when it finished.
   The "some minutes later" in section 1 holds for small factories. For large ones, the
   app shows "Waiting its turn".
6. **Whole-plan outages and finished work.** This is existing behaviour, kept. A re-plan
   starts from the capture's reference time with the capture's recorded progress. The
   email and run page say "Planned from the factory data of <date>, as if <machine> stays
   down for the whole plan". The before/after comparison is against the approved plan
   built from the same capture, so it compares like with like.
7. **Recipients.** Recipients are active users who hold `request_plan` (the people who
   can approve) and are scoped to that factory or unscoped. If none of them has an email
   address, the run page shows "No manager has an email address, so nobody was told", and
   a `PlanEvent` records it.

## 9. Left to the builder

Model/field names, migration details, exact watcher code structure, email template
markup, and UI layout within the existing dark-teal design system.

## 10. Suggested build order

0. Hide "Factory database" (`Dataset.active`). Write the copy and writer-account SQL
   document for the user. Once the user has created them, point the app at the `_demo`
   copies.
1. Prove priority: scenario key `priority_orders` + Factory 6 with/without comparison.
2. Scenario keys `lines_stopped`, `release_after` (+ tests on the copy and its hashes).
3. Issue kind `LINE_STOPPED`, material fields on `MATERIAL_SHORT`, `replan_after`
   accepting A–D (manual path first, so it is testable without the watcher).
4. `OrderPriority` model, API, UI badge/toggle.
5. `AutoPlanSetting`, `craft_auto` user, `PlanRun.trigger`, watcher with debounce/guards.
6. Email (settings, outbox fallback, templates, send-after-finish, failure event).
7. UI: Plans switch, run banner, Home card, Problems board fields.
8. Write-back on approval, machine status write-back, reset action, `WriteBack` log.
9. New-order check and nightly capture in the watcher.
10. Tests (unit + one e2e with locmem email + write-back against a throwaway copy), then
    a live Factory 6 demo run.
