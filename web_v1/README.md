# CRAFT V3 planner and factory ERP (web app)

A web app for planning a garment sewing floor with CRAFT V3. A planner picks a
pinned factory capture and a set of orders, CRAFT V3 plans them (every plan
passes the same physical validation), and a production manager approves or
rejects the result. The shop floor (supervisors, quality, maintenance) then
works from the approved plan in the same app.

The interface is a client product: it talks only about CRAFT and the factory.
The research comparison (FIFO, EDD, SPT and the other baselines) lives in the
paper and `deliverables/`, not in the web app.

Production activation is disabled: approving records the accepted **test**
plan and never changes the factory floor or the factory database.

## Look

Dark teal design (November 2026), with a light theme on the toggle. Colours are
tokens at the top of `frontend/src/styles.css`; Inter for text, Space Grotesk
for headings and numbers. Each page has one glowing "hero" card that answers
its main question (is the plan on time, what is short, which machines are down),
then number tiles, then the detail table. Teal means on time or chosen, amber
waiting or maintenance, coral late or broken, and a word always goes with the
colour. The sidebar is grouped into Planning, Shop floor, Factory and Admin, with
an Ask CRAFT card at the bottom. The mock-ups the pages follow are in the
design canvas shared with the project owner.

## Pages

The capture (30/60/90% load, original or a new database capture) and factory
are chosen in the top bar; every page below follows that choice. Ctrl+K
searches orders, people, machines and styles. The bell gathers alerts: plans awaiting approval, finished or failed runs, overdue orders, machines down, material shortages and late orders in the current plan.
Orders are always named "Order <production order id>", with the buyer's PO number underneath.

| Page | What it shows |
|---|---|
| Home | The current plan's health (how many orders finish late, the worst one), past-due and due-soon orders, machines running, people present, what needs attention, order deliveries, each line over the plan, and orders delivered over time. Floor roles reach it as "Overview" |
| Ask CRAFT | Side panel for plain questions (late orders, one order, stock, machines, quality, who works on what). Answers are worked out in the browser from data the app already reads; an optional AI reader (Groq, `GROQ_API_KEY` in `backend/.env`) only routes the question, and the app falls back to the built-in reader without it |
| New plan / Plans / Plan page | Start CRAFT V3 (Quick or Full) on a set of orders; live progress; delivery tape, Gantt by worker/machine/line/order, late reasons, Excel plan, approve/reject. A queued plan says plainly when the planning service is not running |
| What if… (on a finished plan) | **Disruption re-plan:** mark machines out of service or workers absent and CRAFT re-plans the same orders without them. **Order promising:** pick a style, pieces, release and requested date, and CRAFT plans the new order with the plan's orders and reports the earliest delivery and the effect on the other orders. Both run on a scenario copy of the capture (`integrations/craft/plan_scenario.py`), re-hashed and verified by the runner; the original plan and data never change. Disruptions last the whole plan, because the capture model has no time-limited outages |
| Compare plans | Two finished plans side by side (quick vs full, another order set or capture) |
| Orders | Buyer POs owned by the factory: stage tiles, search, overdue filter, status and shipping; order page with routing progress, defects, materials needed and shipment |
| Styles | Styles with operation bulletin (steps, SAM, machine type), bill of materials, sizes and colours, and **line balancing**: takt time, operators per step (each extra operator goes to the slowest step), bottleneck, pieces per day and balance efficiency |
| Materials | A shortage card when open orders need more than is in stock, then stock against need (BOM × pieces + wastage), suppliers and warehouse stock, with readable quantities (1.55 M kg, 85,881 m) |
| Workforce | Crews with today's shift and rest day (weekly rotation), skills per operation, people with shift/leave |
| Machines | Floor map of every machine by line (maintenance and breakdowns stand out), availability by line and type, machine register |
| Quality | DHU overall, by operation, line and defect type, orders with the most defects |
| My line / Line board / Problems / Quality checks | Shop-floor pages: the supervisor's line today with hourly entry, a full-screen line board for the floor TV, problem reports, and quality checks |
| Users / Factory data / Activity | Admin: accounts and roles, captures and order book, and who requested, cancelled, approved or rejected what |

The ERP pages read the factory database through the SELECT-only `craft_reader`
account in a READ ONLY transaction (writes are refused by MySQL). The generated
quality and machine logs have no meaningful event dates, so those pages show
breakdowns by line, operation and type rather than trends over time.

