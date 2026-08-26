# REST API

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and must pass the release verifier. V2 remains the trusted production
authority until an explicit cutover. No production authority has changed, no consumer
endpoint has switched, and V2 is not audit-only.
The external STRATHEX durable outbox/adapter is not implemented.

V2 public, ledger, and /v1/shadow routes remain the production contract. V3 exposes a
separate frozen ten-route /v3 contract. Do not mix their request/receipt identities or
interpret the presence of a V3 route as consumer cutover.

V3 routes cover health, status, rolling card preparation, field assembly, receipt lookup,
typed batch approval, issue
acknowledgment, result settlement, and credential rotation/revocation. All trusted POSTs
require one bearer service credential and an Idempotency-Key. Loopback is default.
Non-loopback operation also requires pinned mutual TLS. Upstream actor/action/trace
headers are audit metadata, never RBAC.

`POST /v3/approvals/decide` binds one approval snapshot to multiple exact selected and
excluded receipt ID/digest/revision/row bindings, upstream field revisions, decision
time, and actor metadata. It records projection authority atomically and returns an
immutable acknowledgment. It does not implement human RBAC and does not replace
`POST /v3/issues/acknowledge`.

Internal command kinds use typed application services. The frozen consumer contract does
not advertise a generic event-mutation route.

The canonical examples and schemas live in the
[frozen OpenAPI document](../../strathmark/v3/contracts/v3_consumer.openapi.json), with
its [SHA-256](../../strathmark/v3/contracts/v3_consumer.openapi.sha256) verified as exact
bytes. See [Prediction Engine V3](../PREDICTION_ENGINE_V3.md) for the route table and
[STRATHEX consumer migration](../STRATHEX_CONSUMER_MIGRATION.md) for workflow and retries.
