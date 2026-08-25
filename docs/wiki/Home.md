# STRATHMARK Wiki

## Current authority status

V3 is an implemented release candidate under exact-source verification. Its older
rehearsal is stale after current source changes. V2 remains the trusted production
authority until an explicit cutover. No production authority has changed, no consumer
endpoint has switched, and V2 is not audit-only.

STRATHMARK is an offline-capable woodchopping prediction and handicap-mark system. V3
adds independent formula, hierarchical ML, and numeric LLM-council forecasts; automatic
accuracy-earned weighting; full-field Mark-3 rebasing; between-round capability updates;
and a fast exception-first judge workflow. The local event store remains race-day
numeric authority and cloud services are never required to issue or settle a race.

## Start here

1. [Handicap foundations](Handicap-Mark-Math.md) — mandatory timeless domain reading.
2. [Prediction Engine V3](Prediction-Engine-V3.md) — implemented successor and pivot.
3. [Architecture](Architecture-Overview.md) — V2/V3 system boundaries.
4. [Deployment](Deployment.md) — rehearsal, recovery, and cutover.
5. [STRATHEX consumer](STRATHEX-Consumer.md) — upstream authority and adapter contract.
6. [Historical V2 engine](Prediction-Engine-V2.md) — production engine until cutover.

## What must not be confused

- A smaller mark starts earlier; a larger mark waits longer.
- A displayed mark is field-relative. Reconstruct and rebase every later-round field.
- Same-round heats use one epoch; results enter at the next round boundary.
- Both faster and slower valid performances update evidence, but no model infers motive.
- Once a sheet is issued, the first legal completion wins. No adjusted placing exists.
- STRATHMARK authenticates one upstream service. Human RBAC and official results remain
  in the tournament manager.
- A rehearsal attestation is not a production attestation and never switches authority.

The canonical repository documentation is [Onboarding](../../ONBOARDING.md) and
[Prediction Engine V3](../PREDICTION_ENGINE_V3.md).
