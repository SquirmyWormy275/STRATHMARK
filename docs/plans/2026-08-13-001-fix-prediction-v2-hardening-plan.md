---
title: Prediction Engine V2 Second-Pass Hardening - Plan
type: fix
date: 2026-08-13
deepened: 2026-08-13
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Prediction Engine V2 Second-Pass Hardening - Plan

> **Historical V2 hardening contract.** Its evidence remains valid for V2. The later V3
> architecture is specified by
> [`2026-08-22-001-feat-adaptive-ensemble-prediction-engine-plan.md`](2026-08-22-001-feat-adaptive-ensemble-prediction-engine-plan.md).
> V2 remains production authority until explicit cutover.

## Goal Capsule

Harden the released Prediction Engine V2 without changing its approved evidence
boundary or reopening its sealed benchmark. The pass must close the remaining
causal, artifact, drift, persistence, optimizer, packaging, and documentation
gaps; prove each change with isolated tests; preserve public compatibility; and
finish on the existing feature branch and pull request with CI green.

The authority order is:

1. The current user-directed verified-factor boundary and this plan.
2. The canonical V2 contract in `docs/PREDICTION_ENGINE_V2.md`.
3. Mark floor, ceiling, monotonicity, and rounded-gap fallback invariants.
4. The sealed release evidence and checksummed packaged artifact.
5. Existing public Python and REST compatibility contracts.

Stop and surface a blocker if a change would require unavailable tournament
context, alter the published model coefficients or locked metrics, touch a live
database, make race-day calculation wait on a remote service, or weaken a public
compatibility invariant. This run may strengthen validation and observability,
but it must not retune, retrain, reactivate a residual model, reopen locked rows,
merge, deploy, or apply the production migration.

## Product Contract

### Summary

The hardened system remains deterministic and prior-only. It admits only
canonical, strictly earlier evidence; rejects semantically invalid artifacts at
load time; samples a documented finish-time predictive distribution for mark
optimization; records only provenance-complete rows as future training evidence;
measures issued-interval coverage directly; bounds mirror-delivery resources; and
ships reproducible package and CI evidence across every declared Python version.

### Problem Frame

The first release pass established a strong core and a sealed 128-row locked
result. A second adversarial review found several failure modes that ordinary
happy-path tests do not cover:

- a calibrated artifact can lack a causal evidence date;
- a recomputed-checksum artifact can carry invalid coverage, counts, radii, or
  cross-event coefficients and fail only when serving;
- trusted rows can be marked training-eligible without complete provenance;
- idempotency hashes include raw rows that the prediction boundary later rejects;
- drift estimates coverage from residual quantiles instead of the intervals
  actually issued;
- every mirror retry can create another daemon thread;
- optimizer comments blur the distinction between the finish-time posterior and
  the legacy public performance `std_dev`;
- the maximum 64-competitor optimizer path lacks an exhaustive small-field oracle
  and an explicit performance budget;
- supported Python and API dependency claims are not all exercised at their
  compatibility boundaries; and
- stale code comments and TODOs still describe the retired cascade or rounding
  behavior.

### Actors

- A1. Handicapper: requests a deterministic start sheet and may use a manual
  override.
- A2. Tournament application: calls public stateless prediction routes or an
  authenticated trusted-ledger route.
- A3. Model operator: verifies artifacts and monitors settled performance away
  from the prediction hot path.
- A4. Maintainer: audits compatibility, deterministic behavior, packaging, and
  release evidence.

### Requirements

#### Causal evidence and artifact admission

- R1. Every prediction, performance-variance calculation, idempotency identity,
  feature snapshot, calibration component, and drift cohort uses the same
  exclusive UTC `prediction_as_of` boundary.
- R2. Same-day, future, invalid-date, and undated rows are numeric no-ops across
  predictions, intervals, marks, trusted hashes, and stored active features.
- R3. An active calibrated state must have a valid maximum evidence date earlier
  than the request cutoff. Undated calibrated state is invalid, not timeless.
- R4. Artifact loading fails closed before serving when semantic invariants are
  broken, even if the envelope checksum was recomputed correctly.
