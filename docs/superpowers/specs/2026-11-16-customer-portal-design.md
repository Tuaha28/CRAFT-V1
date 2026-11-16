# Customer portal — design (2026-11-16)

The user asked for the customer portal before the factory simulator and said "do it" instead of answering
design questions, so the calls below are the builder's (marked **call**) unless stated as the user's.

## Intent

A buyer at a brand that ordered from the factory (Needle Craft: the factory data names its products
"NeedleCraft Elite …") signs in and sees **their own orders only**: where each one is, when CRAFT's approved
plan says it will be ready, shipping; can ask the customer assistant questions; and can send requests (order
change, sample, quotation) that factory staff review. Built on the downloaded **Needle_Craft_Client_Agent**
(user decision, 2026-11-16).

## Goals

1. Customer accounts tied to one company (`purchase_orders.client_name`) in one dataset (30/60/90% load).
2. Customer pages: My orders, Order detail (journey + planned ready date), Catalogue, Assistant, My requests.
3. The agent runs as its own service (its documented integration) and reads customer data only through our
   backend's `/customer/*` contract with a service key and the verified customer id.
4. Customer requests reach staff (production manager, merchandiser) who accept or decline them.

## Non-goals / anti-goals

- A customer must never see another customer's orders, internal data (machines, workers, costs, other
  pages), or any staff API. Isolation failures are the worst outcome.
- No customer action ever edits a plan or the database; requests are reviewed by staff (agent's own rule).
- No research wording in customer pages (client product).
- Not claiming the handbook's policies are approved: the agent's provenance says catalogue, prices, MOQ and
  policies are assumed. The catalogue page says "indicative, not a quotation".

## Decisions

- **call:** Portal lives inside the existing app: Django app `portal` + React pages shown when the signed-in
  user is a customer. Same login page; customers land on `/portal`.
- **call:** Agent vendored as `web_v1/agent/` (source, docs and knowledge; no `.env`, no Streamlit needed),
  run from its own venv (Python 3.13) by `manage.py run_agent`, which passes keys as environment variables
  read from `backend/.env` (the Groq key is never copied into another file).
- **call:** Agent auth = its JWT mode. Django mints a 10-minute HS256 token (`sub` = customer id `CUST-<n>`,
  `scope` = `customer:read customer:write`, `iss` `needlecraft-portal`, `aud` `needlecraft-agent`) with
  `CRAFT_AGENT_JWT_SECRET` (≥ 32 bytes). Agent → backend calls use `CRAFT_AGENT_SERVICE_KEY` and
  `X-Customer-ID`; unknown or inactive customer ids get 404/403.
- **call:** Order ids are `NC-<production_order_id, 4+ digits>`; one production order = one order line.
- **call:** Stage and progress from the factory records (same journey as the staff Order page); planned ready
  date from the approved CRAFT plan of the order's factory when the order is in it, else the order's
  scheduled end date; arrival from the delivery record.
- **call:** Catalogue = the agent's four reference families (served by `/customer/products`), so the agent's
  answers, request validation and the page agree. The customer's own styles appear in their orders.
- **call:** A request middleware blocks customer sessions from every `/api/` path except
  `/api/v1/portal/*`, session, login and logout (defence in depth beyond per-view checks).
- **call:** Staff permission `portal.handle_request` for production manager and merchandiser; admin creates
  customer accounts (has `floor.manage_users`).
- If the agent is not running, every portal page still works; the Assistant shows "not available".

## Contract (agent ⇄ backend), from the agent's docs

`GET /customer/products`, `GET /customer/orders`, `GET /customer/orders/{id}` (404 for missing and not
owned alike), `POST /customer/requests` with `Idempotency-Key` (same key + same payload → same receipt;
same key + different payload → 409). Receipt `{"request_id":"REQ-<n>","status":"submitted",
"approval_status":"pending_review","created_at":…}`. Order objects carry `customer_id`, `order_id`,
`style_code`, `quantity`, `colour`, `stage`, `progress_percent`, `estimated_ready_date`,
`estimated_arrival_date`, `destination`, `carrier`, `tracking_reference`, invoice fields (null),
`customer_note`, `updated_at`.

## Left to the builder

Exact page layout within the existing design system; wording; test names.
