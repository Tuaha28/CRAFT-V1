# CRAFT V1 private source recovery backup

This is a source-only backup, not a database or validated deployment image.
Production activation must remain disabled. Never run seed, migration, promotion,
or administrative scripts against `garment_fyp_db` during restoration.

## What was preserved

Source code, operation-level CP-SAT, Django/React integration, tests, dependency
manifests, configuration templates, development utilities, the industrial
completion ledger, and selected handoff records. Copied source bytes are bound
by SHA-256 in `SOURCE_BACKUP_MANIFEST.json`.

Original local history is NOT pushed. It contains production-planning
spreadsheets and generated research artifacts. The original repository remains
untouched at its existing history; this backup has an independent root commit.
Original HEAD: `ffc6c98c154a79c782cf4e5b958c3ecddbb39350`.

## Deliberate exclusions

Secrets and local environment files; database files/dumps; captured factory data;
planning spreadsheets; model weights; generated schedules and research results;
logs, screenshots and binary artifacts; capture-derived/unreviewed audit reports;
virtual environments, dependencies, caches and build outputs; local agent settings.
Exclusion paths/categories are recorded in the source manifest. No excluded
original file was deleted or edited by this backup operation.

These private data artifacts require a separate authorized encrypted backup.
Data-dependent tests and source-pinned experiments will NOT run from this clone
alone. Missing evidence is not permission to fabricate factory requirements.

## Restoration

1. Clone this private repository and check its manifest hashes.
2. Read `web_v1/INTEGRATION.md` and
   `web_v1/audits/INDUSTRIAL_COMPLETION_PROGRESS.md` before running anything.
3. Recreate isolated Python and Node environments from dependency manifests.
4. Configure new local secrets using `.env.example`; never commit real values.
5. Restore private captures/results separately and verify their original hashes.
6. Use only disposable TEST application databases for tests; obtain explicit
   authorization before changing any existing application database.

## Development checkpoint at backup time

The previously validated adaptive Factory1 12+13 TEST approval milestone is
recorded in the completion ledger. The newer six-factory governed integration
is work in progress and was interrupted for this backup. New capture adapters,
approved-boundary support, and governance dispatch edits have NOT yet passed
integration tests. In particular, review schema-choice migration requirements,
checkpoint semantics, preflight contracts, and source/global validation before
running multi-factory cohorts. Do not interpret a successful clone as software
correctness, six-factory validation, or industrial readiness.

Backup verification covers remote privacy, Git object integrity, fresh clone,
and file-hash parity. Pattern-based secret scanning is defense in depth, not a
formal guarantee against every possible encoded secret or confidential datum.

## Refresh 2026-10-17

Refreshed from local commit `2645fc0` (branch `craft-v1-consolidation`), the
pre-consolidation snapshot of CRAFT V1. This version adds the six-factory
TEST_CAPTURE governance path (`run_capture_governed.py`, `capture_test_state.py`,
`six_factory_governed_v1/run_progression.py`) and the ledger through 2026-10-17.
Known open defects at this point: Factory 2/5 worker crash in
`route_ready_probes` on empty resource configurations, and independent
machine/worker sampling that can miss every allowed pair. `handout.md` is
excluded from this backup because it contains a live credential. Flagged
secret-scanner lines were reviewed as false positives and are listed in
`SOURCE_BACKUP_MANIFEST.json`.
