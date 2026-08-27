# STRATHEX Competition-Scoped Engine Consumer Contract

## Current status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. STRATHMARK repository implementation and audit are complete for this
candidate. No installed-consumer rehearsal exists until STRATHEX implements the durable
adapter described below; the STRATHMARK development-key rehearsal is source-bound. V2
remains the trusted production-capable engine. V3 is not production-eligible. The target
workflow deliberately selects one eligible engine per single-event or tournament root;
it is not a global V2-to-V3 replacement. No production authority has changed, no
STRATHEX endpoint has switched, and V2 is not audit-only.

STRATHMARK now exposes the typed batch-approval prerequisite. STRATHEX's durable outbox
forwarder and immutable local acknowledgment persistence are not yet implemented; this
document specifies that remaining external work.

This document is a cross-repository consumer contract, not authorization to modify a
live tournament or to manufacture V3 production readiness.

## Why this is a contract replacement

V2 exposes one selected field forecast through its legacy result shape and the frozen
six-route `/v1/shadow/*` boundary. V3 preserves formula, ML, and LLM component forecasts,
capability state, accuracy-earned weights, disagreement consequences, optimizer frontier,
approval state, issue acknowledgment, and settlement authority. Compressing that evidence
into V2's five keys would make the audit record false.

STRATHEX must therefore consume the separate V3 OpenAPI contract and receipt identities.
V2 receipts stay replayable and are never rewritten.

## Authority split

| STRATHEX / tournament manager | STRATHMARK V3 |
| --- | --- |
| human login and RBAC | one authenticated service-principal boundary |
| roster, schedule, round closure | evidence epoch and field derivation |
| judge approval permissions | green/amber/red numeric review projection |
| official issue and stand sheet publication | immutable receipt and issue acknowledgment |
| official result, placing, protest, payout | complete-roster atomic numeric settlement evidence |
| UI, mass approval, flagged review | bounded API, audit metadata, exact retries |

Once STRATHEX has authenticated as the service principal, STRATHMARK does not authorize
the human named by `X-STRATHMARK-Upstream-Actor`. Actor, action, and trace headers are
audit metadata only. STRATHEX must enforce its own roles before calling V3.

## Selection and inheritance contract

STRATHEX creates one immutable competition-root selection. A standalone event owns its
choice. A tournament owns one choice during creation, and every child event, heat, later
round, bracket forecast, and supported championship forecast inherits it. A tournament
child never exposes or accepts another selector.

The selection records the root identity, requested engine, execution mode, judge actor
metadata, time, reason, exact consumer-contract digest, and source commit. The first
authoritative numeric action locks it. For V3, the tournament-open event is lock evidence
and contains the same closed selection fact. A V2-selected root cannot enter the V3
lifecycle. Different roots may select different eligible engines concurrently without
sharing receipts or state.

Before lock, a correction is a new deliberate, reasoned selection record. After lock, an
engine never changes in place. Failure or unavailability blocks numeric progress; it does
not call the unselected engine. Later units define terminal abandonment for locked,
unissued scopes and the creation of a new scope identity.

## Frozen dependency

Pin all of the following to one authorized source release:

- the exact STRATHMARK commit;
- the installed wheel digest;
- `strathmark/v3/contracts/v3_consumer.openapi.json`;
- `strathmark/v3/contracts/v3_consumer.openapi.sha256`;
- the V3 dependency lock; and
- the production release-attestation digest when one exists.

The current OpenAPI SHA-256 is
`5dfd135a0ea18316753b0107e50bf0cdd811bb0ec042b6b57206fb2022708fc3`.
Consumers must verify the file bytes rather than copy this prose value alone.

## V3 route mapping

| Consumer action | V3 route |
| --- | --- |
| process liveness | `GET /v3/health` |
| trusted authority/readiness state | `GET /v3/status` |
| prepare rolling competitor context | `POST /v3/cards/prepare` |
| record exact selected/excluded receipt approvals | `POST /v3/approvals/decide` |
| assemble/recover complete field | `POST /v3/fields/assemble` |
| recover immutable receipt | `POST /v3/receipts/lookup` |
| bind official upstream issue to receipts | `POST /v3/issues/acknowledge` |
| settle complete field outcome | `POST /v3/results/settle` |
| rotate service credential | `POST /v3/credentials/rotate` |
| revoke service credential | `POST /v3/credentials/revoke` |

Every trusted POST uses a bearer service credential and `Idempotency-Key`. Exact retries
must reuse the same canonical request. A changed request under the same key is a conflict.
Unknown fields, duplicate singleton headers, stale expected versions, and illegal state
transitions fail closed.

Internal `CommandKind` values are not a public mutation surface. Issue, settlement,
epoch, field-roster, model-evaluation, and other authority-changing work remains behind
typed application services and the dedicated routes present in the frozen contract.

## Required STRATHEX workflow

### Before a tournament

