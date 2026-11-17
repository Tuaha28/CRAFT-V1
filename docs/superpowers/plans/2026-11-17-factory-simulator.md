# Factory Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A factory clock that plays the approved CRAFT plan forward (output, checks, breakdowns, absences) so the app
and portal behave like a running factory, plus a headless experiment command for the paper.

**Architecture:** New Django app `sim`: a pure engine `advance()` turns (plan, state, time window, rng) into events;
`runner.apply()` writes them through the existing floor records/services as the `craft_sim` user; the planning
worker calls `runner.tick()` every few seconds. Floor "today" and portal progress read the clock. React adds a
clock control and a "Simulated factory clock" badge. `manage.py simulate` runs the engine in memory for experiments.

**Tech Stack:** Django 5.2 + DRF, SQLite app DB, React 19 + TanStack Query, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-17-factory-simulator-design.md`

## Global Constraints

- Never write the original databases; the simulator writes app records only (demo-copy write-back stays the existing opt-in path).
- CRAFT code unchanged; re-plans are normal CRAFT runs; one heavy run at a time.
- Speeds: pause (0), 1× = 1 factory hour per real minute, 10×, 60× = 1 factory hour per real second.
- Performance factor lognormal mean 0.95 sd 0.10 per operation; defects 2–6% per daily check; breakdown 0.15% per
  machine per working hour, repair 2–8 h; absence 2% per planned worker per day; all from one seeded RNG.
- Simulated issues/records are entered by `craft_sim`; reset deletes only `craft_sim` records of that dataset+factory.
- Clock controls need `planning.approve_plan` or `floor.manage_users`; the clock is visible to every staff role.
- Client wording: no research words in the UI; badge text "Simulated factory clock".

## Review Focus

1. A re-plan is approved mid-simulation: the simulator follows the new plan, and output already recorded for an
   operation (any plan run) counts, so no operation exceeds its pieces. → Task 2 `test_follows_new_plan_without_double_output`.
2. The worker was stopped for an hour at 60×: one tick must not flood the database; a tick advances at most 24
   factory hours. → Task 2 `test_tick_caps_a_long_gap`.
3. No approved plan: the clock still moves and nothing is written. → Task 2 `test_no_plan_no_records`.
4. A breakdown is drawn for a machine that is already down: no second issue. → Task 1 `test_no_second_breakdown_for_a_down_machine`.
5. Reset removes only simulated records, never a supervisor's own entries. → Task 3 `test_reset_keeps_human_records`.

---

### Task 1: Engine (pure)

**Files:** Create `web_v1/backend/sim/__init__.py`, `apps.py`, `engine.py`; Test `web_v1/backend/sim/test_engine.py`

**Interfaces — Produces:**
- `HOUR = 3600`; `@dataclass State`: `done: dict[str, int]` (pieces per operation key, all runs), `perf: dict[str, float]`,
  `down: dict[int, int]` (machine id → plan-second when repaired), `absent: dict[int, int]` (worker id → plan day),
  `stopped: set[int]` (lines).
- `advance(plan, state, t0: int, t1: int, rng: random.Random, rates: Rates) -> list[Event]` where t0/t1 are plan
  seconds (multiples of HOUR) and `Event` is a dict with `type` in `OUTPUT` (`key, line, slot, pieces`), `CHECK`
  (`key, line, day, checked, defects`), `BREAKDOWN` (`machine, line, until`), `REPAIRED` (`machine`),
  `ABSENT` (`worker, line, day`), `BACK` (`worker`). `advance` mutates `state` (done, perf, down, absent).
- `@dataclass Rates`: `breakdown_per_hour=0.0015, repair_hours=(2, 8), absence_per_day=0.02, defect=(0.02, 0.06),
  perf_mean=0.95, perf_sd=0.10`.
- Algorithm per factory hour h in [t0, t1): repairs/returns due → events; for each operation with planned pieces
  in the hour (`plan.planned_between`) plus carried backlog (planned before hour end − done): availability 0 if any
  of its machines is down or its line stopped, ×(1 − absent share of its workers); output =
  `floor(min(remaining, (planned_hour + backlog) × perf × availability))`; first hour of each plan day: absences
  drawn per planned worker; each working hour per busy machine: breakdown draw (skip if down); last working hour of
  a day with output: one CHECK per operation (checked = min(day output, 20), defects ~ Binomial(checked, rate)).

- [ ] **Step 1: Write failing tests** (a fake plan object with two operations on line 86, one machine each):
  `test_output_follows_plan_with_perf` (perf forced 1.0, no disruptions → OUTPUT sums equal planned pieces);
  `test_same_seed_same_events`; `test_backlog_carries_forward` (perf 0.5 then 1.0 → later hours exceed plan);
  `test_machine_down_stops_its_operation` then `REPAIRED` after `until`; `test_no_second_breakdown_for_a_down_machine`;
  `test_never_more_than_operation_pieces`; `test_daily_check_has_defects_within_rate`.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe manage.py test --settings=config.settings_integration sim` → FAIL (no module).
