---
name: craft-industrial-web
description: Design and implement the isolated CRAFT garment-factory operations web application with React and Django REST Framework. Use for CRAFT dashboards, schedule visualization, factory monitoring, production control, disruption workflows, or CRAFT result views; keep initial work on mock data unless live integration is explicitly requested.
---

# CRAFT Industrial Web Application

Build the new CRAFT operations product as a separate application in `web_v1/`, with a React frontend and Django REST Framework backend. The existing scheduler, legacy schedulers, agent code, data-generation system, database, and Streamlit dashboard are outside this application's implementation boundary.

## Integration boundary

- Start with deterministic mock fixtures and a mock API. Label mock data and simulated actions clearly in the interface and code.
- Do not import CRAFT, GA, GNN, agent, or database modules into the initial web scaffold. Do not connect to a live database or invoke scheduling from mock controls.
- Treat a later CRAFT integration as a separately authorized backend adapter. Keep REST contracts independent of scheduler implementation details and preserve run IDs, status, validation, replay, and acceptance evidence when those contracts are eventually introduced.
- Never present `COMPUTATIONAL_LIMIT`, rejected schedules, or validation failures as accepted schedules. Explain that the validated active schedule remains in force when a run is not accepted.
- Inspect only the web files relevant to the requested change. Preserve the React + Django direction and leave the existing `frontend/dashboard.py` Streamlit application untouched.

## Product and interaction design

Design an industrial operations tool for factory managers and production teams. Use a desktop-first, dense but readable workspace with responsive tablet/mobile behavior. Prioritize operational hierarchy, clear typography, accessible contrast, restrained status colors, compact KPI treatments, meaningful tables, schedule timelines, and polished selection/hover states. Avoid generic student-dashboard styling, oversized cards, decorative gradients, excessive rounding, gimmicky AI motifs, and motion without operational purpose.

Each page must answer a concrete operational question:

- Dashboard: what needs attention now?
- Schedule: what is running, what comes next, and where is risk?
- Orders: which orders are progressing or at risk against release and due dates?
- Resources: what machine, line, and workforce capacity is available?
- Disruptions: what changed and what action is awaiting review?
- CRAFT control: what did a scheduling run consider and decide, and did it pass acceptance?
- Analytics and history: how are delivery, utilization, schedule stability, and prior decisions changing?

Use explicit loading, empty, stale-data, error, and permission-aware states. Define technical terms with concise tooltips where operators need them. Keep mock interactions visibly simulated and reversible.

## Application structure

Prefer a feature-oriented frontend with shared shell/components and typed API contracts. A reasonable starting point is:

```text
web_v1/
  frontend/
    src/app/                 # routing, app shell, providers
    src/features/            # dashboard, schedule, orders, resources, disruptions, craft, analytics, audit
    src/components/          # reusable tables, filters, status, drawers, empty/loading/error states
    src/design-system/       # tokens and shared primitives
    src/mocks/                # fixtures and mock handlers
    src/lib/                  # API client, formatting, time utilities
  backend/
    config/                  # Django project settings and URLs
    apps/                     # versioned REST apps and serializers
    fixtures/                 # deterministic development data
    tests/
```

Keep Django as the REST boundary. Initially serve fixture-backed read endpoints and simulated, non-persistent mock actions only. Do not add production database settings or scheduler imports during scaffold/design work.

## Reusable interface map

Build around an application shell with factory/context selector, primary navigation, time/status context, and user-role placeholder. Shared components should include a compact KPI strip, status badge, filter/search bar, data table, date/time formatter, resource availability indicator, risk marker, right-side detail drawer, timeline row/lane, confirmation dialog, and consistent loading/empty/error panels.

The page map should cover:

1. Executive dashboard and attention queue.
2. Production schedule with factory/line lanes, operation bars, filters, and detail drawer.
3. Orders list and order detail/routing progress.
4. Resources overview for lines, machines, workers, shifts, calendars, and assignments.
5. Disruption center with event review and clearly mocked actions.
6. CRAFT control center for run scope, bounded window, CP/controller outcome, duration, changed operations, and validation evidence.
7. Analytics and audit/run history.

Keep role awareness in navigation and presentation for Admin, Manager, Production/Line Manager, and read-only Operator roles. Do not build complex authorization until requested.

## Visual system

Create semantic design tokens for surface/background, text, border, spacing, typography, density, and status. Use a neutral industrial base with one restrained brand/accent color and consistent semantic states (success, warning, danger, informational, inactive). Do not assign colors arbitrarily; the same state must look the same across tables, timelines, and summaries. Use a consistent icon set and accessible labels; icons must not carry meaning alone.

The schedule workspace should use a real timeline/Gantt presentation rather than cards. Preserve readable time scales, line/resource grouping, overlap visibility, selection, and keyboard-accessible details. Do not fabricate exactness for mock schedules; show the fixture/source state in the UI.

## Frontend and backend implementation guidance

- Use React with TypeScript and a modern component architecture. Prefer Vite for a standalone SPA unless the repository or a later requirement establishes another build system.
- Keep routing, server-state fetching, visual primitives, domain feature components, and fixture data in separate modules.
- Use Django + Django REST Framework for API contracts. Keep serializers and endpoint payloads explicit and versionable; do not expose internal Python objects as API responses.
- Add only libraries needed by the requested screen. For a new scaffold, consider React Router for page routing, TanStack Query for server state, a restrained accessible primitive/component system (for example Radix UI with project-owned styling), and Recharts for ordinary charts. For a dense interactive schedule timeline, evaluate a timeline/Gantt component against row virtualization, zoom, accessibility, and customization needs before selecting it; do not force a chart library to act as a scheduler.
- Use one shared formatting layer for timestamps, durations, status labels, and units.
- Run relevant frontend lint/type/build checks after meaningful implementation changes. Do not run scheduling experiments or database mutation workflows as a web check.

## Working method

For each web request, inspect only the existing web files and configuration needed for that change, then implement the smallest coherent feature using shared components and the agreed design language. Keep mock and live data providers separate. State clearly when a requested interaction is simulated. Do not expand a screen task into live CRAFT/database integration, authorization, or deployment unless that work is explicitly requested.
