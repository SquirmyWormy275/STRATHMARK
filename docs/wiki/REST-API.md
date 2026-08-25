# REST API

## Current authority status

V3 is an implemented release candidate under exact-source verification. Its older
rehearsal is stale after current source changes. V2 remains the trusted production
authority until an explicit cutover. No production authority has changed, no consumer
endpoint has switched, and V2 is not audit-only.

V2 public, ledger, and /v1/shadow routes remain the production contract. V3 exposes a
separate frozen ten-route /v3 contract. Do not mix their request/receipt identities or
interpret the presence of a V3 route as consumer cutover.

V3 routes cover health, status, rolling card preparation, enumerated single-event
expected-version commands, field assembly, receipt lookup, issue acknowledgment, result settlement, and
credential rotation/revocation. All trusted POSTs require one bearer service credential
and an Idempotency-Key. Loopback is default. Non-loopback operation also requires pinned
mutual TLS. Upstream actor/action/trace headers are audit metadata, never RBAC.

`OnlineCommandKind` in the frozen schema is the generic route's exact public allow-list.
Internal or multi-event command kinds use their typed application services; they are not
advertised as generic commands that would only fail after transport validation.

The canonical examples and schemas live in the
[frozen OpenAPI document](../../strathmark/v3/contracts/v3_consumer.openapi.json), with
its [SHA-256](../../strathmark/v3/contracts/v3_consumer.openapi.sha256) verified as exact
bytes. See [Prediction Engine V3](../PREDICTION_ENGINE_V3.md) for the route table and
[STRATHEX consumer migration](../STRATHEX_CONSUMER_MIGRATION.md) for workflow and retries.
