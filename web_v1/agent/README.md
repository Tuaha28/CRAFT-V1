# Needle Craft Customer Assistant

FastAPI service for customer questions, garment catalogue enquiries, authorised order tracking and confirmed service requests. Integrate it with the scheduling portal through HTTP APIs; Streamlit is an optional development interface.

## Start here

Use Python 3.12. Extract this package and open **needlecraft-agent** in VS Code. These commands use Windows PowerShell and do not require activating the virtual environment.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
code .env
```

Run the copy command only for a new installation. Preserve an existing `.env` rather than overwriting it. Paste your own Groq key into `LLM_API_KEY`, save, then start the service:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001
```

Leave that terminal running. Open a second terminal:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Customer interface: http://localhost:8501. Local API documentation: http://localhost:8001/docs. Health: http://localhost:8001/health.

Linux/macOS: create the environment with `python3 -m venv .venv` and use `.venv/bin/python` instead of the Windows Python path.

## Configuration

The supplied `.env.example` uses Groq, local reference records and development customer credentials. Only `LLM_API_KEY` needs a value to start this local profile. Create your own key at https://console.groq.com/keys. Keep `.env` private.

| Setting | Purpose |
| --- | --- |
| `APP_ENV=local` | Local reference records and interactive API documentation |
| `AUTH_MODE=local` | Development token-to-account mapping in `CUSTOMER_TOKENS` |
| `LLM_MODE=llm` | Live model calls; `offline` provides local fallback behaviour |
| `LLM_BASE_URL` | Groq's OpenAI-compatible API endpoint |
| `LLM_MODEL=openai/gpt-oss-20b` | Model hosted by Groq; no OpenAI key is needed |
| `REQUIRE_GROUNDED_OUTPUT=true` | Validate final answers against recognised evidence |
| `REQUIRE_SEMANTIC_INDEX=false` | Start with lexical retrieval; no embedding API needed |
| `BACKEND_URL`, `BACKEND_SERVICE_KEY` | Optional connection to the scheduling backend |
| `TAVILY_API_KEY` | Optional public company search; not required for handbook/order queries |
| `CORS_ORIGINS` | Browser origins allowed to call the service |

`local` is a readable alias for the existing local runtime; older `demo` configuration values remain compatible. Local credentials are not deployment credentials. Restart the backend after changing settings.

Do not configure an OpenAI embedding model with a Groq chat key. Semantic retrieval requires a separately compatible embedding provider and a built index. The local profile intentionally uses lexical retrieval.

## Verify a live answer

With FastAPI running, open another PowerShell terminal:

```powershell
$headers = @{ Authorization = "Bearer local-customer-1" }
$body = @{ message = "Where is my order NC-1042?" } | ConvertTo-Json
$r = Invoke-RestMethod -Uri "http://localhost:8001/chat" -Method Post -Headers $headers -ContentType "application/json" -Body $body
$r.answer
$r.mode
$r.sources | ConvertTo-Json -Depth 10
```

`mode=llm` confirms the successful live-model path. `offline_fallback` means the model path failed and a local answer was returned. HTTP 200 alone does not establish a successful model call. `/health` reports configured mode, not live provider validation.

Development accounts: `local-customer-1` maps to CUST-001 (NC-1042, NC-1043); `local-customer-2` maps to CUST-002 (NC-2042); `local-visitor` is read-only.

## Connect the scheduling backend

Run the scheduling backend on port 8000 and this service on 8001. Your portal authenticates the customer, calls `/chat` with a customer credential, and renders the returned answer, sources and pending actions. The agent reads catalogue and order records from the scheduling backend using a separate private service credential.

The backend must implement:

| Method | Backend endpoint |
| --- | --- |
| GET | `/customer/products` |
| GET | `/customer/orders` |
| GET | `/customer/orders/{order_id}` |
| POST | `/customer/requests` |

Set `BACKEND_URL` and `BACKEND_SERVICE_KEY` in the agent's `.env`. The backend verifies the service credential and enforces customer ownership independently. Details, schemas and a proxy example are in **docs/HANDOFF.md** and **docs/integration.md**. `examples/friend_backend.py` illustrates the contract; adapt your actual backend to it.

Keep the services separate initially. The existing portal frontend replaces Streamlit in the integrated product; Streamlit can stay available for development without being deployed. On different laptops, `localhost` is not the other laptop: configure a reachable address. In Docker, use the appropriate service hostname.