- [ ] **Step 3: Implement `engine.py`.** Add `'sim'` to `INSTALLED_APPS` in `config/settings_integration.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `sim: engine that plays a plan forward with noise and disruptions`.

### Task 2: Clock model, runner and worker tick

**Files:** Create `sim/models.py` (+migration), `sim/runner.py`; Modify `planning/management/commands/planning_worker.py`;
Test `sim/test_runner.py`

**Interfaces:**
- Consumes: Task 1 `State, advance, Rates`; `floor.plan.approved_plan`, `floor.plan.load`; floor `OutputEntry`,
  `DefectCheck`, `Issue`; `floor.services.report_issue`, `floor.services.move_issue`.
- Produces: `FactoryClock(dataset FK, factory_id, now DateTimeField, speed PositiveSmallIntegerField (0/1/10/60),
  seed IntegerField default 1, state JSONField, last_tick DateTimeField null, set_by FK user)`, unique (dataset, factory_id).
  `runner.system_user()` → `craft_sim` (group "Simulator" with enter_output, record_defect, report_issue,
  handle_issue, repair_machine). `runner.tick(real_now=None) -> int` (factory hours simulated, all clocks);
  `runner.apply(clock, run, plan, events)`; `runner.factory_now(dataset_id, factory_id) -> datetime | None`.
- Behaviour: factory seconds to add = real seconds since `last_tick` × speed × 60 (1× = 60 factory s per real s),
  floored to whole hours, capped at 24 h per tick; the remainder stays (only whole hours advance `now`).
  `done` is rebuilt from `OutputEntry` sums per operation key at the start of each tick (so human and any-run
  output counts). Issues: BREAKDOWN → `report_issue(kind='MACHINE_DOWN')`; REPAIRED → `move_issue(..., 'RESOLVED')`;
  ABSENT → `WORKER_ABSENT`; BACK → resolve. RNG: `random.Random(f'{seed}:{dataset}:{factory}:{hour}')` per hour.
- Worker: `sim.runner.tick()` inside the existing guarded watcher pass every 5 s.

- [ ] **Step 1: Failing tests:** `test_tick_advances_by_speed` (speed 60, 30 real s → +30 factory h capped to 24);
  `test_paused_clock_does_not_move`; `test_tick_writes_output_and_checks_as_craft_sim`;
  `test_breakdown_opens_and_repair_closes_an_issue`; `test_follows_new_plan_without_double_output`;
  `test_tick_caps_a_long_gap`; `test_no_plan_no_records`.
- [ ] **Step 2: Run** `manage.py test ... sim` → FAIL.
- [ ] **Step 3: Implement models, migration, runner; hook the worker.**
- [ ] **Step 4: Run** `manage.py test ... sim floor planning` → PASS.
- [ ] **Step 5: Commit** `sim: factory clock, runner and worker tick`.

### Task 3: Clock API, factory "now" everywhere, portal progress

**Files:** Create `sim/views.py`, `sim/urls.py`; Modify `config/urls_app.py`, `floor/views.py` (`_default_day`),
`portal/orders.py` (progress adds `OutputEntry`), `planning/views.py` (session `sim` field); Test `sim/test_api.py`

**Interfaces:**
- `GET /api/v1/sim/clock?dataset=&factory=` → `{now, speed, running, seed, has_plan}` or `{now: null}`;
  `POST` same query `{action: 'start'|'speed'|'pause'|'reset', speed?: 1|10|60, seed?: int}` (perm check per Global
  Constraints; `reset` deletes `craft_sim` OutputEntry/DefectCheck/Issue of that dataset+factory and the clock).
- `floor.views._default_day(plan, days, request)` uses `runner.factory_now` when a clock exists.
- Portal `orders.fetch` progress = max(MySQL pieces through every step, app `OutputEntry` pieces through every step).

- [ ] **Step 1: Failing tests:** `test_manager_starts_and_sets_speed`, `test_floor_role_cannot_control`,
  `test_reset_keeps_human_records`, `test_line_board_today_follows_the_clock`, `test_portal_progress_counts_simulated_output`.
- [ ] **Step 2–4:** run → FAIL; implement; run `manage.py test ... sim floor portal planning` → PASS.
- [ ] **Step 5: Commit** `sim: clock API; floor today and portal progress follow the factory clock`.

### Task 4: Clock control and badge (frontend)

**Files:** Create `frontend/src/components/FactoryClock.tsx`; Modify `components/Shell.tsx` (topbar),
`portal/PortalApp.tsx` (badge only), `lib/api.ts`, `styles.css`; Test `src/app.test.tsx`

**Interfaces:** `sim.clock(d, f)`, `sim.control(d, f, body)`; `<FactoryClock />` shows date-time, "Simulated factory
clock" badge, and for allowed roles Pause / 1× / 10× / 60× / Reset (reset via `ConfirmButton`); polls every 5 s.

- [ ] **Step 1: Failing test** `shows the simulated clock badge and speed buttons for a manager`.
- [ ] **Step 2–4:** `npx vitest run` FAIL → implement → `npx tsc -b && npx vitest run` PASS.
- [ ] **Step 5: Commit** `sim: factory clock control and badge`.

### Task 5: Experiment command

**Files:** Create `sim/management/commands/simulate.py`, `sim/experiment.py`; Test `sim/test_experiment.py`

**Interfaces:** `experiment.run(plan, *, days, seed, rates) -> dict` (in memory, no DB writes): per order planned
finish day vs simulated finish day, on-time planned/simulated, lateness, adherence (share of planned pieces done
in their planned hour), breakdowns, absences. Command: `simulate --run <plan id> --days 30 --seeds 1-10
--breakdown 0,0.0015,0.003` → `deliverables/paper/results/simulation/<run>_<stamp>.json` + `summary.csv`.

- [ ] **Step 1: Failing tests** `test_no_disruption_perf_one_matches_plan`, `test_more_breakdowns_more_lateness` (mean over seeds),
  `test_same_seed_same_result`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `sim: experiment command for planned vs executed results`.

### Task 6: Docs, live demo, all suites

- [ ] README "Factory simulator" section; live demo on the e2e DB (start at 60×, watch output, a breakdown, auto-plan
  if on, portal progress), record timings; run backend, vitest and Playwright suites → all pass; commit
  `docs: factory simulator`.