- R5. Artifact semantic validation covers nonempty versions; supported engine and
  canonicalization versions; finite coefficients and scales; allowed event and
  history-band labels; complete structural event/count and cross-event maps;
  support-qualified sparse radius maps whose entries each have a matching positive
  count; bounded cross-event coefficients; positive support ranges; nonnegative
  counts and radii; coverage strictly between zero and one; causal date ordering;
  and internally consistent validation metadata.
- R6. Request wood-property values must be finite and positive. Standardized
  property effects are clamped to eight artifact standard deviations before
  matrix evaluation so extreme direct-core inputs cannot overflow or dominate
  the posterior. Public known-species resolution remains compatible.

#### Prediction and mark semantics

- R7. The mark optimizer samples the V2 predictive distribution of actual finish
  time. That distribution includes irreducible event-performance variation plus
  supported state and forecast uncertainty; calibrated interval widening may
  conservatively increase its sampling scale.
- R8. The legacy public `std_dev` remains an absolute-seconds performance summary
  used by compatibility consumers. It is not substituted for a valid V2
  posterior and is used only as the documented fallback when no model posterior
  or interval exists.
- R9. Forecast interval fields and `std_dev` remain distinct in responses,
  persistence, drift reporting, documentation, and tests.
- R10. Mark sheets preserve Mark 3, the effective ceiling no greater than 183,
  monotonic faster-to-later ordering, deterministic common-random-number
  sampling, lexicographic objective order, and the half-to-even rounded-gap
  fallback.
- R11. For tractable small fields and ceilings, the optimizer must match an
  exhaustive global oracle or document and test the bounded approximation. For
  the public 64-competitor limit, a default 2,048-sample optimization must finish
  within 10 seconds on a standard GitHub-hosted Ubuntu runner without changing
  the objective or exceeding 256 MiB of incremental process memory.
- R12. Optimizer changes are evidence-driven: first measure correctness and
  runtime; modify search only for a demonstrated miss or budget violation.

#### Trusted evidence and drift

- R13. Ledger idempotency hashes only canonical request inputs that can affect
  predictions or marks. Adding or changing excluded history must not create a
  conflict when the served field is unchanged.
- R13a. Request hashes are versioned additively. Existing request rows remain
  `raw-v1`; new rows use `active-v2`; and a retry uses the hash algorithm recorded
  on the existing immutable request row. No stored digest is rewritten.
- R14. A row is training-eligible only when it is a nondegraded V2 model result
  with stable identity, model and calibration versions, causal evidence cutoff,
  a complete valid issued interval, and an eligible source. Incomplete rows may
  be stored but are forced ineligible.
- R15. Manual, panel, broad-prior, degraded, incompatible, and provenance-incomplete
  rows never enter future model evidence.
- R16. Drift coverage is the empirical fraction of eligible settled actual times
  inside each row's issued interval. It is never reconstructed from residual
  quantiles.
- R17. Point residual drift, interval coverage, and future mark/fairness
  diagnostics remain separate, advisory signals. Drift never auto-trains,
  auto-activates, auto-disables, or changes race-day output.
- R18. Drift uses latest settlement revisions and keeps model version,
  calibration version, event, nominal coverage, and history-depth metadata
  available for supported cohorting. Missing legacy interval data is labeled
  unavailable and excluded from coverage denominators.
- R18a. Coverage is reported per nominal-coverage cohort. Existing alert thresholds
  apply only to the 0.90 cohort; other nominal levels remain descriptive until an
  explicit threshold and minimum sample size are approved.

#### Offline persistence and delivery

- R19. SQLite remains the authoritative race-day ledger and its prediction,
  outbox, and settlement writes remain atomic and append-only.
- R20. Cloud mirror latency or failure never delays or fails a calculation.
- R21. Mirror scheduling uses at most one delivery worker per ledger and
  deduplicates work already in flight. Durable outbox replay remains available
  through the explicit off-path flush operation.
- R22. Retry storms, duplicate delivery, mirror exceptions, process interruption
  after local commit, and concurrent ledger instances preserve local data and
  expose an honest pending, failed, or recorded state.
- R23. All ledger, API, migration, mirror, and drift tests use temporary local
  paths, mocked remote clients, or disposable service databases with ambient
  production credentials removed before imports.

#### Compatibility, release, and documentation

- R24. Public unauthenticated routes remain stateless; authenticated ledger
  routes retain server-owned caller identity and fail-closed authorization.
