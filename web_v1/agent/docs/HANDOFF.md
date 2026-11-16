# Backend integration handoff

## Responsibilities

The assistant owns retrieval, model/tool orchestration, conversation state and reviewable request preparation. The scheduling backend owns identity, order truth, catalogue truth, commercial approval and scheduling changes. The portal owns the customer interface and explicit confirmation controls.

## Implementation sequence

1. Start the agent using README.md and test one local answer.
2. Implement the four `/customer/*` endpoints in integration.md. Match `PublicProduct`, `PublicOrder` and `SubmissionReceipt` in `app/schemas.py`.
3. Configure `BACKEND_URL` and a private shared service credential. Verify service authentication before trusting `X-Customer-ID`. Query records by both order and customer.
4. Connect your portal login: issue a customer JWT matching the agent's issuer, audience and signing algorithm. Include `sub`, `exp`, `iat`, `nbf`, `iss`, `aud` and a space-separated `scope`. Require `customer:read`; add `customer:write` only when permitted.
5. Add a portal endpoint forwarding messages with that authenticated customer's credential. Do not accept an arbitrary customer ID from the browser or reuse one user's token for everyone.
6. Render `answer`, `sources` and `pending_actions`. Retain `session_id` per authenticated conversation.
7. Add review and confirmation controls. Forward confirmations with the same customer's credential. Handle expired actions and repeated confirmations.
8. Test ownership, failures and idempotency before customer release.

## Forwarding helper

Place this in your existing backend service layer. `customer_jwt` must come from your verified portal authentication flow and match the agent's verification settings. This helper is not a login implementation.

```python
import httpx

async def ask_customer_agent(customer_jwt: str, message: str,
                             session_id: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "http://localhost:8001/chat",
            headers={"Authorization": f"Bearer {customer_jwt}"},
            json={"message": message, "session_id": session_id},
        )
        response.raise_for_status()
        return response.json()
```

Derive the JWT from a verified authentication dependency in your router. Map upstream errors appropriately. Use a configured service URL in deployment. Customer JWT and backend service key have different purposes and are not interchangeable.

## Backend fields

Products require `style_code`, `name`, `fabric`, `gsm`, `base_price_usd`, `moq`, `sizes`.

Orders require `order_id`, `customer_id`, `style_code`, `quantity`, `colour`, `stage`, `progress_percent`, `updated_at`. Date, shipment, invoice and customer-note fields are optional; see `PublicOrder` for names and types. Return approved customer-facing fields only. The agent checks ownership and removes identity from model evidence.

Receipts require `request_id` matching `REQ-...`, `status="submitted"`, `approval_status="pending_review"`; `created_at` is optional.

The request POST receives an `Idempotency-Key`. Persist receipts by customer and key; return the same receipt on retries and reject conflicting payload reuse. Confirmed requests enter your backend's approval workflow. The agent does not edit schedules directly.

## Local contract check

Start the included reference backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn examples.friend_backend:app --port 8000
```

Set these in the agent `.env` and restart it:

```dotenv
BACKEND_URL=http://localhost:8000
BACKEND_SERVICE_KEY=reference-service-key
```

The published key is only for this development fixture. Configure a private shared value of at least 32 characters on the actual backend and agent. This fixture is not the scheduling implementation.

## Request validation

| Kind | Required fields besides `kind` and `description` |
| --- | --- |
| `quote` | `style_code`, positive integer `quantity`, `colour`, `destination` |
| `sample` | `style_code` |
| `order_change` | `order_id` |
| `support` | No additional schema-required fields |

Descriptions require at least 10 nonblank characters. Omit unused optional fields instead of sending empty strings or zero. Tool-level catalogue and policy checks also apply. Inspect response `detail` for 422 errors.

## Acceptance checks

- Responses using the live model show `mode=llm`; fallback is visible.
- Orders match backend records and timestamps.
- Customer A cannot access customer B's orders or sessions.
- Read-only accounts cannot prepare or confirm requests.
- Missing fields produce useful validation errors.
- Submission requires explicit confirmation; retries do not duplicate requests.
- Missing facts lead to clarification or support.
- Provider and backend failures are controlled.
- Company knowledge has owner approval before release.

## Replace reference data

Review data_provenance.md with the company. Replace assumed policies and products in the authoring scripts, rebuild the corpus and review approval metadata. Connect live order/catalogue APIs. The Word handbook is an editorial reference; JSON chunks are runtime retrieval input.

## Delivery boundaries

Included: source, templates, handbook, FAQ, integration fixture, tests and deployment assets. Excluded: API keys, real customer data, virtual environments, runtime databases, caches and personal settings.

Remaining validation depends on your Groq account, actual backend, company-approved data and deployment infrastructure. SQLite state is designed for one host. Optional web search needs its own configured provider; the Groq key alone does not enable it.
