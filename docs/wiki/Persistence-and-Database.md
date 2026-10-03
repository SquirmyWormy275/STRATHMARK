# Persistence and Database

The current separate Linux competition profile (STRATHMARK 3.0.0rc4 / STRATHEX 7.3.1) supports explicit engine choice, real Formula/ML, local signed approval and issue, settlement, restart, and later-round learning. See the [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md). Windows CNG production qualification remains separate; the LLM council is unavailable in this local profile.

## Authority status

V3 is a release candidate whose source-bound rehearsal does not change authority. V2
remains the globally trusted production authority, V3 is not production-eligible, and
no production authority has changed.

V2 uses the local result store, trusted prediction ledger, shadow receipts, and optional
Supabase mirror documented by the V2 contract.

V3 uses a separate local SQLite event authority. Events are closed, canonical,
hash-chained, consecutively versioned, and appended through idempotent expected-version
commands. Projections are disposable views rebuilt from the log. Large canonical
evidence, forecasts, and bundles live in content-addressed blob storage and are bound by
digest.

Prepared cards, fields, approvals, issue acknowledgments, credentials, jobs, and
settlements are explicit aggregates. Exact retries recover original bytes. Changed
retries, stale versions, illegal transitions, gaps, duplicates, and tampering fail
closed. A typed approval decision atomically binds selected and excluded receipt
revisions; it remains distinct from issue authority. A batch issue commits all fields or
none; issued receipts never mutate. Live
settlement requires the complete issued roster and commits observations and settlement
as one command. Seven source-bound reactions must then close durably before the
derivation barrier opens; reaction automation never creates a judge approval decision.

The optional mirror/archive is best-effort and asynchronous. It is not required for
calculation, issue, lookup, settlement, recovery, or model scoring. V2 import is a
repeatable read-only snapshot; V2 and V3 never become concurrent trusted writers.

Tests and rehearsals must set STRATHMARK_TEST_DB=1 plus unique V2 and V3 database and
base paths. Known production identifiers and default operator paths are rejected.

See the canonical [architecture](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md) and
[deployment runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md).