- R25. Field calculation still snapshots one immutable V2 bundle and never
  trains, reloads per competitor, or reads global history on the hot path.
- R26. Health reports distinguish an available core from one compatible with
  the requested cutoff.
- R27. The release verifier binds the manifest, prelock record, report, and
  packaged artifact to a review-visible independent attestation. Coordinated
  report-plus-artifact tampering must fail verification.
- R27a. The attestation is a separate reviewed file containing fixed digests for
  the manifest, prelock record, report, and packaged artifact. Normal verification
  never regenerates it; changing it requires an explicit governance-only update
  reviewed independently from the release payload changes it authorizes.
- R28. The existing locked role and numeric artifact payload are immutable in
  this pass. Verification uses only the safe release-verification path.
- R29. CI exercises Python 3.10, 3.11, 3.12, and 3.13 as declared; checks resolved
  dependency consistency; tests one documented oldest-compatible API set and a
  current-compatible set; and smoke-tests installed artifacts outside the source
  checkout.
- R30. Cross-platform golden scenarios cover marks, stable IDs, canonical hashes,
  warnings, artifact fingerprint, and normalized serialized metadata. Numeric
  canonicalization is introduced only if measurement proves platform drift.
- R31. Canonical docs, code comments, TODOs, changelog, API reference, and wiki
  source describe V2, actual rounding, variance semantics, drift limits, release
  evidence, and the still-separate production migration without contradiction.

### Key Product Decisions

- KPD1 (`user-directed`). Unsupported tournament factors remain accepted only as
  compatibility no-ops until future tournament software records them with
  provenance. Rejected: infer tournament context from names or neighboring rows;
  those factors cannot be proven today. Governs R1-R2 and R31.
- KPD2 (`user-approved`). The published locked evaluation remains sealed.
  Rejected: tune after inspecting locked outcomes; that would convert the test
  into training evidence. Governs R27-R28.
- KPD3 (`session-settled`). This hardening pass changes admission, monitoring,
  delivery, tests, and documentation, not published model numbers. Rejected:
  bundle a model retune into correctness fixes; it would erase the evidentiary
  boundary. Governs R3-R6 and R27-R30.

### Flows

- F1. Load artifact -> verify envelope and semantic invariants -> report usable or
  incompatible state -> snapshot once -> predict from canonical earlier evidence.
- F2. Calculate field -> produce posterior distributions -> optimize deterministic
  marks -> preserve interval and `std_dev` as separate outputs -> return even when
  trusted persistence or mirroring fails.
- F3. Authenticated calculate -> canonicalize only active request evidence ->
  derive stable request/prediction IDs -> atomically write predictions and outbox
  -> schedule bounded best-effort delivery.
- F4. Settle by prediction ID -> append or correct settlement revision -> select
  latest eligible revision -> compute point drift and issued-interval coverage
  separately.
- F5. Verify release -> validate immutable manifests, report, artifact, and
  independent attestation -> installed-package smoke outside checkout -> report
  evidence without opening the locked workbook role.

### Acceptance Examples

- AE1. A result timestamped `2025-12-31 23:30-12:00` is treated as
  `2026-01-01` UTC and cannot affect a `2026-01-01` prediction, `std_dev`, mark,
  trusted hash, or active feature snapshot.
- AE2. A checksum-valid artifact with nominal coverage `1.5`, a negative count,
  an out-of-cap cross-event coefficient, or calibrated radii without an evidence
  date is rejected before prediction.
- AE3. Adding an invalid or future history row to an otherwise identical trusted
  retry returns the same request hash and does not raise an idempotency conflict.
- AE4. A direct ledger caller marks an incomplete baseline row eligible; the
  ledger stores it as ineligible and it never appears in training rows.
- AE5. Three issued intervals contain two settled actuals. Coverage is `2/3`
  regardless of the baseline residual quantiles; a legacy row without an
  interval does not enter the denominator.
- AE6. One hundred rapid schedule attempts against a blocked mock mirror create
  bounded worker activity, return promptly, preserve one durable outbox entry per
  entity, and remain flushable after the mirror recovers.
- AE7. A small field where coordinate descent is suboptimal is either solved to
  the exhaustive oracle or retained as an explicit, tested approximation with a
  documented reason and fallback; no search result is worse than rounded-gap.
