# STRATHMARK Architecture

## Status and authority

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and does not change authority. V2
remains the globally trusted production authority; V3 is not production-eligible. No
production authority has changed, and V2 remains runnable and writable.

The two engines are deliberately separate:

| Boundary | V2 | V3 |
| --- | --- | --- |
| Namespace | `strathmark.*` | `strathmark.v3.*` |
| Numeric design | one prior-only core plus optional residual | blind formula + ML + LLM council ensemble |
| Evidence boundary | exclusive historical date | historical cutoff plus tournament/round epoch |
| Persistence | V2 ledger and shadow contract | V3 event authority and rebuildable projections |
| Consumer contract | public/ledger and six `/v1/shadow/*` routes | frozen V6, 18-path `/v3/*` contract |
| Authority now | trusted production | audited release candidate, rehearsal only |

V3 does not mutate V2 receipts or project itself through V2's five keys. Eligibility is
an installation fact; numeric authority is selected once per competition root. A
standalone event owns its selection, while a tournament's events and rounds inherit the
tournament selection. Separate roots may select different eligible engines. One root
never changes engines in place and never uses request-by-request fallback.

### Documented pivot: global switch to scoped selection

The earlier design assumed one global V2-to-V3 consumer switch. The current architecture
keeps V2 and V3 as explicit choices so real V3 feedback can be gathered without making
every competition a V3 competition. The root selection is immutable at the first
numeric action, carries actor/time/reason and exact contract/source identity, and is
repeated in every consequential V3 receipt. This preserves one auditable numeric
authority from setup through finals while allowing another competition root to choose
the other eligible engine.

## V3 data flow

```mermaid
flowchart LR
    A["Verified history + tournament result events"] --> B["Cutoff + frozen round epoch"]
    B --> C["Canonical pseudonymous evidence packets"]
    C --> D["Transparent formula"]
    C --> E["Universal + specialist ML"]
    C --> F["Blind 3-member LLM council"]
    D --> G["Deterministic validation"]
    E --> G
    F --> G
    G --> H["Dual-state capability"]
    G --> I["Predictive + consequence credibility"]
    H --> J["Weighted linear distribution pool"]
    I --> J
    J --> K["Consequence disagreement gate"]
    K --> L["Fairness-frontier optimizer + Mark-3 rebase"]
    L --> M["Immutable field receipt"]
    M --> N["Green/amber/red approval projection"]
    N --> O["Typed multi-receipt approval decision"]
    O --> P["Atomic issue acknowledgment"]
    P --> Q["Complete-roster atomic settlement"]
    Q --> R["Seven durable derivation reactions"]
    R --> A
```

The circular edge does not mean a result can change its own race. All fields in a round
share one epoch. Settled results become input only after the legal boundary to the next
round. Capability, invalidation, scoring, coverage, weights, readiness, and credibility
must all close before the derivation barrier opens; none inserts a judge decision.
The approval-decision event records the tournament manager's selected and excluded
receipt bindings. It neither authorizes a human nor acknowledges official issue.

Before the exact field exists, the same frozen round authority can produce a signed
pre-field forecast set. It contains an ordered roster, marginal raw-time distributions,
and p50 seed times, but explicitly records `purpose=pre_field_seeding_only` and
`issued_mark=false`. It has no field identity, stand assignment, joint field optimization,
or displayed mark. After STRATHEX creates and synchronizes the exact field, field
assembly combines compatible cards jointly, optimizes the actual race, rebases it to
Mark 3, and produces the only receipt from which V3 marks may be reviewed or issued.

## Layers

### Contracts

`strathmark/v3/contracts/` owns closed dataclasses, identifiers, event and forecast
vocabularies, receipt schemas, canonical JSON, numeric bounds, and error types. Canonical
bytes and digests are part of the authority protocol, not serialization convenience.

### Domain

`strathmark/v3/domain/` contains deterministic evidence folding, capability state,
credibility, pooling, joint dependence, disagreement, state machines, and the optimizer.
It performs no environment reads, storage, provider I/O, or background work.

### Application

`strathmark/v3/application/` coordinates expected-version commands, rolling cards,
field assembly, approvals, issue, settlement, lifecycle, automated factory work,
operational status, recovery proof, and eligibility preparation. Ports make every external
effect explicit.

