# STRATHMARK Wiki

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and must pass the release verifier.
V2 remains the globally trusted production authority. V3 is not production-eligible,
V2 is not audit-only, and no production CNG identity is provisioned.

The current product model selects one eligible engine per competition root rather than
performing one global replacement. A standalone event selects once; a tournament selects
once and every child inherits. Separate roots may use different eligible engines, but a
root never mixes V2 and V3 or silently falls back.

STRATHMARK is an offline-capable woodchopping prediction and handicap-mark system. V3
adds independent formula, hierarchical ML, and numeric LLM-council forecasts; automatic
accuracy-earned weighting; full-field Mark-3 rebasing; between-round capability updates;
and a fast exception-first judge workflow. The local event store remains race-day
numeric authority and cloud services are never required to issue or settle a race.

The factory composition/scheduler and bounded CNG evaluator entrypoint are runnable
seams. Concrete family executors, local settlement metrics, production OS/CNG identities,
exact-source CI, external STRATHEX forwarding, and production eligibility remain explicit
gates.

## Start here

1. [Handicap foundations](Handicap-Mark-Math.md) — mandatory timeless domain reading.
2. [Prediction Engine V3](Prediction-Engine-V3.md) — successor release candidate and pivot.
3. [Competition engine selection](Competition-Engine-Selection.md) — one immutable choice per root.
4. [Architecture](Architecture-Overview.md) — V2/V3 system boundaries.
5. [Deployment](Deployment.md) — rehearsal, recovery, and eligibility.
6. [STRATHEX consumer](STRATHEX-Consumer.md) — upstream authority and adapter contract.
7. [Historical V2 engine](Prediction-Engine-V2.md) — preserved trusted V2 contract.

## What must not be confused

- A smaller mark starts earlier; a larger mark waits longer.
- A displayed mark is field-relative. Reconstruct and rebase every later-round field.
- Same-round heats use one epoch; results enter at the next round boundary.
- Both faster and slower valid performances update evidence, but no model infers motive.
- Once a sheet is issued, the first legal completion wins. No adjusted placing exists.
- STRATHMARK authenticates one upstream service. Human RBAC and official results remain
  in the tournament manager.
- The V6, 18-path contract separates pre-field seeding, exact-field marks, approval, and
  issue. Pre-field forecasts are signed but always say `issued_mark=false`.
- A rehearsal attestation is not a production attestation and never switches authority.

The post-format five-run Windows result-to-ready benchmark recorded a 3.414-second
maximum against the 120-second limit. It is focused performance evidence, not final
exact-wheel or production evidence.

The canonical repository documentation is [Onboarding](../../ONBOARDING.md) and
[Prediction Engine V3](../PREDICTION_ENGINE_V3.md).
