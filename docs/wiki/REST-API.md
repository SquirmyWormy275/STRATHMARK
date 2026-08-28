# REST API

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and must pass the release verifier.
V2 remains the globally trusted production authority. V3 is not production-eligible,
and V2 is not audit-only.

V2 public, ledger, and /v1/shadow routes remain the production contract. V3 exposes the
separate frozen V6, 18-path `/v3/*` contract. Do not mix their request/receipt identities
or interpret the presence of a V3 route as production eligibility or competition
selection.

V3 routes cover health/status; competition open/close; snapshot synchronization; round
freeze/close; rolling card preparation; pre-field forecast; field assembly; approval
page/detail/decision; receipt lookup; issue acknowledgment; result settlement; and
credential rotation/revocation. All trusted POSTs
require one bearer service credential and an Idempotency-Key. Loopback is default.
Non-loopback operation also requires pinned mutual TLS. Upstream actor/action/trace
headers are audit metadata, never RBAC.

`POST /v3/approvals/decide` binds one approval snapshot to multiple exact selected and
excluded receipt ID/digest/revision/row bindings, upstream field revisions, decision
time, and actor metadata. It records projection authority atomically and returns an
immutable acknowledgment. It does not implement human RBAC and does not replace
`POST /v3/issues/acknowledge`.

`POST /v3/forecasts/pre-field` is field-independent. It returns a signed forecast set
for seeding/grouping with `purpose=pre_field_seeding_only` and `issued_mark=false`. It
accepts no fabricated field or stand identity. Only `POST /v3/fields/assemble`, after
exact field synchronization, produces mark-bearing receipts.

Internal command kinds use typed application services. The frozen consumer contract does
not advertise a generic event-mutation route.

The canonical examples and schemas live in the
[frozen OpenAPI document](../../strathmark/v3/contracts/v3_consumer.openapi.json), with
its [SHA-256](../../strathmark/v3/contracts/v3_consumer.openapi.sha256) verified as exact
bytes. See [Prediction Engine V3](../PREDICTION_ENGINE_V3.md) for the route table and
[STRATHEX consumer migration](../STRATHEX_CONSUMER_MIGRATION.md) for workflow and retries.
