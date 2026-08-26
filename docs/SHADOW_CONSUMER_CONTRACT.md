# Trusted Shadow Consumer Contract

> **Frozen V2 consumer contract.** The V3 release candidate has a separate `/v3/*` contract
> and its older rehearsal evidence is stale after current source changes. V2 remains the trusted production
> authority until an explicit cutover. No V3 route, artifact, or rehearsal changes this
> shadow contract or switches a consumer.

The frozen local consumer contract is packaged at
`strathmark/contracts/shadow_consumer_v1.openapi.json`. It is an OpenAPI 3.1
document covering the complete versioned STRATHMARK boundary consumed by
Missoula: service health, calculate/recover, receipt lookup, current status,
numeric settlement/void, bounded mirror replay, and advisory drift.

The sibling `.sha256` file pins the canonical JSON representation. Python
consumers should use `load_shadow_consumer_contract()` and
`shadow_consumer_contract_digest()` instead of reading package paths directly.
Both functions fail closed if the installed document is malformed, has the wrong
contract version, exposes a different route set, or no longer matches the reviewed
digest. The current reviewed SHA-256 is
`8b0a11a6613c74ad7a5e01f3fe99d6bbede8b94dc7cdffe27930b5d0193d90db`.

`scripts/freeze_shadow_consumer_contract.py` deterministically regenerates the
canonical artifact and checksum. The response contract is closed at every fixed
object boundary, including receipt cores, predictions, evidence, ledger state,
numeric revisions, drift, and health. Only explicitly keyed diagnostic and drift
cohort maps accept dynamic keys, and their values remain schema constrained.

The live `/openapi.json` and `/docs` surfaces copy these seven operations and all
of their referenced components directly from that checksum-verified artifact.
FastAPI therefore documents the required security pair, strict request and 200
response schemas, and the complete closed non-200 matrix exactly as reviewed.
The one component-name collision with the legacy `/calculate` route is renamed
inside the generated legacy documentation before the frozen components are added,
so neither contract is weakened.

The request boundary matches the live service limits: wood diameter is 225 through
500 millimeters inclusive, numeric actual time is positive and no greater than 300
seconds, and every baseline drift residual is between -300 and 300 seconds
inclusive. Trusted numeric fields use strict runtime validation: JSON numeric
strings and booleans are rejected rather than coerced into numbers or integers.

Every protected operation requires `strathmark.actor-attestation.v2`. Its decoded
claims are closed and bind the consumer, originating actor, role snapshot, exact
action, subject `run_revision`, audience, lifetime, and nonce to a required
`strathmark.shadow-request-digest.v1` digest. The digest is SHA-256 over canonical
JSON containing that schema version and the validated request fields actually
provided. Object keys are sorted, compact JSON separators and ASCII escaping are
used, and integral JSON numbers normalize to integers. Consumers should call
`canonical_shadow_request_digest()` and `sign_actor_attestation()` rather than
reimplementing these rules. The server compares the request digest before claiming
the nonce, so a modified body cannot consume a valid assertion. Bearer credentials
and attestation signing keys must be unique within their maps and disjoint across
the two maps; crossed secret material makes authentication configuration invalid.

The frozen operation extensions record the required action, subject revision
field, and request-digest schema for every protected route. The allowed role
matrix is also frozen: judges and administrators may perform normal operator
actions, administrators alone may replay the mirror, and the bounded
`system-adapter` role may only apply numeric outcomes. A direct `scorer` role is
not accepted. Adapter submissions retain the attested originating actor as the
immutable outcome actor.

## Authority and privacy

- STRATHMARK owns numeric predictions, immutable receipt cores, live numeric
  settlement/void revisions, and the payload-free monitoring projection.
- Missoula owns preparation, human authorization, lifecycle approval, complete
  operational outcomes, prospective observations, official results, standings,
  points, publication, and payouts.
- The examples use pseudonymous namespaced identifiers. Display names, contact
  data, medical information, and free-text observations are outside this contract.
- Cloud mirroring is optional and off the trust path. `not-configured` and pending
  delivery do not make a locally recorded receipt untrusted.

## Consumer sequence

1. Refresh and attest a verified local evidence snapshot for the explicit UTC
   cutoff.
2. Prepare one ordered whole-field request with stable namespaced identities.
3. Look up the receipt before calculating; calculate only when lookup returns 404.
4. Persist the returned `core_json` byte-for-byte. Treat `status` as a live
   projection, not part of the immutable core.
5. Review or issue only when trust is `recorded`, freshness is `current`, and
   readiness is true. Official championship authority remains unchanged.
6. Submit only eligible positive raw elapsed times, or explicit void revisions,
   using optimistic numeric revision numbers and stable Missoula outcome IDs.
7. After an ambiguous calculation timeout, look up the exact receipt. After an
   ambiguous numeric-outcome timeout, retry only the identical payload with the
   same `outcome_revision_id`; a committed write returns its duplicate result.
   Never repeat a correction under a new identity.

