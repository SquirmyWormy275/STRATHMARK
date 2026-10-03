# Deployment and Recovery

Under the Windows CNG qualification policy, V3 is not production-eligible. Its release candidate rehearsal evidence is source-bound and does not grant Windows production authority. V2 remains the established production baseline; the completed Linux local profile uses its separate operator policy.

The current separate Linux competition profile (STRATHMARK 3.0.0rc4 / STRATHEX 7.3.2) supports explicit engine choice, real Formula/ML, local signed approval and issue, settlement, restart, and later-round learning. See the [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md). Windows CNG production qualification remains separate; the LLM council is unavailable in this local profile.

The retained preview profile, [Linux numeric candidate](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_CANDIDATE.md) runs Formula, trained ML, pooling, and optimization for unissued previews. It does not support approval, issue, settlement, next-round learning, or production qualification.

## Current authority status

STRATHMARK 3.0.0rc4 provides the complete separate Linux local competition profile: real Formula/ML, deliberate V2/V3 choice, exact fields, approval, separate issue, judge-authorized outcomes, settlement, restart, corrections, verified independent recovery, and later-round learning. The [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md) describes its operator policy and persistent local signer. The LLM council is unavailable in this profile. V2 remains the established production baseline. Windows V7 CNG and full three-assessor factory qualification remain separate installation gates; ephemeral rehearsal signatures do not satisfy them.

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

Production eligibility requires installation-owned non-exportable Windows CNG keys, the
exact production evidence set, a zero-open-tournament V2 freeze, resolution of all
ambiguous work, a signed final V2 manifest, initialized V3 verification, an
installed-consumer rehearsal, a signed pre-switch handoff, and separate authorization
to enable V3 as a choice. That does not select V3 globally or remove V2. Each new
standalone event or tournament root deliberately selects one eligible engine, and the
other engine is never its automatic fallback.

The V7 installed rehearsal must cover pre-field seeding receipts that issue no marks,
exact-field assembly that does, immutable tournament inheritance, exact retries, and
restart recovery.

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

See the canonical [deployment runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md).