## API and requests

| Method | Agent endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Configuration status |
| GET | `/readyz` | Local state and circuit readiness |
| GET | `/products` | Catalogue |
| GET | `/orders` | Accessible orders |
| POST | `/chat` | Question plus optional `session_id` |
| POST | `/requests/prepare` | Validate a request and return a review action |
| POST | `/requests/{action_id}/confirm` | Explicit, idempotent submission |
| GET | `/metrics` | Private operational metrics |

Business endpoints require a bearer credential. Metrics require a separate monitoring credential. Preserve each returned `session_id` within that customer's conversation. A pending action must be displayed for review and confirmed separately. Submission does not mean commercial or scheduling approval.

Quote preparation example:

```json
{
  "kind": "quote",
  "style_code": "NC-TEE-180",
  "quantity": 1000,
  "colour": "navy",
  "destination": "Amman",
  "description": "Request a quotation for plain navy T-shirts without printing."
}
```

A quote requires all four product/delivery fields. A sample requires `style_code`; an order change requires `order_id`. All requests require a nonblank description of at least 10 characters. Missing or invalid fields return 422. The current reference quote policy also validates catalogue and minimum quantity constraints.

## Knowledge and data

The knowledge pack covers 42 topic areas and 240 authored questions: company information, fabrics, sizing, branding, sampling, prices, ordering, progress, shipping, payments, complaints, reorders, account access and service requests. The agent can handle paraphrases, but unsupported facts require clarification or a support handoff.

The Word handbook is a readable reference document. **It is not read directly on every chat.** Runtime retrieval uses `data/chunks.json`, built from the knowledge authoring scripts. The extended FAQ provides additional coverage.

To update the corpus, edit `scripts/build_knowledge.py` and `scripts/build_extended_faq.py`, then run:

```powershell
.\.venv\Scripts\python.exe scripts\build_knowledge.py
```

Restart the API afterwards. If semantic indexing is enabled, rebuild that index too. Manual Word edits do not automatically change runtime answers. Word regeneration uses `scripts/build_handbook.py` and requires `python-docx` separately.

Business prices, catalogue, payment policies and orders are reference assumptions, not company-approved facts. See **docs/data_provenance.md**. Replace and approve them before use with real customers.

## Tests and troubleshooting

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\evaluate_retrieval.py
```

To check your running model integration:

```powershell
$env:VALIDATION_CUSTOMER_TOKEN="local-customer-1"
.\.venv\Scripts\python.exe scripts\live_validate.py --cases 1 --check-orders
code reports\live_validation.json
```

Review the answers as well as technical results. Groq's free-plan quotas can limit a multi-call agent. Start with one request at a time.

| Symptom | Check |
| --- | --- |
| Model 401 | Groq key, saved `.env`, restarted API |
| Model 429 | Provider rate limits or quota |
| Agent 401/403 | Customer token/JWT and required scopes |
| Request 422 | Response `detail`, required fields, valid quantities and catalogue rules |
| Order 404 | Order reference and authenticated ownership |
| Frontend cannot connect | API is running; configured port/address is reachable |
| `offline_fallback` | Provider access, rate limits and grounding validation; it is not a live-model success |

## Deployment

Start from `.env.production.example`, then follow **docs/deployment.md**. Configure portal JWT verification, backend service credentials, exact CORS origins and a private metrics token. Replace reference knowledge with approved data. The deployment template deliberately refuses unapproved reference knowledge unless you explicitly opt in for an internal presentation.

State is SQLite WAL on a persistent local disk. This design supports workers on one host; independent hosts require shared database/limit infrastructure. Back up state, configure retention and keep keys out of version control. Docker assets are included; their execution has not been verified in this build environment.

## Package map

- `app/`: API, agent, tools, authentication, retrieval and state.
- `data/`: retrieval corpus and evaluation questions.
- `docs/`: handbook, integration, deployment, provenance and validation.
- `tests/`: regression and security/workflow checks.
- `scripts/`: knowledge builds, evaluation and maintenance.
- `examples/`: reference backend contract implementation.
- `streamlit_app.py`: optional development customer interface.

Groq tool planning and strict final JSON output are separate requests. Evidence and schema checks run locally. Automated tests use simulated providers; no real Groq key was available for build-time verification. Automated checks do not guarantee semantic accuracy for every possible question. See **docs/validation.md** for recorded checks and remaining validation.
