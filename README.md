<p align="center">
  <img src="https://raw.githubusercontent.com/SquirmyWormy275/STRATHMARK/main/assets/strathmark_logo.png" alt="STRATHMARK" width="480"/>
</p>

# STRATHMARK

STRATHMARK is an offline-capable woodchopping prediction and handicap-mark engine.
It preserves the released V2 engine and contains the V3 adaptive ensemble release candidate.

## Current release state

V3 is a `3.0.0rc1` release candidate that tracks all 232 requirements in
the V3 plan. Repository implementation and audit are complete for this candidate. The
checked-in development-key rehearsal is valid only for the source commit and digests it
names and must pass the release verifier; it is not production evidence. V2 remains the
globally trusted production authority. V3 is not production-eligible, no production
authority has changed, and V2 is not audit-only.

The current integration model is **competition-scoped selection**, not one global engine
replacement. A standalone event selects once at event setup. A tournament selects once
at tournament creation, and every child event and round inherits that choice. Different
competition roots may use different eligible engines, but one root never mixes V2 and
V3 and never silently falls back. STRATHMARK supplies the V6 contract for that workflow;
the external STRATHEX adapter and its installed rehearsal remain separately versioned
consumer responsibilities.

The distinction matters:

- **V2.0.0** is the immutable released and currently trusted production engine.
- **V3.0.0rc1** has separate code, contracts, storage, and tests. The formatted
  five-run Windows result-to-ready benchmark completed with a maximum of **3.414
  seconds**, well inside the 120-second requirement. Exact-wheel evidence and a
  development-key rehearsal attestation are candidate outputs, not permanent claims
  that survive a source change.
- **Factory qualification** has a runnable local composition/scheduler and a bounded
  production-CNG evaluator entrypoint. Concrete formula/ML/LLM family executors, the
  local settled-evidence metric evaluator, installation OS identities and ACLs, and CNG
  provisioning are still deployment gates; their absence cannot be replaced by mocks.
- **Production-eligible V3** does not exist until a non-exportable Windows CNG identity
  signs the exact production evidence and a separately authorized zero-open-tournament
  eligibility handoff is completed. That production identity has not been provisioned.
  Eligibility would make V3 available for deliberate selection; it would not select V3
  globally or remove V2.

Start with the mandatory domain source of truth,
[`docs/wiki/Handicap-Mark-Math.md`](docs/wiki/Handicap-Mark-Math.md). It explains why a
smaller mark starts earlier, how rebasing preserves a race, and why a mark from one field
cannot simply be copied into another.

## Install the trusted V2 release

```bash
python -m pip install "strathmark @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
python -m pip install "strathmark[api] @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
```

The V2 tag is not a V3 installation. For development and rehearsal of V3, use an exact
authorized source commit and install its locked dependencies in an isolated environment.
Do not point a development checkout at a production database or treat installation as
production eligibility.

## V2 Python example

```python
from datetime import date

from strathmark import HandicapCalculator
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionContext,
    WoodProfile,
)

competitors = [
    CompetitorRecord(
        name="Alice",
        competitor_id="competitor-alice",
        gender="F",
        history=[HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1))],
    ),
    CompetitorRecord(
        name="Bob",
        competitor_id="competitor-bob",
        gender="M",
        history=[HistoricalResult("SB", 35.2, "Pine", 300, 5, date(2025, 3, 1))],
    ),
]

results = HandicapCalculator().calculate(
    competitors,
    WoodProfile(species="Pine", diameter_mm=300, quality=5),
    event_code="SB",
    context=PredictionContext(prediction_as_of=date(2026, 1, 1)),
)
for result in results:
    print(result.name, result.predicted_time, result.mark, result.interval)
```

This remains V2 behavior: one prior-only model, a deterministic joint optimizer, an
exclusive historical date cutoff, and five compatibility keys. Numeric LLM output is
retired in V2. These are versioned V2 facts, not V3 constraints.

V2 was the right correction for the evidence available in 2026: it removed temporal
leakage, unsafe cascade labels, and uncalibrated confidence while keeping race-day work
deterministic and offline. The pivot happened when the product contract expanded to a
live multi-round process that must compare independent methods, learn between rounds,
preserve component disagreement, and prepare the next field inside the show cadence.
Those requirements conflict with V2's deliberately single-authority, date-only receipt
shape, so V3 is a separate engine rather than a silent patch to V2.

## What V3 changes

V3 generates and preserves three independent numeric views of the same sealed evidence:

1. a transparent robust formula;
2. an independent hierarchical ML system; and
3. a blind council of two local LLM families plus one selected cloud model.