- AE8. The 64-competitor scenario completes within the accepted race-day budget
  on the reference CI runner and returns the same normalized golden result on
  Windows and Linux.
- AE9. Public `/calculate` and `/predict` calls leave the trusted ledger unchanged;
  the protected route records only after successful authorization.
- AE10. Editing both an artifact and its report while recomputing their local
  checksums still fails the independent release attestation.

### Success Criteria

- SC1. Every adversarial artifact and temporal-boundary example fails or remains
  invariant at the earliest owning boundary.
- SC2. Drift reports actual issued-interval coverage and never fabricates coverage
  for rows that did not publish an interval.
- SC3. Trusted training rows are provenance-complete and idempotency identity
  matches the predictor's active causal evidence.
- SC4. Mirror work is nonblocking, durable, replayable, and resource-bounded.
- SC5. Optimizer semantics are unambiguous, small-field correctness is measured,
  and the maximum public field has a recorded performance result.
- SC6. Release evidence verifies without opening locked rows, and coordinated
  tampering is caught.
- SC7. All supported Python versions, API compatibility sets, installed-package
  smokes, focused tests, full isolated tests, lint, formatting, and diff checks
  pass on the existing pull request.
- SC8. Canonical documentation and wiki source contain no unlabeled retired
  numeric cascade, tournament-weighting, rounding, variance, or release claims.

### Scope Boundaries

In scope:

- causal and semantic validation;
- trusted ledger admission, identity, interval fields, and outbox scheduling;
- interval-aware advisory drift;
- optimizer semantics, correctness oracle, and measured performance;
- health, package, dependency, cross-platform, and release-attestation gates;
- documentation, TODO, changelog, API reference, and wiki-source reconciliation.

Out of scope:

- new prediction factors or inferred tournament context;
- model retuning, retraining, coefficient changes, residual promotion, or a new
  accuracy claim;
- opening or re-evaluating the locked test role;
- automatic operational action from drift;
- production Supabase access, production migration application, merge, or deploy;
- tournament-management software and its future provenance schema.

## Planning Contract

### Key Technical Decisions

- KTD1. Use one canonical active-evidence projection for prediction, performance
  variance, feature snapshots, and trusted idempotency. Rationale: separate raw
  projections allow excluded rows to affect marks or create false conflicts.
  Rejected: hash all request history for conservative idempotency; it violates
  causal invariance. Governs R1-R2, R13, and AE1-AE3.
- KTD2. Validate semantic artifact invariants at construction/load time and make
  active calibration require a dated evidence maximum. Rationale: checksum
  integrity proves bytes were not accidentally changed, not that the payload is
  safe to serve. Rejected: defer validation until prediction; that creates
  request-time failures. Governs R3-R6 and AE2.
- KTD3. Treat the V2 optimizer distribution as a finish-time posterior containing
  both irreducible outcome variation and forecast uncertainty. Preserve
  absolute-seconds `std_dev` as a separate compatibility statistic and fallback.
  Rationale: winning probability is defined over future finish outcomes, while
  substituting the legacy statistic discards calibrated model uncertainty.
  Rejected: optimize only point-estimation uncertainty or only legacy `std_dev`.
  Governs R7-R12 and AE7-AE8.
- KTD4. Compute interval coverage from each prediction's own issued bounds and
  nominal level. Rationale: residual-quantile reconstruction is not equivalent to
  heterogeneous log-space conformal intervals. Rejected: preserve the existing
  residual proxy for convenience. Governs R16-R18 and AE5.
- KTD5. Fail closed for training eligibility but fail open for race-day storage and
  mirroring. Rationale: incomplete evidence must not train a future model, while
  optional persistence must not block calculations. Rejected: reject the entire
  field write for one ineligible row. Governs R14-R23 and AE4-AE6.
- KTD6. Use a single bounded per-ledger delivery worker with in-flight
  deduplication and durable SQLite outbox state; keep synchronous bounded flush as
  the recovery primitive. Rationale: a lock serializes work but does not bound the
  number of waiting threads. Rejected: one daemon thread per attempt. Governs
  R19-R22 and AE6.
