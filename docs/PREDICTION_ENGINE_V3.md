# Prediction Engine V3

## Release and authority status

V3.0.0rc1 is a release candidate in the `strathmark.v3` namespace that
tracks all 232 requirements in the in-repository V3 plan. Implementation is under final
audit. The older checked-in rehearsal attestation is stale and must be regenerated from
the final documentation commit. A fresh rehearsal, when generated, is signed by an
ephemeral development key. V2 remains the trusted production authority until an
explicit cutover. No production authority has changed, no consumer endpoint has
switched, and V2 is not audit-only.

This distinction is intentional. A production release requires a live, non-exportable
Windows CNG signing identity, an exact production evidence set, a zero-open-tournament
V2 freeze, an initialized V3 installation, an isolated consumer rehearsal, and separate
release authorization. The rehearsal artifact cannot satisfy those gates.
No production CNG identity is currently provisioned.

Read [`wiki/Handicap-Mark-Math.md`](wiki/Handicap-Mark-Math.md) first. It controls domain
meaning. This page controls the V3 release-candidate mechanism under final audit.

## Why V3 exists

V2 correctly replaced an unsafe legacy cascade with one reproducible prior-only model.
That design could not implement the later-settled product contract: independent formula,
ML, and LLM forecasts must all be generated and compared; every valid completion must
inform later rounds; every field must be rehandicapped and rebased; component accuracy
must earn influence; and the judge must receive an auditable exception-first sheet in
less than the live call-up window.

V3 is therefore a separate contract, ledger, namespace, and cutover boundary. It does
not reinterpret V2 receipts or hide new behavior behind V2's five compatibility keys.

## Race and evidence invariants

- A mark is a start count. Smaller marks start earlier; larger marks wait longer.
- Event, log diameter, species/material, and their versioned physical context remain
  prediction inputs.
- Every race is a new field. The slowest expected competitor in that field is rebased
  to Mark 3, including elite-only finals.
- Displayed marks from independently rebased fields are not portable ability evidence.
- Every heat in one round shares one frozen evidence epoch. Results become eligible only
  after the round closes, so an early heat cannot change a later heat in that round.
- Every valid completion counts, including underperformance and performances by
  eliminated competitors. DNF, DQ, DNS, void, penalty, and correction remain explicit;
  no raw time is fabricated.
- Once a sheet is issued, its marks are immutable. The first legal completion wins.
  STRATHMARK never changes the race placing after the fact.

## Prediction pipeline

```text
versioned evidence + frozen tournament epoch
        -> canonical pseudonymous competitor packet
        -> transparent robust formula forecast
        -> independent universal/specialist ML forecast
        -> blind three-member LLM council forecast
        -> deterministic validation and abstention
        -> capability and credibility updates
        -> accuracy-weighted linear distribution pool
        -> consequence-based green/amber/red disagreement
        -> deterministic fairness-frontier optimizer
        -> immutable field receipt and approval projection
```

### Independent assessors

The formula, ML hierarchy, and LLM council receive the same sealed evidence and cannot
see one another's answers. Each returns a positive raw-time distribution with provenance,
support, warning, and abstention state. Output validation is deterministic. An invalid,
late, inconsistent, or unavailable assessor abstains; it is never relabeled as another
assessor.

The LLM council is numeric in V3. It consists of two distinct local model families and
one pinned cloud model chosen through the same sealed domain bakeoff. Schema validity
alone cannot promote a model. Identity invariance, roster-order invariance, directional
behavior, calibration, latency, failure handling, and temporal replay are required.
Rolling per-competitor cards keep final field assembly independent of fresh synchronous
LLM inference.

The live rolling path schedules an executable council payload, not a symbolic request.
That payload binds the promoted two-local/one-cloud council identity, card-scoped
provider tokens, and member deadlines. Cloud work may overlap, but local GPU inference
is strictly serialized. Each member outcome and the aggregate council receipt are
sealed separately. Restart replays those exact receipts without calling a provider
again; a failed provider audit reconstructs an explicit unavailable member rather than
inventing a forecast. The older generic scheduler exists only as a compatibility seam
and is not sufficient to drive live council execution.

### Capability and foxing resistance

V3 records surprise without claiming to infer motive. A dual-state mechanism tracks
current form and demonstrated capability. Both faster- and slower-than-expected valid
performances update future estimates. A single anomalous result can move uncertainty
and review status immediately while the persistent capability state changes through a
bounded, auditable process.

This removes the useful strategy of coasting through preliminary rounds: every valid
completion can affect later rounds and future events. It still does not label a person a
cheat, adjudicate sportsmanship, or replace a judge.

### Credibility and pooling

Formula, ML, and council begin with equal cold-start weights. Settled results score each
sealed component forecast. Deterministic predictive and consequence ledgers then update
context-sensitive credibility. Weights are frozen for an open tournament epoch and
change only at legal boundaries. The original component forecasts and weights remain in
the receipt, so a pooled answer is always decomposable.

