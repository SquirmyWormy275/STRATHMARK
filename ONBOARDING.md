# Onboarding

## Current status

V3 is a `3.0.0rc1` release candidate in a separate namespace that tracks
all 232 in-repository requirements. Repository implementation and audit are complete for
this candidate. The checked-in development-key rehearsal is source-bound and must pass
the release verifier; it is not production evidence. V2 remains the globally trusted
production authority. V3 is not production-eligible. No production authority has
changed, and V2 is not audit-only.
No production CNG identity is provisioned. Never turn a rehearsal attestation into a
production-readiness claim.

The normal package and trusted V2 engine support Python 3.10-3.13. V3 race-day
execution, migrations, tests, and release evidence require the designated Python 3.13
environment and exact V3 release lock. Do not run V3 authority on an older interpreter
or enable SQLite `trusted_schema` to work around an incompatible bundled SQLite build.

## Mandatory domain gate

Read [`docs/wiki/Handicap-Mark-Math.md`](docs/wiki/Handicap-Mark-Math.md) in full before
analyzing or changing predictions, marks, sheets, simulations, results, or their
documentation. It defines the sport: smaller marks start earlier, marks are start counts,
rebasing is a field-wide translation, and marks copied from independently rebased fields
are not comparable ability evidence. Association-specific procedures do not replace that
foundation.

## Read in this order