- KTD7. Keep published benchmark inputs, metrics, and numeric artifact payload
  immutable; add the separate fixed-digest attestation defined by R27a and make it
  a verifier input, never a verifier output. Rationale: a report that authenticates
  its sibling artifact cannot detect a coordinated edit, while a separately
  governed Git-reviewed digest set supplies an independent trust anchor. Rejected:
  rerun or replace the locked test, or regenerate attestation during ordinary
  verification. Governs R27-R28 and AE10.
- KTD8. Test API dependencies as compatible sets, including a verified oldest set
  and current resolution. Rationale: current Starlette releases use `httpx2`,
  while older FastAPI lower bounds may resolve Starlette versions built around
  plain `httpx`; independent lower bounds do not prove a working pair. Rejected:
  rename or raise dependencies without a minimum-version smoke. Governs R29.
- KTD9. Measure cross-platform numerical output before canonicalizing it. If drift
  exists, normalize only serialized audit fields at an explicitly versioned
  precision; never round internal model or optimizer math to make a golden pass.
  Governs R30.

### Alternatives Considered

- Keep the current drift proxy: rejected because it can report healthy coverage
  even when issued intervals miss actual outcomes.
- Let callers opt into training with a boolean: rejected because the authoritative
  ledger must derive eligibility from stored provenance.
- Use an unbounded executor instead of raw threads: rejected because it changes
  syntax but not the resource guarantee.
- Replace coordinate search immediately: rejected until the exhaustive oracle and
  maximum-field measurement demonstrate a correctness or performance gap.
- Repackage or retrain the model to add validation fields: rejected because the
  existing payload already contains the needed causal date and must remain sealed.
- Add live production migration tests: rejected; disposable PostgreSQL or static
  contract verification is the permitted boundary.

### High-Level Technical Design

```text
raw request/history
        |
        v
canonical active-evidence boundary ----> trusted request hash
        |                                      |
        v                                      v
validated immutable V2 bundle           atomic local ledger + outbox
        |                                      |
        v                                      +--> one bounded mirror worker
finish-time predictive distributions
        |
        +--> calibrated issued intervals --> latest settlements --> coverage drift
        |
        +--> deterministic joint optimizer --> marks + legacy std_dev kept separate

sealed manifest + prelock + report + artifact + independent attestation
        |
        v
safe verifier --> installed wheel/sdist and cross-platform golden smokes
```

This sketch defines boundaries and ownership, not exact implementation syntax.

### Assumptions

- The packaged core's calibration already has a valid evidence date.
- The public maximum field size remains 64 competitors.
- The existing locked report remains the sole release accuracy claim for the
  current artifact.
- CI can add bounded matrix jobs; disposable PostgreSQL coverage may be added only
  if it remains isolated and deterministic.
- No current settled-production volume is assumed sufficient for automatic drift
  decisions or optimizer retuning.

### Open Questions

Resolved during planning:

- The optimizer targets future finish outcomes, not only estimator uncertainty
  and not only legacy absolute `std_dev`; KTD3 owns the distinction.
- The maximum-field race-day budget is 10 seconds and 256 MiB incremental memory
  on the reference CI runner. Local measurements are diagnostic, not the release
  authority.
- Invalid calibrated state is rejected during artifact admission rather than
  silently treated as compatible.

Deferred to implementation evidence:

- The exact oldest FastAPI, Starlette, and TestClient dependency set. U6 must
  identify the lowest mutually compatible set by an isolated install smoke and
  align metadata to that proven combination; it may not guess from independent
  package lower bounds.
- Whether the current coordinate search requires replacement. U5's exhaustive
  oracle and capacity measurement decide; no search rewrite is authorized without
  a demonstrated objective miss or budget violation.
- Whether disposable PostgreSQL is reachable in CI. U4 adds the service-backed
  contract when it can guarantee isolation; otherwise it preserves and labels the
  static migration gate rather than using production credentials.

### System-Wide Impact

- Prediction core: request and artifact validation become fail-closed earlier;
  in-domain inputs and current golden predictions remain unchanged, while
  extreme accepted direct-core properties are bounded by R6.
- Calculator: history canonicalization, optimizer semantics, and comments align;
  result ordering and public fields remain compatible.
- Ledger: trusted row selection gains interval/provenance columns; scheduling
  becomes bounded; append-only schema semantics remain intact.
- Drift: report construction receives issued bounds and exposes separate point and
  coverage states; no hot-path or automatic action is added.
