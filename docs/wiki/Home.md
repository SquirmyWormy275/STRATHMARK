# STRATHMARK Wiki

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and must pass the release verifier. V2 remains the trusted
production authority until an explicit cutover. No production authority has changed,
no consumer endpoint has switched, and V2 is not audit-only. No production CNG identity
is provisioned. The external STRATHEX durable outbox/adapter is not implemented.

STRATHMARK is an offline-capable woodchopping prediction and handicap-mark system. V3
adds independent formula, hierarchical ML, and numeric LLM-council forecasts; automatic
accuracy-earned weighting; full-field Mark-3 rebasing; between-round capability updates;
and a fast exception-first judge workflow. The local event store remains race-day
numeric authority and cloud services are never required to issue or settle a race.

The factory composition/scheduler and bounded CNG evaluator entrypoint are runnable
seams. Concrete family executors, local settlement metrics, production OS/CNG identities,
exact-source CI, external STRATHEX forwarding, and cutover remain explicit gates.

## Start here

1. [Handicap foundations](Handicap-Mark-Math.md) — mandatory timeless domain reading.
2. [Prediction Engine V3](Prediction-Engine-V3.md) — successor release candidate and pivot.
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
- The ten-route V3 contract records typed multi-receipt approval decisions separately
  from issue acknowledgment. STRATHEX still needs its durable forwarding outbox.
- A rehearsal attestation is not a production attestation and never switches authority.

The post-format five-run Windows result-to-ready benchmark recorded a 3.414-second
maximum against the 120-second limit. It is focused performance evidence, not final
exact-wheel or production evidence.

The canonical repository documentation is [Onboarding](../../ONBOARDING.md) and
[Prediction Engine V3](../PREDICTION_ENGINE_V3.md).