`application/factory_runtime.py` is the runnable local composition and scheduler for
factory automation plus settled-evidence monitoring. It fails closed unless every
formula/ML/LLM family executor is explicitly injected at the local-configured boundary.
The repository also provides a bounded canonical file exchange and the installed
`strathmark-v3-factory-evaluator` command, which opens an existing non-exportable Windows
CNG key by name for one evaluator request. The repository script delegates to that same
packaged entry point. These seams do not provide the concrete family
executors, local settlement-metric evaluator, installation OS identities/ACLs, or CNG
provisioning required for production qualification.

### Infrastructure

`strathmark/v3/infrastructure/` implements the local SQLite event authority, projections,
durable jobs, outbox, content-addressed blobs, artifact integrity, backup/restore, Ollama,
cloud, V2 snapshot import, and rolling-head recovery. Optional provider or archive failure
cannot become local numeric authority.

### API and composition

`strathmark/v3/api/` exposes the injected FastAPI service. A pre-body boundary rejects
invalid transport, duplicate singleton headers, unauthenticated trusted requests,
oversized bodies, and capacity exhaustion before application work. Loopback is default;
non-loopback requires pinned mutual TLS. The OpenAPI document and SHA-256 are frozen
installed artifacts.

`strathmark/v3/composition.py` is the only V3 environment reader. It produces one
immutable configuration snapshot and performs no I/O. Production ML and release
authority require installation-owned non-exportable Windows CNG identities; tests use
explicit ephemeral adapters that cannot pass production verification.

## Event authority and concurrency

Every behavior-changing request has a stable command identity, canonical payload digest,
idempotency key, target aggregate, and sorted expected-version map. The SQLite writer
validates the legal transition, appends consecutive aggregate/global events, and advances
required projections in one short transaction.

- Exact retry returns the original bytes.
- Same key with changed payload conflicts.
- Stale versions and illegal transitions fail closed.
- Multi-field issue either commits all fields or none.
- Issued receipts cannot be mutated.
- Projection views may be deleted and rebuilt from the event log.
- Durable jobs use leases, heartbeats, bounded retries, and reconciliation after
  ambiguous external work.

Large packets, forecasts, model bundles, and support data live in content-addressed blob
storage. Events carry their verified digests. The optional mirror/archive consumes an
outbox and never owns race-day authority.

## Rolling preparation and live field assembly

As scheduling information arrives, V3 prepares per-competitor context cards and seals
their assessor forecasts before the final roster exists. The field assembler combines
current compatible cards, validates epoch/evidence/bundle/roster revisions, pools one
joint distribution, calculates disagreement and marks, and commits the receipt.

This separates slow speculative inference from the critical call-up path. The
post-format, five-run designated-Windows result-to-ready benchmark measured a maximum of
3.414 seconds against the 120-second limit while preserving exact-source digests,
component latency, exact-retry recovery, and immutable issuance.

Pre-field forecasting fills the intentional gap between those stages. It lets the
tournament manager seed or group competitors before exact fields and stands exist,
without fabricating field facts or prematurely issuing marks. Its durable rolling-card
authority is scoped by competitor, context, epoch, and bundle so concurrent competition
roots cannot supersede one another's prepared state.

## Failure and readiness model

Readiness is dependency-specific. Field assembly, issue, receipt lookup, result
settlement, recovery, and support export have different dependency graphs; one aggregate
green light cannot hide a red critical path. Status records active bundle and epoch,
weights, queues, oldest job, assessor availability, model warmth, event tip,
projection/backup health, and SLA risk.

Recovery exercises process, machine, worker, Ollama, cloud, power, WAL, blob, disk, and
queue faults. If trusted numeric service cannot be restored, authority is stated as
traditional/manual. Silent partial state and automatic V2/V3 dual authority are forbidden.

## Official-system boundary

STRATHMARK owns numeric prediction and receipt evidence. The tournament manager owns
human authentication/RBAC, roster and schedule authority, official issue, judging,
results, publication, points, protests, and payouts. Actor/action/trace headers entering
V3 are audit metadata only.

For full behavior, use [`PREDICTION_ENGINE_V3.md`](PREDICTION_ENGINE_V3.md). For current
operations and eligibility, use [`DEPLOYMENT.md`](DEPLOYMENT.md). V2 architecture remains in
[`PREDICTION_ENGINE_V2.md`](PREDICTION_ENGINE_V2.md).