- API and health: public routes remain stateless; compatibility status becomes
  cutoff-aware and additive.
- Release: verification gains independent binding without reading locked data;
  package jobs exercise source-independent installs.
- Documentation: primary and wiki sources replace stale behavior claims while
  keeping historical decisions explicitly superseded.

### Risks and Mitigations

- Risk: stricter artifact validation rejects legitimate historical artifacts.
  Mitigation: load the current packaged artifact in every focused and installed
  package gate; degrade incompatible external artifacts through the documented
  provider fallback.
- Risk: canonical request hashes change for existing untrusted raw payloads.
  Mitigation: implement R13a's additive `raw-v1`/`active-v2` protocol, select the
  existing row's algorithm on retry, and preserve all recorded digests.
- Risk: interval-aware drift has fewer usable rows than the current proxy.
  Mitigation: label sample size and unavailability honestly; do not backfill
  fictional bounds.
- Risk: worker lifecycle introduces races at shutdown.
  Mitigation: daemon-safe bounded ownership, in-flight cleanup in `finally`, and
  deterministic explicit flush tests.
- Risk: performance gates become flaky.
  Mitigation: separate deterministic correctness/operation-shape gates from a
  generously bounded reference benchmark and record the environment.
- Risk: exact floats differ across BLAS or OS builds.
  Mitigation: compare normalized audit output first; do not alter model math
  unless a real public determinism violation is measured.
- Risk: dependency lower bounds form an impossible combination.
  Mitigation: declare and test a complete compatible set, then align metadata to
  that proven set.
- Risk: broader migration tests accidentally use real credentials.
  Mitigation: scrub ambient connection variables and require an explicit
  disposable service URL; otherwise skip with a visible reason.

### Research and Evidence

- `strathmark/prediction_v2.py`: current request, calibration, model, artifact,
  sampling, and compatibility boundaries.
- `strathmark/calculator.py`: immutable field snapshot, performance variance,
  optimizer distribution reconstruction, and raw-history ledger identity.
- `strathmark/ledger.py`: append-only trusted rows, training-row projection,
  outbox, and one-thread-per-attempt scheduling.
- `strathmark/drift.py`: settled-row filtering and residual-derived coverage.
- `strathmark/mark_optimizer.py`: deterministic common-random-number coordinate
  search and rounded-gap fallback.
- `scripts/validate_v2.py` and `benchmarks/prediction_v2_*`: safe release verifier,
  sealed roles, and current report-to-artifact binding.
- `.github/workflows/ci.yml` and `pyproject.toml`: supported Python versions,
  dependency claims, and installed-wheel coverage.
- `docs/solutions/architecture-decisions/absolute-variance-only.md`,
  `mark-floor-ceiling-invariants.md`, and
  `tournament-weighting-and-decay.md`: historical invariants and supersession.
- `docs/solutions/best-practices/test-isolation-no-prod-db.md`: isolation boundary.
- Official FastAPI testing and release documentation and Starlette TestClient and
  release documentation: the 2026 transition from plain `httpx` to `httpx2` and
  the need to validate dependency combinations instead of isolated lower bounds.

### Settled-Decision Conflict Report

- KPD1 and KPD2 have no invalidating conflict. Repository evidence reinforces the
  no-proxy factor boundary and sealed-test rule.
- Historical absolute-variance guidance appeared to conflict with optimizer use
  of a lognormal posterior. KTD3 resolves the conflict by assigning different
  meanings: the optimizer samples future finish outcomes, while public `std_dev`
  remains an absolute-seconds compatibility statistic. No public field is
  redefined.
- The existing comment that marks are always rounded up conflicts with the tested
  half-to-even fallback invariant. Documentation is corrected; numeric behavior
  is preserved.

## Implementation Units

### U1. Establish causal and semantic admission boundaries

- Traces: R1-R6, R25-R26; AE1-AE2.
- Files: `strathmark/features.py`, `strathmark/prediction_v2.py`,
  `strathmark/predictor.py`, `tests/test_features.py`,
  `tests/test_prediction_v2.py`, `tests/test_validation.py`, `tests/test_api.py`.
- Approach: add adversarial and metamorphic tests first; centralize semantic
  artifact validation; require dated active calibration; validate request property
  bounds; make health cutoff-aware; preserve valid artifact outputs.
