# Deployment and Recovery

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. The
checked-in development-key rehearsal is source-bound and must pass the release verifier. V2 remains the
trusted production authority until an explicit cutover. No production authority has
changed, no consumer endpoint has switched, and V2 is not audit-only. No production CNG
identity is provisioned. The external STRATHEX durable outbox/adapter is not implemented.

V3 deployment and rehearsal require Python 3.13 with the exact V3 release lock. The
normal package and trusted V2 engine continue to support Python 3.10-3.13. Do not weaken
SQLite `trusted_schema` to make an older interpreter's bundled SQLite accept V3 schema
objects.

Use isolated V2/V3 database paths and STRATHMARK_TEST_DB=1 for every test or rehearsal.
Generate executable evidence only from the exact committed candidate, its built and
installed wheel, and the pinned local models. The ordinary verifier must reject stale or
missing evidence; after a fresh ephemeral rehearsal is emitted it must pass, while the
production-required verifier must reject it with production_attestation_required.
Neither result changes authority.

Production requires installation-owned non-exportable Windows CNG keys, the exact
production evidence set, a zero-open-tournament V2 freeze, resolution of all ambiguous
work, a signed final V2 manifest, initialized V3 verification, an installed-consumer
rehearsal, a signed pre-switch handoff, and separate authorization for the final endpoint
switch. Any preparation failure resumes V2 or declares traditional/manual authority.
There is no automatic V2 numeric fallback after a V3 cutover.

Production verification also requires an operator-pinned public release identity passed
with `--trusted-production-identity`. The verifier never treats signer metadata embedded
in the attestation under review as its trust root.

The runnable factory scheduler and bounded CNG evaluator entrypoint do not supply
production algorithms or authority. Install the concrete local formula/ML/LLM family
executors and authenticated settlement-metric evaluator, provision separate OS
identities/ACLs and CNG keys, and exercise that boundary in exact-source CI before
accepting factory evidence.

The focused post-format result-to-ready benchmark completed five Windows trials with a
3.414-second maximum against the 120-second limit. It is one part of the complete
source-bound release evidence.

See the canonical [deployment runbook](../DEPLOYMENT.md).