Disagreement is classified by the consequence for the sheet, not by prose or one raw
distance alone. Normal green and amber work can be scanned and batch-approved, with
amber flagged visibly. Red work is excluded from ordinary batching, and degraded work
uses its separate deliberate lane. Counterfactual marks, assessor differences,
uncertainty, and required actions remain visible. There is no default manual estimate:
the judge must deliberately choose a permitted action, and that action is signed and
auditable.

### Field optimization

The optimizer consumes the pooled joint distribution, legal mark bounds, common random
numbers, and the field roster. It searches a deterministic fairness frontier, preserves
monotonic starts, uses integer marks, and rebases the field frontmarker to 3. One native
Rust kernel accelerates the bounded search on the designated Windows machine; the ABI,
source, binary, and benchmark manifest are pinned.

## Event authority and recovery

The local V3 SQLite event store is the race-day numeric authority. Commands carry stable
identities, closed schemas, canonical payload digests, idempotency keys, and sorted
expected aggregate versions. One short transaction appends the next event and advances
required projections. Exact retries return the original result; changed retries, stale
versions, illegal transitions, chain gaps, or ambiguous state fail closed.

Prepared cards, assembled fields, approval decisions, issue acknowledgments, and result
settlements are separate states. Issue is field-atomic. Live settlement requires the
complete issued roster and commits every observation, the live settlement, and the
field-settlement event in one transaction. A projection failure therefore leaves no
partial result or event. An exact retry returns the stored outcome; a changed retry is
rejected. Acknowledged receipts never mutate. The optional archive/mirror is asynchronous
and never required for race-day calculation, issue, lookup, or settlement.

Each valid settled observation opens seven mandatory derivation reactions: capability,
invalidation, scoring, coverage, weights, readiness, and credibility. Settlement then
drives those reactions through durable source-specific authorities. A rolling forecast
completion is cryptographically bound into invalidation/readiness; settled component
scores and coverage bind the credibility and weight receipts. The round/tournament
barrier stays closed until every reaction is durably complete. The dispatcher is
restart-safe and creates no judge approval decision: unflagged and flagged sheets remain
undecided until the tournament-manager workflow deliberately acts.

The forecast runtime must use the same-ledger settlement-reaction port and wakes it
immediately after every durable card publication. This is required because scheduling
work is not evidence that Formula, ML, and all three council components actually
published. Without the publication wake-up, the last asynchronous component could leave
the derivation barrier closed until an unrelated settlement or process restart. A wake-up
failure is reported after the card is sealed; an exact retry safely re-drives the
idempotent reaction authority.

The recovery proof covers process, machine, worker, Ollama, cloud, power, WAL, blob,
disk, and queue failures. It verifies no duplicate forecast, no partial issue, no receipt
mutation, deterministic projection rebuild, and an explicit traditional/manual authority
state if trusted service cannot be restored.

## Service and authority boundary

STRATHMARK authenticates one calling service. Inside V3 that service has unrestricted
V3 authority. `X-STRATHMARK-Upstream-*` headers are bounded audit metadata, never human
authorization. Human login, roles, approval permissions, official results, publication,
points, protests, and payouts belong to the tournament manager.

Loopback is the default listener. Non-loopback operation requires mutual TLS with an
expected server hostname and pinned client CA in addition to the service credential.
Forwarded proxy identity, redirects, ambiguous singleton headers, oversized bodies, and
unbounded in-flight work are rejected before business handling.

## Frozen V3 REST surface