- Test scenarios: UTC boundary rows; row-order invariance; unrelated competitor
  history invariance; inactive-factor invariance; extreme finite properties;
  recomputed-checksum semantic corruption; undated calibrated state; current
  packaged artifact load; compatible and incompatible health cutoffs.
- Verification outcome: invalid state is rejected at load/request boundaries,
  current in-domain golden predictions remain unchanged, and extreme direct-core
  property effects remain finite and bounded.
- Depends on: none.

### U2. Align trusted identity and training admission

- Traces: R1-R2, R13-R15, R19, R23-R25; AE1, AE3-AE4, AE9.
- Files: `strathmark/calculator.py`, `strathmark/ledger.py`,
  `tests/test_calculator.py`, `tests/test_ledger.py`, `tests/test_api.py`.
- Approach: reuse the canonical active-evidence projection for new `active-v2`
  request hashes and stored active features; preserve `raw-v1` rows and select
  their recorded algorithm on retry; derive eligibility from complete validated
  provenance; keep incomplete rows append-only but ineligible; preserve route
  statelessness.
- Test scenarios: excluded-row retry invariance; meaningful active-row conflict;
  schema bootstrap and exact retry of an existing `raw-v1` row; new `active-v2`
  identity; missing versions/cutoff/interval; invalid coverage/bounds; manual,
  broad-prior, degraded, and exact-model rows; latest settlement revisions; public
  route write absence; authenticated stable-ID write.
- Verification outcome: hashes track only behaviorally active inputs and training
  rows contain only provenance-complete model predictions.
- Depends on: U1.

### U3. Make settled drift interval-aware

- Traces: R16-R18, R23; AE5.
- Files: `strathmark/ledger.py`, `strathmark/drift.py`,
  `tests/test_ledger.py`, `tests/test_drift.py`.
- Approach: project issued interval bounds, nominal coverage, state/scope, model
  and calibration versions, and history depth from trusted rows; calculate direct
  empirical coverage per nominal level; retain current alerts only for the 0.90
  cohort and report other levels descriptively; retain point residual shift
  separately; label missing and insufficient samples.
- Test scenarios: heterogeneous intervals, several nominal levels, missing legacy
  intervals, invalid/ineligible rows, correction revisions, event/version/history
  cohort filtering, zero eligible coverage rows.
- Verification outcome: coverage equals direct containment and cannot be changed
  by substituting baseline residual quantiles.
- Depends on: U2.

### U4. Bound and exercise mirror delivery

- Traces: R19-R23; AE6.
- Files: `strathmark/ledger.py`, `strathmark/store.py`,
  `tests/test_ledger.py`, `tests/test_store.py`, and migration contract tests when
  a disposable service is available.
- Approach: replace per-attempt threads with one bounded worker and in-flight
  deduplication; preserve atomic local commit and explicit flush; add failure and
  recovery observability; keep cloud payload sanitation and RLS assumptions.
- Test scenarios: blocked mirror retry storm; duplicate entity scheduling;
  multiple entities; mirror exception; crash-shaped post-commit recovery;
  concurrent ledger instances; duplicate cloud acknowledgment; flush limit;
  disposable migration/RPC/RLS contract or explicit isolated skip.
- Verification outcome: local writes return promptly, each ledger owns at most one
  delivery worker, and every pending durable item can be replayed without
  duplication.
- Depends on: U2.

### U5. Prove optimizer semantics, correctness, and capacity

- Traces: R7-R12; AE7-AE8.
- Files: `strathmark/calculator.py`, `strathmark/mark_optimizer.py`,
  `tests/test_calculator.py`, `tests/test_mark_optimizer.py`, and a bounded
  optimizer benchmark/report under `benchmarks/` or `scripts/`.
- Approach: encode KTD3 in typed metadata and documentation; build an exhaustive
  oracle for small fields/ceilings; compare the current coordinate search before
  changing it; measure 64-competitor runtime and memory; improve search only when
  evidence shows an objective miss or budget failure.
- Test scenarios: exact-posterior versus interval and `std_dev` fallback paths;
  equal medians; manual/model mixtures; ceiling pressure; seeded repeatability;
  exhaustive adversarial small fields; objective tie breakers; fallback on invalid
  samples; maximum public field.