1. Verify the pinned contract and installed artifact digests.
2. Authenticate the service and check `/v3/status` plus operation-specific readiness.
3. Confirm local durable authority, current backup/recovery state, active bundle, epoch,
   weights, queue health, and assessor availability.
4. Send plausible future contexts for rolling card preparation.
5. Require a deliberate eligible engine choice at the competition root; do not default
   or silently fall back to another adapter.

### During a round

1. Open/freeze one evidence epoch for the round.
2. Prepare every field against that same epoch.
3. Do not feed an early heat's result into a later heat in the same round.
4. When the final roster is known, assemble the complete field. Never copy displayed marks
   from qualifying heats.
5. Render the approval projection exception-first: green rows compact and batchable;
   normal amber rows flagged but batch-eligible, and red rows expanded with assessor and
   counterfactual consequences.
6. Require the judge to deliberately choose each flagged action. There is no default
   manual estimate.
7. Persist the human decision and a durable outbox row in one STRATHEX transaction.
   Forward the exact approval snapshot, selected and excluded receipt bindings, actor
   metadata, timestamp, and idempotency identity to `/v3/approvals/decide`. Store the
   returned immutable acknowledgment before considering the outbox item delivered.
8. Separately acknowledge the complete issued receipt set atomically before publication.

### After the race

1. Preserve the first legal completion and upstream official result.
2. Submit every completion plus explicit DNF/DQ/DNS/void/penalty state field-atomically.
3. Reuse the original issue/receipt identities and increment only legal source revisions.
4. Close the round deliberately, then advance the epoch so all valid performances can
   inform the next round.
5. Never request recalculation of an issued sheet or an adjusted placing.

## UI contract for the live cadence

The operator flow must fit a heat every ten minutes and a possible five-minute final
turnaround. STRATHEX should display:

- one compact row per field with state, SLA, receipt revision, epoch, and issue status;
- one mass-approve action for currently eligible green sheets;
- normal amber sheets flagged but batch-eligible, and red sheets singled out with exact
  reason codes and counterfactual mark impact;
- stale/superseded work excluded from mass action;
- a deliberate action and reason requirement for every exception;
- an immutable issued state; and
- the selected authority and a visible blocked/recovery state if it cannot serve trusted
  numeric work.

An LLM-generated narrative must never decide the color, authorization, mark, or official
result. The displayed explanation is derived from signed structured evidence.

## Error and retry behavior

| Condition | Consumer response |
| --- | --- |
| timeout/ambiguous response | use receipt lookup or exact idempotent retry; do not create a new identity |
| `409 authority_conflict` | refresh authoritative versions/state and require deliberate reconciliation |
| `503 request_capacity_exhausted` | retain current sheet/state and retry within bounded cadence policy |
| `504 operation_deadline_exceeded` | recover by identity; never assume failure or success |
| stale card or roster revision | regenerate the complete pre-issue field |
| issue batch contains one stale member | treat the entire batch as unissued |
| post-issue scratch/non-start | preserve the issued sheet and record the legal non-start/result state |
| assessor unavailable | show abstention and surviving evidence; never relabel a fallback |
| selected V3 becomes unavailable | block new numeric work and recover exact V3 state; never invoke V2 automatically |

## Adapter rehearsal

The installed STRATHEX adapter must run against an isolated V3 database and the exact
frozen contract. The rehearsal covers prepare, field assembly, approval projection,
issue, restart lookup, result settlement, same-round epoch isolation, next-round update,
stale/corrected requests, credential rotation/revocation, and all documented error codes.
Its digest must match the V3 initialization snapshot used for cutover preparation.

Passing the adapter rehearsal changes no authority.

The current repository provides the STRATHMARK endpoint and contract tests only. The
external STRATHEX outbox implementation, dependency pin, and end-to-end acknowledgment
replay remain required before this rehearsal can pass.

## V3 production-eligibility boundary

STRATHEX may offer V3 as a production selection only after it receives and verifies a
production-CNG-signed handoff that says:

```text
current_authority=v2
next_authority=v3
endpoint_switched=false
requires_explicit_release_authorization=true
```

That handoff proves readiness to ask for production eligibility. It is not a competition
selection. After separate release authorization, enable the pinned V3 contract as an
eligible choice. Keep V2 available for separately selected roots. Never allow V2 and V3
trusted writes inside the same competition scope.

If eligibility preparation fails, V3 remains unavailable. If selected V3 fails after a
scope locks, recover that V3 scope or follow its explicit terminal workflow; do not
reinterpret the scope as V2.

## Preserved V2 integration

For every scope that deliberately selects V2, STRATHEX continues to follow
[`SHADOW_CONSUMER_CONTRACT.md`](SHADOW_CONSUMER_CONTRACT.md). V2's exclusive date cutoff,
numeric-LLM retirement, residual-only ML, five keys, and shadow routes remain exact for
that adapter. They must not be generalized into V3 behavior.