Exact retries survive process restart and artifact upgrades because receipt lookup
precedes provider loading. A new request/run revision whose only material change is
the prospective observation fingerprint produces a distinct receipt while retaining
the same active numeric input fingerprint, calculation input, and numeric output.
Reusing the original caller/request/run identity with a changed observation remains
an idempotency conflict and returns HTTP 409.

Receipt lookup and status always derive current freshness from the durable local
`ResultStore`; omitting `current_active_fingerprint` never asserts current state.
The optional caller fingerprint is only an additional mismatch guard: when it
differs from the recorded active fingerprint the response is stale and not ready,
even if the server-side evidence snapshot would otherwise be current. Calculate
also re-derives this status after its atomic write and reload, closing the window
where a concurrent evidence refresh could return a newly persisted receipt as
review-ready.

New calculate and numeric-outcome writes fail with HTTP 503 unless authentication,
an explicitly attested single-writer durable topology, and persistent local ledger
are all truthful. Calculate additionally requires current verified local evidence.
Receipt-bound settlement and void remain available after event completion when the
current evidence snapshot is stale or missing. These readiness checks run before
the one-use actor nonce is claimed, so the same assertion can be retried after an
operator restores the prerequisite. Receipt lookup, status, and mirror replay
remain available for recovery while write readiness is degraded.

Public health never performs the full snapshot/activation scan and never reserves the
ledger writer. It exposes only an in-process verified evidence attestation, with
freshness recomputed from current UTC time. Restart, cache miss, or an observed evidence
database-file change therefore reports unavailable/not-ready until an authenticated
bounded workflow verifies the snapshot again. Ledger persistence readiness likewise
uses the cached successful initialization observation plus current file identity and
permissions; it does not prove infrastructure durability.

Numeric request rows are strict discriminated variants: `settle` requires a
positive bounded `actual_time`; `void` permits only null/omitted `actual_time`.
Every void and every correction (`expected_revision > 0`) requires an explicit
reason code before the ledger can be called. Receipt lookup and numeric outcome
writes accept 25-10,000 ms deadlines. A write timeout is deliberately ambiguous
because the atomic commit may already exist; the operation keeps its critical slot
until the worker actually stops and the exact stable outcome ID is the recovery
key. A calculation timeout likewise keeps its slot until background completion
because cancelling after partial calculation or persistence would be unsafe.
Calculate and numeric outcome share a bounded critical executor. Receipt lookup
and status have a separate bounded recovery executor, while mirror replay and
advisory drift use a third bounded maintenance executor. Blocked calculations or
maintenance therefore cannot consume receipt-recovery capacity; saturation is
reported as workload-specific HTTP 429 responses.

The in-memory mirror queue, each automatic pending scan, and each explicit replay
are bounded. The durable append-only outbox is intentionally not hard-capped in
this release: dropping an undelivered row or blocking a local trusted receipt would
violate the local-authority and nonblocking-mirror requirements, and no reviewed
archive/compaction protocol exists yet. A permanently unavailable mirror is still
reported as `retryable-failed`, not the planned `permanent-failed` state. Operators
must monitor backlog counts/oldest age and provision disk or disable mirroring.
This is a known R13/R37 residual; it does not weaken local receipt trust, but a
finite durable-capacity and permanent-failure policy remains required before the
outbox can be represented as fully bounded.
Explicit replay is fair within each requested scope: never-attempted rows are
selected first, followed by the oldest prior attempt, with stable creation and
outbox identities breaking ties. A repeatedly failing oldest row therefore cannot
starve newer durable work when operators replay one row at a time.

All documented non-200 responses use the closed payload-free `ErrorResponse`
shape, `{ "detail": "..." }`. Live validation errors are normalized to the same
shape, and the executable matrix checks every protected route plus `/health`.

## Offline rehearsal

`tests/test_shadow_consumer_contract.py` is the executable reference rehearsal. It
removes ambient database/cloud variables, uses a temporary SQLite file and an
in-process verified evidence adapter, then exercises calculate, context invariance,
restart recovery under an intentionally unusable upgraded provider, lookup, live
status, settle, void, health, mirror replay, and advisory drift. The same test
validates every packaged request/response example against its JSON Schema and the
live Pydantic request models. It also validates actual responses from all seven
routes, injects unreviewed fields at deep response boundaries to prove they are
rejected, and exercises adversarial values immediately inside and outside each
numeric boundary.

The CI-installed distribution path, `scripts/smoke_installed_distribution.py`,
loads the contract from the installed package, verifies its checksum, and then
executes an authenticated offline calculate plus receipt-lookup lifecycle against
temporary SQLite evidence and ledger state using the v2 request-digest actor
attestation. It runs outside the checkout for both wheel and source distribution
jobs. Its `--offline` option supports the same check without package index access
when dependencies are already available locally.

This proves the local contract only. It does not prove production durability,
hosted Supabase behavior, deployment, secrets, or official-handicap authority.