1. [`docs/wiki/Handicap-Mark-Math.md`](docs/wiki/Handicap-Mark-Math.md) — timeless domain meaning.
2. [`README.md`](README.md) — repository status and entry points.
3. [`docs/PREDICTION_ENGINE_V3.md`](docs/PREDICTION_ENGINE_V3.md) — V3 release-candidate contract and pivot.
4. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — V2/V3 boundaries and data flow.
5. [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — rehearsal, recovery, and eligibility gates.
6. [`docs/STRATHEX_CONSUMER_MIGRATION.md`](docs/STRATHEX_CONSUMER_MIGRATION.md) — consumer handoff.
7. [`docs/PREDICTION_ENGINE_V2.md`](docs/PREDICTION_ENGINE_V2.md) — preserved V2 contract.

## Mental model

V2 is the currently trusted production engine. It uses one prior-only statistical core,
one exclusive date cutoff, an optional residual, five compatibility keys, and a joint
mark optimizer. Those constraints remain exact for V2 and its receipts.

V3 is the successor release candidate. Formula, hierarchical ML, and a three-member LLM
council independently forecast the same sealed evidence. Accuracy-earned credibility
weights pool the valid distributions. Every race is reconstructed and rebased to Mark 3;
one round shares one frozen evidence epoch; every valid completion becomes eligible at
the next round boundary. Consequential disagreement is surfaced as green, amber, or red
for deliberate judge action.

The local factory composition/scheduler is runnable, and its bounded evaluator
entrypoint opens an existing production CNG key by name. Concrete formula/ML/LLM family
executors, the local settled-evidence metric evaluator, installation OS identities and
ACLs, and provisioned CNG keys are not supplied by that composition. Promotion remains
manual and signed.

V3 authenticates one calling service, not human roles. Tournament-manager login, RBAC,
official issue, results, publication, and payouts remain upstream. Once inside STRATHMARK,
the authenticated service principal has full V3 authority; actor headers are audit data.
The frozen V6 contract has 18 paths. It includes lifecycle and snapshot routes, a
field-independent pre-field forecast route, and typed approval decisions. A pre-field
receipt is signed seeding evidence with `issued_mark=false`; it never issues a start
mark. Only exact-field assembly can produce marks. Approval evidence remains separate
from official issue.

Engine eligibility and competition selection are separate. The current pivot replaces
the earlier global-switch assumption with one immutable selection per standalone event
or tournament root. Tournament children inherit it. Different roots may use different
eligible engines, but one root never mixes engines or falls back silently. V2 remains
globally authoritative while V3 production eligibility is absent.

## Code map

| Responsibility | Source |
| --- | --- |
| Closed contracts and canonicalization | `strathmark/v3/contracts/` |
| Formula, ML, council, validation | `strathmark/v3/assessors/` |
| Capability, credibility, pooling, disagreement, optimization | `strathmark/v3/domain/` |
| Commands, rolling cards, approval, issue, settlement, factory | `strathmark/v3/application/` |
| Live Formula/ML/LLM execution and durable replay | `strathmark/v3/application/forecast_runtime.py` |
| Atomic settlement reaction closure | `strathmark/v3/application/settlement_reactions.py` |
| SQLite authority, providers, artifacts, backup/recovery | `strathmark/v3/infrastructure/` |
| Service auth and frozen REST surface | `strathmark/v3/api/` |
| Side-effect-free environment snapshot | `strathmark/v3/composition.py` |
| V3 local migrations | `strathmark/v3/migrations/` |
| Rehearsal and release verification | `scripts/replay_v3.py`, `scripts/run_v3_release_evidence.py`, `scripts/verify_v3_release.py` |
| Historical production engine | `strathmark/prediction_v2.py`, `strathmark/calculator.py` |

Importing `strathmark.v3` performs no I/O, opens no database, loads no model, starts no
thread, and makes no network request. Runtime construction is explicit and injected.

## Non-negotiable rules

- Never write a test, replay, training run, or rehearsal against production data.
- Set unique `STRATHMARK_DB_PATH`, `STRATHMARK_V3_DB_PATH`, and pytest `--basetemp`
  before collection; set `STRATHMARK_TEST_DB=1`.
- Preserve V2 receipts and behavior. V3 never rewrites them or hides behind V2 keys.
- Require one deliberate engine selection at the competition root. Never default it,
  expose a child override, or mix receipts from both engines in one scope.
- Use pre-field forecasts only for seeding/grouping. They do not contain marks; assemble
  the synchronized exact field before displaying or issuing any V3 mark.
- Do not copy displayed marks between fields. Reconstruct and rebase the complete field.
- Same-round fields share one epoch; results affect only a later round.
- Both overperformance and underperformance update capability without inferring motive.
- Issued marks and legal winners are immutable.
- An unavailable assessor abstains. Never relabel or silently substitute it.
- Local numeric authority must not depend on Ollama, cloud, or archive availability.
- Rehearsal keys and ephemeral signers cannot authorize production.
- Failed V3 eligibility preparation leaves V2 authoritative or declares
  traditional/manual authority if V2 cannot serve.
- No code, document, test result, or model output grants official competition authority.

## Isolated verification

```powershell
$env:STRATHMARK_TEST_DB = '1'
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\onboarding-v2.sqlite3"
$env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\onboarding-v3.sqlite3"
python -m pytest tests/v3 -q --basetemp .tmp/onboarding-v3 -p no:cacheprovider
python scripts/replay_v3.py
python scripts/run_v3_release_evidence.py --local-model qwen3.5:9b --local-model ministral-3:8b
$source = (git rev-parse HEAD).Trim()
python scripts/verify_v3_release.py --evidence benchmarks/v3/v3_executable_evidence.json --emit-rehearsal $source --output-attestation benchmarks/v3/v3_release_attestation.json
python scripts/verify_v3_release.py
python scripts/verify_v3_release.py --require-production
```

Generate evidence only from a committed, unchanged candidate with the pinned local
models installed. The ordinary verifier must reject missing, stale, failed, substituted,
or tampered evidence. After a fresh rehearsal is emitted, the last command must still
refuse it. A passing production-required check is valid only with a separately created
production CNG-backed attestation and does not itself switch consumer authority.

The source-bound, post-format five-run Windows result-to-ready benchmark recorded a
maximum of 3.414 seconds against the 120-second limit. That component result does not
replace the final exact-wheel, rehearsal, production-identity, or eligibility gates.

For the preserved V2 suite, use another unique database and base directory:

```powershell
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\onboarding-v2-suite.sqlite3"
python -m pytest tests -q --ignore=tests/v3 --basetemp .tmp/onboarding-v2-suite -p no:cacheprovider
```

## Common work

When changing a contract, update the frozen OpenAPI document and checksum, installed-
wheel proof, examples, consumer migration, and release evidence together. When changing
prediction behavior, add RED evidence first and preserve causal cutoffs, immutable source
forecasts, deterministic replay, and the domain invariants above. When changing storage,
keep the event log authoritative, projections rebuildable, exact retry idempotent, and
the optional archive non-blocking.

The dated plans and `docs/solutions/` explain historical decisions. Check their status:
V2's retired numeric cascade and V2-only limitations are evidence about their versions,
not unqualified statements about V3.
