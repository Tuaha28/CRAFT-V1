# CRAFT Industrial Operations Web V1

An opt-in, isolated CRAFT scheduling/governance vertical slice is now available.
See [INTEGRATION.md](INTEGRATION.md) for its separate application database,
worker, authentication, candidate review, tests and explicit live-admission limits.
The original demo described below remains separate; only the isolated worker
imports the scheduling stack. This is **not a live factory deployment**.

The `/schedule` page now has two explicitly separated views: the original
illustrative demo and a controlled TEST timeline populated from persisted,
validated CRAFT candidate/approved TEST schedule versions. The controlled view
requires an authenticated integration session and checks the comparison API's
validation evidence before drawing operation bars. It is not a live production
Gantt; see [INTEGRATION.md](INTEGRATION.md) for setup and safety gates.

An isolated, desktop-first operations-control demo. The application is intentionally backed by deterministic fixture data and a process-local Django REST API. It has no CRAFT scheduler imports, no legacy scheduler imports, and no production database connection.

## Architecture

- `frontend/` — React 19, TypeScript, Vite, React Router, TanStack Query, TanStack Table/Virtual, Radix UI primitives, Recharts, Lucide, and SVAR React Gantt (MIT open-source edition).
- `backend/` — standalone Django + Django REST Framework API. The fixture service holds mock records in memory; Django's configured SQLite database is `:memory:` and is not used by API endpoints.
- UI reads go through typed frontend contracts and the `services/api.ts` layer. TanStack Query owns caching, refetching, loading/error states, and mutation invalidation.
- Demo mutation routes are namespaced under `/api/v1/demo/`, return an explicit demo notice, and update only process-local fixture objects.

## Requirements

- Node.js 20+ and npm 10+
- Python 3.11+
- Windows PowerShell commands below are shown for this repository; equivalent `python -m venv`/`pip` commands work on other platforms.

## Install and run the backend

```powershell
cd web_v1/backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python manage.py check
python manage.py runserver 127.0.0.1:8000
```

Backend URL: `http://127.0.0.1:8000`  
Health: `http://127.0.0.1:8000/api/v1/health`

No database migration is required. No source/project production database credentials are read.

## Install and run the frontend

In a second terminal:

```powershell
cd web_v1/frontend
npm.cmd install
npm.cmd run dev
```

Frontend URL: `http://127.0.0.1:5173`  
The Vite dev server proxies `/api` to `http://127.0.0.1:8000`.

Checks:

```powershell
npm.cmd run lint
npm.cmd test
npm.cmd run build
```

## Demo/mock mode

The global `DEMO / MOCK DATA` badge is always visible. Six factory fixtures, four production lines per factory, 36 orders, three routing operations per order, machines, shift-qualified workers, disruptions, CRAFT run evidence, analytics, and audit rows are generated deterministically. Demo events/priority changes/runs are held only in the running backend process and disappear when it restarts. They never invoke a scheduler or persist to a database. The sample `COMPUTATIONAL_LIMIT` case explicitly states that existing validated schedule remains active and physical infeasibility is not proven.

## Page map

| Route | Operational question |
|---|---|
| `/dashboard` | What needs attention right now? |
| `/schedule` | Compare the illustrative demo with validated, application-owned TEST schedule versions. |
| `/orders` | Will each PO finish on time? |
| `/resources` | What resource capacity is available? |
| `/disruptions` | What changed and what requires repair? |
| `/craft` | What did CRAFT decide, and can it be trusted? |
| `/analytics` | How are delivery, resources, and repair outcomes trending? |
| `/history` | What happened, who triggered it, and what was accepted? |

The schedule page uses SVAR React Gantt in read-only mode. The illustrative view
contains generated demo operations. The controlled TEST view uses only persisted
candidate rows, shows changed prior-active assignments as outlines, provides a
resource detail drawer, and withholds bars if validation evidence is not accepted.
Neither view edits or promotes schedule data; manager decisions remain in
`/governance`.

## API map

`GET /api/v1/health`  
`GET /api/v1/dashboard/summary`  
`GET /api/v1/factories` · `GET /api/v1/lines`  
`GET /api/v1/orders` · `GET /api/v1/orders/:id`  
`GET /api/v1/resources/machines` · `GET /api/v1/resources/workers`  
`GET /api/v1/schedule` · `GET /api/v1/disruptions`  
`GET /api/v1/craft/runs` · `GET /api/v1/craft/runs/:id`  
`GET /api/v1/analytics` · `GET /api/v1/audit`  
`POST /api/v1/demo/disruptions` · `POST /api/v1/demo/orders/:id/priority` · `POST /api/v1/demo/craft/run`

List endpoints accept filters such as `factory_id`, `line_id`, `status`, `risk`, `priority`, and `search` where applicable, and return `{count,next,previous,results}` pagination envelopes. API errors use `{error:{code,message,details}}`.

## Controlled CRAFT integration boundary

The original demo API remains fixture-backed; its `api/integrations.py`
placeholder does not execute CRAFT. Separately, opt-in Django governance runs
the existing CRAFT solver in a background subprocess for isolated `TEST` and
`TEST_SIM` scopes, persists validated candidates, and permits manager-approved
TEST-only promotion. The controlled schedule view reads those governed records.
See [INTEGRATION.md](INTEGRATION.md) for exact startup and browser-test commands.
Live factory admission and production promotion remain disabled.

## Accessibility and layout

The layout targets 1440–1920px factory/manager workstations, contracts to laptops/tablets, and provides constrained mobile behavior. Focus styles, semantic controls, keyboard-compatible Radix dialogs, visible status labels, and contrast-conscious semantic colors are included. Tables keep sticky headers and explicit empty/loading/error states.
