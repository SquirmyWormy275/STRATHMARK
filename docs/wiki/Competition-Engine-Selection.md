# Competition Engine Selection

## Current authority status

V2 remains STRATHMARK's globally trusted production authority. V3.0.0rc1 is a separate
release candidate and is not production-eligible. A development-key rehearsal, V6 route,
or selection record does not change that status.

## The pivot

The earlier deployment design assumed one global V2-to-V3 consumer switch. The current
design keeps both engines explicit so judges can test an eligible V3 on deliberately
chosen competitions, compare real operating feedback, and continue choosing V2
elsewhere. Production eligibility enables an engine as a choice; it does not select that
engine for a competition.

This pivot changes the authority workflow, not the internal numeric definition of V2 or
V3. Historical V2 receipts and dated V3 planning documents remain evidence of their
versions and decisions.

## Selection rules

- A standalone event is a competition root and selects one engine during event setup.
- A tournament selects one engine during tournament creation.
- Every child event, heat, quarter-final, semi-final, divisional final, and grand final
  inherits the tournament selection.
- Child units do not expose or accept another engine selector.
- There is no default. The judge or upstream authority must deliberately choose.
- The first authoritative numeric action locks the selection.
- Different competition roots may use different eligible engines concurrently.
- One root never mixes V2 and V3 receipts, changes engine in place, or invokes the other
  engine as a silent fallback.

The immutable selection records the root ID, engine, execution mode, selecting actor,
selection time, reason, exact V6 consumer-contract digest, and exact STRATHMARK source
commit. V3 scope-open authority and consequential receipts repeat that binding so the
choice can be audited after restart.

An unavailable selected engine blocks new numeric work for that scope. Recovery uses the
same engine's durable authority. A later competition may deliberately select another
eligible engine, but that does not reinterpret or repair the original scope.

## Prediction before fields exist

Tournament seeding happens before exact fields and stand assignments may exist. V6 adds
`POST /v3/forecasts/pre-field` for that stage. The request binds an ordered competitor
set, target event/material context, frozen round epoch, forecast-set revision, and hard
deadline. It does not require or invent a field or stand identity.

The signed response contains marginal raw-time distributions and p50 seed times. Its
authority is intentionally limited:

```text
purpose = pre_field_seeding_only
issued_mark = false
```

These values may support sorting, seeding, or grouping. They are not marks, a start
sheet, an approval candidate, or official issue evidence. A p50 seed time must never be
printed in the mark column.

## Marks require the exact field

Once STRATHEX creates the actual field and stand assignments, it synchronizes those
versioned facts and calls `POST /v3/fields/assemble`. Field assembly validates the
competition selection, round epoch, evidence, bundle, roster order, and revisions. It
then pools the compatible competitor cards jointly, evaluates disagreement and the
fairness frontier, and rebases the complete field to Mark 3.

Only that exact-field receipt can carry displayed V3 marks into judge review and issue.
Marks from independently rebased heats are never copied into a later field.

## V6 contract boundary

The frozen contract is `strathmark.v3-consumer-contract.v6` with 18 paths. Consumers
must verify the installed OpenAPI bytes, sibling SHA-256, exact source commit, and service
status. STRATHMARK authenticates the upstream service; human RBAC, the selection UI,
official issue, judging, publication, and payouts remain tournament-manager authority.

See [STRATHEX Consumer](STRATHEX-Consumer.md), [REST API](REST-API.md), and
[Deployment](Deployment.md) for integration, retry, and eligibility requirements.