- Verification outcome: mark semantics are explicit, invariants hold, search is
  globally checked where tractable, and reference capacity is recorded.
- Depends on: U1.

### U6. Strengthen release, dependency, and determinism evidence

- Traces: R27-R30; AE8, AE10.
- Files: `scripts/validate_v2.py`, `tests/test_validate_v2.py`, `pyproject.toml`,
  `.github/workflows/ci.yml`, immutable release-attestation metadata, and focused
  package/golden smoke fixtures.
- Approach: add R27a's separately governed fixed-digest file as an immutable
  verifier input without editing numeric payloads; prohibit ordinary regeneration;
  add coordinated-tampering tests; include Python 3.11; run dependency consistency;
  establish verified oldest/current API sets; install wheel and source distribution
  outside the checkout on representative Windows and Linux jobs; compare normalized
  golden outputs.
- Test scenarios: report-only, artifact-only, and coordinated tampering; missing
  attestation; valid release verification; oldest and current API TestClient;
  missing packaged model; wheel/sdist import; cross-platform golden field;
  intentional normalized-field change.
- Verification outcome: the safe verifier catches coordinated edits, supported
  environments agree on public audit output, and packaging claims match tested
  reality without reopening locked rows.
- Depends on: U1, U2, U5.

### U7. Reconcile canonical documentation and operational handoff

- Traces: R7-R10, R16-R18, R21, R26-R31; SC8.
- Files: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `TODOS.md`,
  `docs/PREDICTION_ENGINE_V2.md`, architecture/deployment/persistence docs,
  `docs/wiki/`, public docstrings, and `STRATHMARK API.txt` when intentionally
  force-added.
- Approach: document finish-time posterior versus `std_dev`, direct interval
  coverage, bounded delivery, cutoff-aware health, release attestation,
  compatibility sets, actual half-to-even fallback, sealed evidence, and the
  separate unapplied production migration; supersede stale ensemble/cascade and
  tournament-weighting TODOs.
- Test scenarios: contradiction grep, primary relative-link audit, wiki navigation
  audit, generated/API examples against live response shapes, ignored-file check.
- Verification outcome: all authoritative and public-facing sources describe the
  same behavior and operational boundary.
- Depends on: U1-U6.

## Verification Contract

- V1. RED evidence is recorded for every behavior-bearing unit before production
  edits; regressions prove they fail when the guarded defect is restored.
- V2. Focused prediction, validation, calculator, optimizer, ledger, drift, API,
  store, packaging, and release suites pass with temporary database and base paths.
- V3. The complete test suite passes with ambient production connection variables
  removed and no writes outside isolated fixtures.
- V4. Ruff check, Ruff format check, and diff whitespace checks pass on all changed
  files.
- V5. Safe release verification succeeds exactly once per final gate and never
  uses prepare or locked-open modes.
- V6. The packaged artifact loads from an installed distribution outside the
  source checkout on declared environments.
- V7. Cross-platform golden output and optimizer capacity evidence are persisted
  with environment metadata and reviewed before any numeric canonicalization or
  search change.
- V8. CI on the existing pull request is green across lint, supported Python,
  API compatibility, optional ML, release, package, isolation, and new hardening
  gates.
- V9. Git history shows review-visible plan, tests, code, documentation, and
  evidence changes without modification of the sealed numeric benchmark payload.

## Definition of Done

- All R1-R31 requirements and AE1-AE10 examples are implemented or proven already
  satisfied, with no silent deferrals.
- The current packaged model produces unchanged valid predictions and retains its
  published locked metrics and numeric payload.
- Drift uses actual issued intervals; trusted training rows and hashes share the
  canonical causal boundary; mirror concurrency is bounded.
- Optimizer semantics and capacity are proven without weakening mark invariants.
- Supported runtime, dependency, installed-package, and cross-platform claims are
  covered in CI.
- Canonical docs, code comments, TODOs, and wiki source are synchronized.
- Independent simplification and structural code review find no unresolved P0/P1
  issue; actionable findings are fixed and reverified.
- Changes are committed and pushed to the existing feature branch and pull request;
  all required CI checks are green.
- Production databases, production migration, merge, deployment, unavailable
  factors, locked-test reopening, and model retuning remain untouched.
