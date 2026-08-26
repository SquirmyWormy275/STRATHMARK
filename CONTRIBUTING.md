# Contributing to STRATHMARK

STRATHMARK supplies numeric handicap evidence to tournament software. V3.0.0rc1 is an
a release candidate that tracks all 232 in-repository requirements;
implementation is under final audit. Its checked-in evidence is stale rehearsal
evidence until regenerated on the final documentation commit. V2 remains the trusted
production authority until explicit cutover. Changes here can affect live start sheets,
so domain, causality, authority, isolation, and recovery evidence are mandatory.

Read [`docs/wiki/Handicap-Mark-Math.md`](docs/wiki/Handicap-Mark-Math.md), then
[`ONBOARDING.md`](ONBOARDING.md), before changing prediction or mark behavior.

## Development install

```bash
pip install -e ".[dev,api]"
```

Optional extras are `ml`, `llm`, `db`, and `security`. Installing a library grants no
numeric or production authority. V2 uses LLM integrations only for narrative features;
V3 uses pinned local/cloud candidates only through its deterministic wrappers and signed
factory lifecycle.

## Test isolation

Never allow collection or a test to open production data. Use unique writable paths:

```powershell
$env:STRATHMARK_TEST_DB = '1'
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\contrib-v2.sqlite3"
$env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\contrib-v3.sqlite3"
python -m pytest tests/v3 -q --basetemp .tmp/contrib-v3 -p no:cacheprovider
```

Use a different V2/V3 database pair and `--basetemp` for every concurrent run. Provider
and PostgreSQL live tests require explicit disposable/local opt-in and never replace
deterministic fakes or static migration gates.

## Required workflow

For behavior changes, prove RED first, implement the smallest coherent fix, refactor,
then run focused, adjacent, full isolated, lint/format, replay, release-verifier,
distribution-build, and installed-wheel checks in proportion to risk. Preserve unrelated
dirty work and stage only intended files.

Contract changes must update code, frozen OpenAPI bytes and checksum, installed package,
consumer migration, documented examples, and release evidence together. Prediction
changes must preserve sealed cutoffs/epochs, immutable component forecasts, deterministic
replay, and issued-result authority. Storage changes must preserve append-only authority,
exact retry, expected versions, projection rebuild, and non-blocking archive behavior.

## Formatting

```bash
ruff check .
ruff format --check .
git diff --check
```

## Architecture rules

- A smaller mark starts earlier; every new field is reconstructed and rebased.
- Same-round fields share one evidence epoch; results affect later rounds only.
- Both overperformance and underperformance update evidence without inferring motive.
- Formula, ML, and LLM outputs remain independent and auditable in V3.
- An unavailable assessor abstains; it is never relabeled.
- Issued marks and legal winners never change after the race.
- STRATHMARK service authentication is not tournament-manager human RBAC.
- Local issue/lookup/settlement cannot require provider or archive availability.
- Ephemeral keys and rehearsal evidence cannot satisfy production gates.
- No successful check, commit, or installation implicitly authorizes cutover.

All PRs must pass CI before merge. Merge, deployment, production migration, credential
provisioning, model promotion, and authority cutover remain distinct authorized actions.