Valid forecasts are calibrated, scored against settled results, and pooled using
accuracy-earned weights. Disagreement remains visible as uncertainty and as a
green/amber/red consequence class. Every race—including each later-round field—is
reconstructed and rebased so its slowest expected competitor starts at Mark 3. Every
valid completion can update later rounds, while all heats in the same round share one
frozen evidence epoch.

This design makes coasting less useful without pretending a model can infer motive.
Overperformance and underperformance both affect capability evidence. Issued marks and
legal winners remain immutable.

### Why selection is competition-scoped

The earlier deployment design assumed one global V2-to-V3 endpoint cutover. The product
pivot keeps both engines explicit so judges can test V3 on deliberately chosen
competitions, compare real operating feedback, and still choose V2 elsewhere. Locking
the choice at the competition root prevents an event, heat, or final from changing its
numeric authority after evidence has begun to accumulate. This is an authority and
auditability change, not a change to how either engine calculates its own predictions.

Read [`docs/PREDICTION_ENGINE_V3.md`](docs/PREDICTION_ENGINE_V3.md) for the complete
mechanism, pivot rationale, REST surface, recovery rules, and eligibility gate.

## V3 consumer contract

V3 exposes a separate 18-path `/v3/*` service contract, version
`strathmark.v3-consumer-contract.v7`. The canonical installed
artifact is `strathmark/v3/contracts/v3_consumer.openapi.json`; its sibling SHA-256 file
freezes exact bytes. The tournament manager must pin both. The dedicated
`POST /v3/approvals/decide` route records one authenticated, idempotent decision over
multiple exact receipt ID/digest/revision bindings plus explicit exclusions. It is
separate from official issue acknowledgment.

`POST /v3/forecasts/pre-field` returns a signed, field-independent forecast set for
seeding or grouping competitors before stands and exact fields exist. Its receipt says
`purpose=pre_field_seeding_only` and `issued_mark=false`; it cannot be printed as a start
sheet or treated as a mark. Only `POST /v3/fields/assemble`, after the exact field and
roster have been synchronized, performs joint optimization and produces field-relative
marks.

Authenticated `GET /v3/status` publishes the current pre-field P-256 signer identity:
stable key ID, key class, provider, DER public key, canonical identity digest, and a
binding digest over that identity plus the exact source commit and installed consumer
contract. Consumers must validate that binding before trusting a signed pre-field
receipt; a key ID from the receipt alone is not a trust anchor.

STRATHMARK authenticates the calling service, not human roles. Upstream actor headers are
audit metadata. Human login, RBAC, official issue, results, publication, and payouts stay
in the tournament manager. Loopback is the default; non-loopback operation additionally
requires pinned mutual TLS.

See [`docs/STRATHEX_CONSUMER_MIGRATION.md`](docs/STRATHEX_CONSUMER_MIGRATION.md). Do not
switch a live consumer merely because the V3 endpoints import or the rehearsal passes.
This repository does not certify a consumer deployment. STRATHEX must still prove its
durable outbox, immutable acknowledgments, exact V7 pin, lifecycle orchestration, and
restart behavior in an installed-adapter rehearsal.

## Reproduce the V3 rehearsal

Use isolated paths:

```powershell
$env:STRATHMARK_TEST_DB = '1'
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\readme-v2.sqlite3"
$env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\readme-v3.sqlite3"
python scripts/replay_v3.py
python scripts/run_v3_release_evidence.py --local-model qwen3.5:9b --local-model ministral-3:8b
$source = (git rev-parse HEAD).Trim()
python scripts/verify_v3_release.py --evidence benchmarks/v3/v3_executable_evidence.json --emit-rehearsal $source --output-attestation benchmarks/v3/v3_release_attestation.json
python scripts/verify_v3_release.py
python scripts/verify_v3_release.py --require-production
```

Run the evidence command only from the exact committed candidate with the two pinned
local models installed. It builds and installs the wheel, executes all twelve proof
classes, and signs a source-bound evidence envelope. The ordinary verifier must then
pass. The production-required verifier must reject the ephemeral rehearsal with
`production_attestation_required`. These commands leave authority unchanged. Against the
missing, stale, failed, or tampered artifacts, even the ordinary verifier must fail closed.

## Documentation

- [Onboarding](ONBOARDING.md)
- [Handicap foundations](docs/wiki/Handicap-Mark-Math.md)
- [V3 engine](docs/PREDICTION_ENGINE_V3.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Deployment and eligibility](docs/DEPLOYMENT.md)
- [STRATHEX consumer migration](docs/STRATHEX_CONSUMER_MIGRATION.md)
- [Historical V2 engine](docs/PREDICTION_ENGINE_V2.md)
- [Changelog](CHANGELOG.md)

## License

[Apache 2.0](LICENSE)
