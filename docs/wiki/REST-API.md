# REST API

Under the Windows CNG qualification policy, V3 is not production-eligible. Its release candidate rehearsal evidence is source-bound and does not grant Windows production authority. V2 remains the established production baseline; the completed Linux local profile uses its separate operator policy.

The retained preview profile, [Linux numeric candidate](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_CANDIDATE.md) separately runs real Formula/ML numeric previews through STRATHEX 7.2. It uses a subprocess contract, without V7 approval, issue, settlement, next-round learning, or production authority. The full authenticated V7 lifecycle requires operational composition and installation qualification.

## Current authority status

STRATHMARK 3.0.0rc7 provides the complete separate Linux local competition profile: real Formula/ML, deliberate V2/V3 choice, exact fields, approval, separate issue, judge-authorized outcomes, settlement, restart, corrections, verified independent recovery, and later-round learning. The [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md) describes its operator policy and persistent local signer. The LLM council is unavailable in this profile. V2 remains the established production baseline. Windows V7 CNG and full three-assessor factory qualification remain separate installation gates; ephemeral rehearsal signatures do not satisfy them.

V2 public, ledger, and /v1/shadow routes remain the production contract. V3 exposes the
separate frozen V7, 18-path `/v3/*` contract. Do not mix their request/receipt identities
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
[frozen OpenAPI document](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.json), with
its [SHA-256](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.sha256) verified as exact
bytes. See [Prediction Engine V3](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V3.md) for the route table and
[STRATHEX consumer migration](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/STRATHEX_CONSUMER_MIGRATION.md) for workflow and retries.
