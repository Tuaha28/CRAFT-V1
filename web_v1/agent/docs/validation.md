# Validation of version 0.2.0

Version 0.2.1 adds Groq-specific separation of tool requests and strict final JSON output, covered by a simulated-provider regression test. The full updated suite has 39 tests. No live Groq credential was supplied; account-level validation must be run using `docs/groq_setup.md`.

Checked on 12 October 2026 with Python 3.12.

| Check | Result | Practical meaning |
| --- | --- | --- |
| Automated workflow and hardening tests | 38 passed | Signed identity, scope enforcement, ownership, isolation, payload validation, durable limits, leases, confirmation, replay, expiry, retry/circuit failures, guards, cleanup and backup |
| Authored-question retrieval | 240 of 240 expected top section | Literal FAQ matching plus lexical retrieval; not unseen phrasing or model-answer accuracy |
| Real HTTP between two FastAPI services | Passed | Reference catalogue/order/request contract, account isolation, confirmed request and replay |
| Python compilation | Passed | Valid source syntax |
| Streamlit initial render | No AppTest exceptions | Updated preview starts; not a full browser interaction test |
| Original Word handbook | 26 pages previously rendered and visually reviewed | Core document unchanged; extended questions supplied separately |

Model planning was exercised using mocked provider responses, including order lookup and request preparation. Production-mode failure was tested to produce a guarded handoff rather than an assumed offline answer. Unknown citations, unsupported numbers, short secrets, missing production configuration and unscoped writes were tested.

Rate and lease tests used independent Store instances sharing a SQLite database and concurrent threads. They establish local shared-state atomicity, not an external database cluster or performance benchmark. The deployment supports multiple workers sharing local storage on one host.

The 240 authored questions cover 42 topics in 312 chunks. The 32 separate scenarios cover paraphrases, multi-part requests, permissions, model attacks, failures and language variation. They remain a manual live-model review set, not reported as passing model-quality checks.

No real hosted/local model, semantic embedding provider or public search key was supplied. No actual scheduling backend repository was supplied. Docker is configured but not built or launched here. `scripts/live_validate.py` runs explicit provider checks once configured and produces a report requiring human answer review. `/readyz` checks local readiness and known open circuits; it does not certify model quality.

No release claim should turn these bounded test results into a promise of perfect answers or all conceivable task capabilities.


## Professional handoff check — 2026-10-13

The full automated suite passed: 40 tests. Added a local-profile authentication regression test. All Python sources passed syntax parsing. The handoff includes a Groq local template, a separate JWT deployment template, and backend handoff instructions. No real provider key was available for live model validation. The handbook was retained without changes.
