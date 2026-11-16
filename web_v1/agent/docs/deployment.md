# Deploying the customer agent for your FYP

Version 0.2.0 includes a deployment configuration with signed identity, guarded answers, durable limits, dependency failure handling and monitoring. It is intended for one application host with multiple API workers sharing a local state volume. It is not an architecture for multiple independent hosts sharing SQLite over a network filesystem.

## First run in VS Code

Extract the ZIP, open the project folder and use Python 3.12. In PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\setup_env.py --mode demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8001
```

In another terminal start the preview:

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

This offline mode lets you inspect products, retrieval, order access and confirmed requests immediately. Model-driven dialogue needs the model setup below. If an `.env` exists, edit it; the setup script deliberately does not overwrite credentials.

## Enable model-driven operation

Configure an available model supporting Chat Completions function calls and structured JSON outputs. A hosted configuration example is:

```dotenv
LLM_MODE=llm
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your_private_provider_key
LLM_MODEL=gpt-4.1-mini
EMBEDDING_MODEL=text-embedding-3-small
REQUIRE_GROUNDED_OUTPUT=true
```

Confirm access to these models in your account. The package uses the HTTP API directly, so a provider must support the requested function-calling and JSON-schema response protocol, not merely plain text chat. Source documentation: https://developers.openai.com/api/docs/models/gpt-4.1-mini and https://platform.openai.com/docs/guides/embeddings.

For local Ollama, use `LLM_BASE_URL=http://localhost:11434/v1`, an installed tool-capable model and a compatible installed embedding model. Set `LLM_API_KEY=ollama` if appropriate. Local model output quality is not assumed equivalent to a hosted model; use the same validation process.

Build the index, then restart the service:

```powershell
.\.venv\Scripts\python.exe scripts\index_embeddings.py
```

Indexing sends the project customer knowledge to the selected embedding provider and may incur provider charges. This pack contains assumed public-facing policies. Do not add private customer or staff documents to this public corpus.

## Switch to the deployment configuration

For a fresh environment, `scripts/setup_env.py --mode production` creates `.env` from `.env.production.example` and generates local signing, monitoring and backend-service secrets without printing them. For an existing `.env`, copy the required settings manually from the example and generate your secrets privately.

Match the portal's JWT issuer, audience, algorithm and verification key. HS256 uses a shared signing secret, so only trusted backend services can possess it. RS256 is supported with the portal public key in `JWT_VERIFICATION_KEY`; the agent must not hold the portal private signing key. Escaped newlines in the environment are decoded for PEM public keys.

Customer JWTs require `sub`, `iss`, `aud`, `iat`, `nbf`, `exp` and a space-separated `scope`. The subject is the verified customer account identifier. Read permission is `customer:read`; request preparation and confirmation also require `customer:write`. In the reference project, subjects CUST-001 and CUST-002 match the example orders. Never let an untrusted client choose its subject.

For a local HS256 integration test only, `scripts/mint_test_token.py CUST-001` produces a token lasting 15 minutes. Paste it into the Streamlit access-token field. The real portal must issue tokens from its verified login process; do not expose this helper as a public endpoint.

Production startup rejects demo authentication, offline model mode, missing backend settings, weak secrets, unrestricted CORS and disabled answer guards. The example requires a semantic index. `ALLOW_REFERENCE_DATA=true` is an explicit setting for your assumed-data FYP presentation, not company approval. Set it false when replacing the pack with company-approved records. Production never seeds reference orders in its local state database.

## Connect the scheduling application

Implement the contract in `docs/integration.md`, set `BACKEND_URL` and match `BACKEND_SERVICE_KEY` in the main backend. The agent sends a service credential and a customer subject derived from the verified JWT. The main backend must authenticate the service and enforce record ownership. The agent also checks returned owner and order reference, validates fields, and strips unapproved internal fields.

For the included example backend, set `EXAMPLE_SERVICE_KEY` to the matching generated backend secret in the backend process environment. Start it with `python -m uvicorn examples.friend_backend:app --port 8000`. It illustrates the contract, not your friend's actual production application.

The main backend must persist idempotency by customer and key, reject conflicting reuse, and return the same receipt after a lost-response retry. This is essential because an HTTP timeout does not prove that the write failed. Request submission always remains separate from schedule or commercial approval.

## Container deployment

Configure `.env`, build the semantic index before building the image, and run:

```powershell
docker compose up --build -d
docker compose logs --tail 100 agent
```

The container runs as a non-root user with two workers, a read-only root filesystem, dropped capabilities, a private local port and a writable named state volume. SQLite rate, quota, circuit and conversation lease state is shared by both workers. Use a local disk volume; scale to a shared server database before adding independent application hosts.

If the main backend or Ollama runs on your host laptop, container localhost points to the container. Use `http://host.docker.internal:8000` for the host backend, or the appropriate Docker service hostname. Serve public traffic through your existing HTTPS reverse proxy. Keep the agent private behind the main backend where possible. For an older state volume created by a root container, back it up and correct volume ownership for UID 10001 before switching images.

## Validation before presentation or release

Run automated checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\evaluate_retrieval.py
.\.venv\Scripts\python.exe scripts\smoke_integration.py
```

The last command starts local example services temporarily. It does not call a real LLM or use your friend's backend. It explicitly uses the reference API contract and laptop test identity.

With the configured service running, set `VALIDATION_CUSTOMER_TOKEN` privately to a valid customer token and run:

```powershell
.\.venv\Scripts\python.exe scripts\live_validate.py --cases 24 --check-orders
```

If public search is configured, add `--check-web`. These are real potentially billable provider calls. The generated report checks response mode, evidence presence and tool execution. Review every answer for semantic accuracy, not only a passing technical result. Run the 32 cases in `docs/scenario_queries.json`, especially permission attacks, paraphrases, combined questions and the languages you want to support. All 240 authored retrieval questions can be evaluated without provider calls; they are not 240 verified model-generated answers.

## Monitoring and maintenance

`/health` reports process status. `/readyz` checks local state and known required-provider circuit failures; it is not a substitute for live model evaluation. `/metrics` requires the separate monitoring bearer token and exports counts without customer labels. Counters include HTTP failures, provider retries, blocked dependencies, tool outcomes, model token usage, handoffs and deadline failures.

Defaults allow 30 authenticated API requests per account per fixed 60-second window, 200 chat attempts per fixed 24-hour quota window, eight global in-flight chats and one in-flight chat per account. Windows begin at the first request after the previous window expires. A conversation cannot be processed by two workers simultaneously. Limits are configurable in `.env`; use explicit shared settings when running two workers.

The network layer retries bounded transient failures and opens a shared circuit after repeated provider failures. Writes are retried only with their stable idempotency key. Chat has a total deadline and finite tool/context/output budgets. Model answers need recognised evidence IDs; unsupported numeric values or links are rejected. These guards do not prove every sentence is semantically correct, which is why live answer review remains necessary.

Use `scripts/maintain_state.py backup --destination backups/state.sqlite3` for a consistent local backup. Retain protected copies outside the host and test restoration. Schedule `scripts/maintain_state.py cleanup` through your deployment's scheduler to apply the reference retention configuration: inactive conversations 30 days and local requests/audit events 90 days. Agree the actual retention policy before real company use. The script does not delete orders from the main backend.

## Evidence limits

Automated security and failure checks have been run. Local HTTP integration has been run. A real hosted/local model, a live semantic embedding provider, public search credentials, your friend's actual backend and a Docker engine were not supplied to the build environment. Those are explicitly reported as not live-tested. No system can honestly be guaranteed to answer every conceivable question perfectly; unsupported requests follow clarification or support review.
