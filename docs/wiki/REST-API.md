# REST API

Start with `uvicorn strathmark.api:app --host 127.0.0.1 --port 8000`; interactive
OpenAPI docs are at `/docs`. For `/health` and all six trusted-shadow operations,
the live documentation is overlaid from the checksum-verified frozen consumer
contract, including its security requirements and closed error schemas. Trusted
numeric request fields reject numeric strings and booleans instead of coercing
them.

| Route | Authentication | Persistence |
| --- | --- | --- |
| `GET /health` | public | none |
| `POST /predict` | public | none |
| `POST /calculate` | public | none |
| `POST /simulate` | public | none |
| `POST /ledger/calculate` | Bearer token | append-only trusted field |
| `POST /ledger/predictions/{prediction_id}/settle` | Bearer token | immutable settlement revision |
| `POST /results` | Bearer token | local result history |
| `GET /results/{competitor_name}` | Bearer token | local result history read |
| `POST /v1/shadow/calculate` | service bearer + v2 actor attestation | atomic immutable receipt and local mirror outbox |
| `POST /v1/shadow/receipts/lookup` | service bearer + v2 actor attestation | immutable receipt read and live projection |
| `POST /v1/shadow/status` | service bearer + v2 actor attestation | bounded aggregate/status read |
| `POST /v1/shadow/outcomes/apply` | service bearer + v2 actor attestation | append-only settlement/void revision |
| `POST /v1/shadow/mirror/replay` | admin service bearer + v2 actor attestation | bounded off-path delivery attempts |
| `POST /v1/shadow/drift` | service bearer + judge/admin v2 actor attestation | bounded advisory read |

Set `STRATHMARK_API_TOKEN` to enable protected routes. If it is absent they return 503;
an invalid token returns 401.

The six `/v1/shadow/*` operations use a separate, closed authentication boundary:
`STRATHMARK_SHADOW_SERVICE_CREDENTIALS` maps consumer IDs to bearer secrets, while
`STRATHMARK_SHADOW_ATTESTATION_KEYS` maps them to disjoint signing keys. The required
v2 attestation carries consumer, actor, role snapshot, allowed action, run revision,
audience, nonce, short expiry, and a digest of the exact canonical validated body.
Credential and signing-key values must not overlap. Replays, mismatched bodies, roles,
actions, or revisions fail closed.

Trusted calculation requires `STRATHMARK_TRUSTED_TOPOLOGY=offline-single-writer-durable`,
a writable durable SQLite ledger, and a current integrity-verified local evidence
snapshot. These readiness checks happen before nonce claim. Calculate rechecks
evidence after persistence to close concurrent-refresh races. Receipt-bound settlement
and void require authentication and durable single-writer readiness but deliberately do
not require a current snapshot, allowing bad evidence to be retracted during an evidence
refresh outage. Mirror replay is administrator-only; drift is advisory and never a
race-day blocker.

`/health` accepts optional `prediction_as_of=YYYY-MM-DD` to evaluate the core and
calibration against a historical exclusive cutoff; without it, UTC today is used.
Its public shadow-readiness projection is constant-work and read-only: it never
rescans snapshot rows or reserves the SQLite writer. Evidence is reported ready only
from an in-process attestation populated by a successful full verification on an
authenticated bounded workflow. A restart, cache miss, or observed database-file
change reports evidence unavailable and not ready until that verification runs again.
Freshness is still recomputed from the attested `captured_at` and current UTC time.

`/calculate` accepts competitors, wood, event code, and optional exclusive
`prediction_as_of`. Results include the predicted time, mark, method, forecast interval,
performance `std_dev`, versions, optimizer metadata, warnings, and degraded state.

`/ledger/calculate` adds `request_id` and requires every `competitor_id`. Identical
retries return original prediction IDs; changed inputs or deterministic prediction
outputs under the same key return 409. Settlement verifies prediction/competitor/event, deduplicates exact retries, and
requires a reason for corrections.

`/simulate` defaults to and caps at 250,000 races, with a 4,000,000
competitor-by-simulation cell limit and one concurrent simulation per process. It is a
post-mark audit, separate from the optimizer's fixed 2,048 samples.

The canonical [Shadow Consumer Contract](../SHADOW_CONSUMER_CONTRACT.md) freezes the
route schemas, non-200 response matrix, roles, limits, examples, and checksum. See the
repository's `STRATHMARK API.txt` for legacy request/response details and [Prediction
Engine V2](Prediction-Engine-V2) for numeric semantics.
