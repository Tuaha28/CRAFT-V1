# Integrating two FastAPI applications

Version 0.2.0 adds JWT authentication and scope enforcement. For deployment, the main portal issues a short-lived customer JWT with the issuer, audience and signature configuration in `docs/deployment.md`. Demo credential mappings are only for local preview. Both the chat tool loop and write endpoints enforce `customer:write` before preparing or submitting requests.

Keep the scheduling backend on port 8000 and the agent on port 8001. The frontend calls the scheduling backend. It verifies the logged-in customer and forwards the question to `POST /chat` on the agent with a bearer credential mapped server-side to that customer. Do not allow the frontend to choose another customer ID.

The agent reads customer-facing records from the scheduling backend using a separate service credential. The main backend must verify that credential and independently scope queries to the forwarded verified customer context. In a production integration, prefer signed short-lived customer claims or your existing JWT verification rather than maintaining a token per customer. Never accept `X-Customer-ID` unless service authentication has already succeeded.

## Agent API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Active mode and configuration status |
| GET | `/products` | Available customer catalogue |
| GET | `/orders` | Authenticated account's orders |
| POST | `/chat` | Question, optional session ID |
| POST | `/requests/prepare` | Validate and show request details |
| POST | `/requests/{action_id}/confirm` | Explicit idempotent submission |

All except health need a bearer token. The chat request is:

```json
{"message":"Where is NC-1042?","session_id":null}
```

The response includes `answer`, `session_id`, `sources`, `tool_trace`, `pending_actions` and `mode`. Store the session ID per authenticated conversation, not globally across users. A confirmation receipt contains a request ID and status; it does not signify commercial or schedule approval.

## Main backend contract

Set these in the agent `.env`, then restart it:

```dotenv
BACKEND_URL=http://localhost:8000
BACKEND_SERVICE_KEY=reference-service-key
```

The agent sends `Authorization: Bearer <service key>` and `X-Customer-ID: <verified mapped customer>` to these endpoints:

| Method | Main backend path | Expected response |
| --- | --- | --- |
| GET | `/customer/products` | Catalogue array matching `app/store.py` product fields |
| GET | `/customer/orders` | Array of accessible customer-facing orders |
| GET | `/customer/orders/{order_id}` | Accessible order object matching reference fields |
| POST | `/customer/requests` | Confirmed submission receipt |

Missing and unauthorised orders return the same 404 result. Return only approved customer-facing fields, never machine logs, personnel, internal costs or other orders. The agent relies on the main backend contract for this filtering and ownership enforcement. The reference adapter cannot prove arbitrary third-party backend correctness.

The order object must include `customer_id` for the agent's independent ownership check. The agent verifies that it matches the authenticated subject and removes that field, plus any unexpected internal fields, before returning model evidence or customer results. Progress must be within 0 to 100, timestamps and prices must be valid, and the returned order reference must match the requested reference. Catalogue and submission receipts are validated too. This adds a second check; it does not replace backend authentication and ownership queries.

Requests use `kind`, `description` and relevant `order_id`, `style_code`, `quantity`, `colour` and `destination` fields. The POST includes `Idempotency-Key` equal to the prepared action ID. The main backend must persist an idempotency mapping for each customer and key, reject a conflicting reuse with a different payload, and return the same receipt for retries, including after a timeout. This prevents duplicate requests when the original write succeeded but the response was lost.

```json
{"request_id":"REQ-12345","status":"submitted","approval_status":"pending_review"}
```

The agent persists that receipt locally. It never calls a schedule-edit endpoint. Your friend's backend takes the request through approval and invokes its existing scheduling logic only when authorised.

## Try the included example backend

`examples/friend_backend.py` is a small contract demonstration, not your friend's actual scheduling application. From the project folder run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn examples.friend_backend:app --port 8000
```

Then set `BACKEND_URL` and `BACKEND_SERVICE_KEY` above and run the agent on 8001 and Streamlit on 8501. The catalogue, order lookup and confirmed request will now use HTTP between the two FastAPI services. Requests persist in a separate example-backend SQLite file.

## How to give this to your friend

Send the ZIP. Your friend installs `requirements.txt`, copies `.env.example` to `.env`, starts the agent and implements the contract in the existing backend. Streamlit is optional and can remain in the folder. The actual UI uses the same chat and confirmation response fields. You can also merge the agent into the existing codebase later, but do not simply mount it while bypassing its authentication dependencies.

On different laptops, localhost points to each laptop itself. Use the other laptop's reachable LAN IP during controlled testing. Docker containers also have separate localhost contexts; use the correct service hostname or host address. Hosted deployment uses a configured service URL and proper authenticated transport.