Manual drag-and-drop of operations is deliberately not offered: a CRAFT
operation's timing follows the crews' shift calendar, and a moved operation
fails CRAFT's replay validation unless it is re-planned by CP-SAT (tested on
real plans). Use a new plan and Compare plans for what-if questions.

## Run modes

| Mode | Settings | Typical time (10 orders, laptop) |
|---|---|---|
| Full | The locked V3 settings of the paper: CP-SAT windows 10/20/30 s, objective 30 s, 1,800 s improvement per start, 8 search workers, 14-day dispatch window, horizon +90 days | 1.4–3.2 hours on a 4-core cloud machine; longer on a laptop |
| Quick | The same V3 code with the runner's validated laptop limits (3/5/8 s, objective 5 s) and 120 s improvement per start | about 1.5 hours (measured 1 h 34 min, 30% load, factory 4, orders 1–10) |

Quick results are not comparable with the paper's numbers.

## How it works

```
React (frontend/, Vite :5173) ──/api──▶ Django + DRF (backend/, :8001, session login)
                                          │  (CRAFT_API=http://127.0.0.1:8002 points Vite at another port)
                                          │  planning app: datasets, runs, decisions, audit events
                                          ▼
                                   planning_worker (one run at a time)
                                          │ subprocess, scheduler virtualenv (repo venv/)
                                          ▼
                   scratch/run_craft_v2.py --mode v3   (CRAFT V3, unchanged)
                   integrations/craft/plan_digest.py   (KPIs, orders, Gantt rows, Excel plan)
```

- The web app never imports the scheduler. Each run is the same command the
  paper experiments used, so the web app cannot change CRAFT V3.
- Captures are pinned folders under
  `web_v1/audits/factory_source_integration_v1/capture_*` with a SHA-256
  manifest; the runner verifies the hashes before planning.
- **Capture the database now** runs `capture_six_factories.py`, which requires
  the dedicated `craft_reader` account (SELECT-only grants are checked, the
  session is set read-only). Database settings come from the repository `.env`;
  they are never passed to scheduler runs.
- Run outputs are kept in `backend/planning_runs/<run id>/` (git-ignored).
- The worker touches `backend/planning_runs/worker.alive` on every loop. The
  runs API reports `worker_down` for a queued run when that file is older than a
  minute, and reading runs marks a RUNNING run whose worker went silent for 3
  minutes as failed ("Interrupted"), so nothing looks busy forever when the
  worker is stopped. Such runs are never retried automatically.

## Start it

Requirements: the repository scheduler virtualenv (`venv\`), the backend
virtualenv (`web_v1\backend\.venv`, `pip install -r requirements.txt`) and
Node.js 20+ (`npm install` in `web_v1\frontend`).

```powershell
# once: create a production manager (the password is written to the file, never printed)
$env:DJANGO_SETTINGS_MODULE='config.settings_integration'; $env:DJANGO_SECRET_KEY='any-local-value'
web_v1\backend\.venv\Scripts\python.exe web_v1\backend\manage.py migrate
web_v1\backend\.venv\Scripts\python.exe web_v1\backend\manage.py planning_setup --user manager1 --role manager --password-file web_v1\backend\.planner_users.local

# every time: API, worker and web page in three windows
powershell -ExecutionPolicy Bypass -File web_v1\start_planner.ps1
```

Then open http://127.0.0.1:5173. Roles: **Planner** (request and cancel
plans) and **Production manager** (also approve or reject). Use
`--role planner` for planners.

## Tests

```powershell
# backend: the web app's own tests (a few minutes; ERP tests skip if MySQL is not running)
$env:DJANGO_SETTINGS_MODULE='config.settings_integration'; $env:DJANGO_SECRET_KEY='test-only'
web_v1\backend\.venv\Scripts\python.exe web_v1\backend\manage.py test planning assistant erp floor api governance.tests governance.test_security_headers
# the full governance suite (old pre-V3 pipeline) starts real CRAFT runs and takes about 25 minutes:
# run it alone, never next to other heavy work on a laptop
web_v1\backend\.venv\Scripts\python.exe web_v1\backend\manage.py test governance
# frontend
cd web_v1\frontend; npx tsc -b; npx vitest run; npx vite build
```

## Older material

`frontend/legacy_demo/` holds the earlier fixture-backed demo pages, kept for
reference and excluded from the build. The governance TEST scopes of the
pre-V3 CRAFT (see [INTEGRATION.md](INTEGRATION.md)) still run on the same
server under `/api/v1/governance/`.
