<p align="center">
  <img src="https://raw.githubusercontent.com/SquirmyWormy275/STRATHMARK/main/assets/strathmark_logo.png" alt="STRATHMARK" width="480"/>
</p>

# STRATHMARK

STRATHMARK is an offline-capable woodchopping prediction and handicap-mark engine.
It preserves the released V2 engine and contains the implemented V3 adaptive ensemble.

## Current release state

V3 is an implemented release candidate under whole-system verification. Its executable
evidence must be regenerated after the candidate source is committed; the older
checked-in attestation is intentionally stale and cannot verify the current tree. V2
remains the trusted production authority until an explicit cutover. No production
authority has changed, no endpoint has switched, and V2 is not audit-only.

The distinction matters:

- **V2.0.0** is the immutable released and currently trusted production engine.
- **V3 release candidate** has separate code, contracts, storage, and tests. Exact-wheel
  Windows evidence and a development-key rehearsal attestation are candidate outputs,
  not permanent claims that survive a source change.
- **Production V3** does not exist until a non-exportable Windows CNG identity signs the
  exact production evidence and a separately authorized zero-open-tournament handoff is
  completed.

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
cutover.

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

Read [`docs/PREDICTION_ENGINE_V3.md`](docs/PREDICTION_ENGINE_V3.md) for the complete
mechanism, pivot rationale, REST surface, recovery rules, and cutover gate.

## V3 consumer contract

V3 exposes a separate ten-route `/v3/*` service contract. The canonical installed
artifact is `strathmark/v3/contracts/v3_consumer.openapi.json`; its sibling SHA-256 file
freezes exact bytes. The tournament manager must pin both.

STRATHMARK authenticates the calling service, not human roles. Upstream actor headers are
audit metadata. Human login, RBAC, official issue, results, publication, and payouts stay
in the tournament manager. Loopback is the default; non-loopback operation additionally
requires pinned mutual TLS.

See [`docs/STRATHEX_CONSUMER_MIGRATION.md`](docs/STRATHEX_CONSUMER_MIGRATION.md). Do not
switch a live consumer merely because the V3 endpoints import or the rehearsal passes.

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
local models installed. It builds and installs the wheel, executes all eleven proof
classes, and signs a source-bound evidence envelope. The ordinary verifier must then
pass. The production-required verifier must reject the ephemeral rehearsal with
`production_attestation_required`. These commands leave authority unchanged. Against the
current stale or missing artifacts, even the ordinary verifier must fail closed.

## Documentation

- [Onboarding](ONBOARDING.md)
- [Handicap foundations](docs/wiki/Handicap-Mark-Math.md)
- [V3 engine](docs/PREDICTION_ENGINE_V3.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Deployment and cutover](docs/DEPLOYMENT.md)
- [STRATHEX consumer migration](docs/STRATHEX_CONSUMER_MIGRATION.md)
- [Historical V2 engine](docs/PREDICTION_ENGINE_V2.md)
- [Changelog](CHANGELOG.md)

## License

[Apache 2.0](LICENSE)