The canonical OpenAPI document is
[`../strathmark/v3/contracts/v3_consumer.openapi.json`](../strathmark/v3/contracts/v3_consumer.openapi.json).
Its SHA-256 is stored beside it and mechanically checked. Unknown request fields fail.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v3/cards/prepare` | Prepare or recover a rolling competitor card |
| `POST` | `/v3/approvals/decide` | Record one exact multi-receipt approval decision and explicit exclusions |
| `POST` | `/v3/credentials/revoke` | Revoke a service credential |
| `POST` | `/v3/credentials/rotate` | Rotate a service credential with bounded overlap |
| `POST` | `/v3/fields/assemble` | Assemble or recover one field receipt |
| `GET` | `/v3/health` | Public process health; not trusted readiness |
| `POST` | `/v3/issues/acknowledge` | Atomically bind upstream issue to receipt set |
| `POST` | `/v3/receipts/lookup` | Recover an immutable receipt by identity |
| `POST` | `/v3/results/settle` | Record a field-atomic outcome set |
| `GET` | `/v3/status` | Authenticated authority and service status |

Every trusted POST requires `Authorization: Bearer <service credential>` and an
`Idempotency-Key`. Credential bootstrap is an offline listener-stopped operation.
Credential rotation and revocation are themselves audited commands.

The approval route binds one immutable approval snapshot, selected and excluded receipt
IDs, receipt digests, receipt/upstream revisions, row digests, decision timestamp, actor
metadata, and caller-scoped idempotency. It invokes the approval projection authority
atomically. It does not implement RBAC, originate authorization, or replace
`/v3/issues/acknowledge`. STRATHEX must still forward it durably through its own outbox;
that external outbox is not implemented in this repository.

The consumer boundary exposes only dedicated typed workflows. Internal command kinds
remain behind their typed application services and are not advertised through a generic
event-mutation route. The frozen contract's command-coverage extension classifies every
internal `CommandKind` without pretending every command is a public operation.

## Live cadence contract

The verified progression contains heats, quarter-finals, semi-finals, divisional finals,
and a grand final. It enforces:

- 10-minute heat cadence;
- no more than two minutes from a valid result to a ready later-round sheet;
- under two seconds for final field assembly from prepared cards;
- a five-minute final call-up path;
- one evidence epoch across all fields in the same round; and
- between-round updates without modifying any issued winner or receipt.

Windows capacity is release evidence, not a source-code assertion. The evidence runner
records the exact machine, wheel, dependencies, commands, bounded Ollama probes, NVIDIA
thermal observations, and memory/storage pressure results for the committed candidate.
An older machine receipt cannot establish capacity for changed source. No rehearsal is a
universal hardware guarantee or a production attestation.

The post-format five-run designated-Windows result-to-ready benchmark completed all
trials and recorded a maximum of 3.414 seconds against the 120-second limit, with exact
source bindings and per-component latency retained in
`benchmarks/v3/result_to_ready_manifest.json`. This focused performance result does not
substitute for regeneration of the complete exact-wheel release evidence on the final
documentation commit.

## Factory runtime boundary

`compose_local_factory_runtime()` supplies a runnable local scheduler for factory
automation and continuous settled-evidence monitoring. It accepts only the complete,
ordered formula/ML/LLM executor family at the configured-local boundary and retains the
existing manual, signed promotion authority. The bounded canonical evaluator exchange
can run in a separate process; the installed `strathmark-v3-factory-evaluator` command
opens an already provisioned Windows CNG key by name and evaluates one request with
exact-retry checks. The repository script is a thin delegate to that packaged command.

This is a composition boundary, not a production factory declaration. Concrete family
executors, the local evaluator that derives settlement metrics from authoritative local
facts, OS accounts and ACL separation for builder/evaluator/signer, and non-exportable
CNG key provisioning remain installation work. The final exact-source evidence and CI
must exercise those real components before promotion or cutover evidence is accepted.

## Reproducible proof

Use isolated paths. These commands do not change authority:

```powershell
$env:STRATHMARK_TEST_DB = '1'
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\v2-proof.sqlite3"
$env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\v3-proof.sqlite3"
python scripts/replay_v3.py
python scripts/run_v3_release_evidence.py --local-model qwen3.5:9b --local-model ministral-3:8b
$source = (git rev-parse HEAD).Trim()
python scripts/verify_v3_release.py --evidence benchmarks/v3/v3_executable_evidence.json --emit-rehearsal $source --output-attestation benchmarks/v3/v3_release_attestation.json
python scripts/verify_v3_release.py
python scripts/verify_v3_release.py --require-production
```

The evidence runner must start from an unchanged committed tree and builds and installs
the exact wheel before executing all eleven required proof classes. Evidence is kept
outside the wheel so its digest cannot depend circularly on its own attestation. The
ordinary verifier must fail for a missing artifact, changed source or tree, failed
command, source-only substitution, signature mismatch, or canonical-byte tamper. After
fresh evidence and a rehearsal attestation are produced, the ordinary verifier must
pass and the production-required command must fail with
`production_attestation_required`. That failure proves the verifier does not convert
rehearsal evidence into production authority.

## Cutover gate

A separately authorized operator must satisfy all of the following before any switch:

1. Generate the exact production evidence set with a live non-exportable Windows CNG
   identity, then verify it against the separately operator-pinned public identity using
   `--trusted-production-identity`. Never trust the identity embedded in the attestation
   under verification.
2. Freeze V2 trusted writes only at zero open tournaments.
3. Resolve every in-flight or ambiguous V2 operation and sign the final V2 authority
   manifest.
4. Verify the initialized V3 database, bundle, consumer contract, and release digest.
5. Rehearse the installed tournament-manager adapter and match its digest.
6. Produce the signed pre-switch handoff. It must still state `current_authority=v2`,
   `next_authority=v3`, `endpoint_switched=false`, and
   `requires_explicit_release_authorization=true`.
7. Obtain the separate release authorization and switch the consumer contract once.

Any preparation failure resumes V2. If V2 cannot be resumed, the system declares
traditional/manual authority explicitly. There is no silent numeric fallback and never
dual trusted write authority.

## Historical boundary

[`PREDICTION_ENGINE_V2.md`](PREDICTION_ENGINE_V2.md) remains the exact V2 contract and
receipt explanation. The dated
[`V3 implementation plan`](plans/2026-08-22-001-feat-adaptive-ensemble-prediction-engine-plan.md)
records why the pivot was made and which alternatives were rejected. Neither document
authorizes a production cutover.
