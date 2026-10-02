# FAQ

The current separate Linux competition profile (STRATHMARK 3.0.0rc4 / STRATHEX 7.3.0) supports explicit engine choice, real Formula/ML, local signed approval and issue, settlement, restart, and later-round learning. See the [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md). Windows CNG production qualification remains separate; the LLM council is unavailable in this local profile.

## Is V3 ready for official competitions?

No. V3.0.0rc3 is a release candidate that tracks all 232 in-repository
requirements. Core implementation and contract tests exist; installed lifecycle composition and qualification remain unfinished. V2
remains the globally trusted production authority and V3 is not production-eligible.
The checked-in development-key rehearsal is
source-bound and must pass the release verifier. No production authority has changed and no
consumer endpoint has switched. No production CNG identity is provisioned.

## Can Linux run V3 previews?

Yes. The [Linux numeric candidate](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_CANDIDATE.md) runs actual Formula, trained ML, pooling, and exact-field optimization with STRATHEX 7.2. It returns unissued previews and cannot approve, issue, record official results, settle, or learn at the next round.

## Why build formula, ML, and LLM forecasts?

They are independent views of the same sealed evidence. V3 compares and preserves all
three, learns which is accurate in context, and retains disagreement instead of hiding
it behind one selected number.

## Do marks carry from a heat into a final?

No. A displayed mark is relative to its field. V3 reconstructs every race from the
underlying evidence and rebases the slowest expected competitor in that field to Mark 3.
All heats in one round share one epoch; results become eligible at the next boundary.

## Does V3 change the winner after the race?

Never. Once a sheet is issued, its marks are immutable and the first legal completion
wins. V3 updates future capability evidence, not the placing.

## How does V3 address foxing or coasting?

Every valid completion counts, including overperformance and underperformance and work
by eliminated competitors. A surprising result changes uncertainty, review state, and
future capability through an auditable bounded mechanism. V3 does not claim to infer
motive or declare somebody dishonest.

## Who approves a sheet?

The tournament manager owns human login, roles, official issue, results, and payouts.
STRATHMARK authenticates one upstream service and returns numeric evidence plus a
green/amber/red review projection. The typed batch-approval endpoint records selected
and excluded receipt bindings; the separate issue endpoint freezes official issue.
Actor headers are audit metadata, not permissions.

## What if an assessor or network is unavailable?

The assessor abstains. Prepared valid evidence may still support a sheet, or the judge
must deliberately select a permitted non-predictive action. Local issue, lookup, and
settlement never require cloud or archive access.

## What remains true about V2?

V2 still uses its prior-only core, exclusive date cutoff, optional residual, five
compatibility keys, and narrative-only LLM integration. Those are V2 facts, preserved in
[Prediction Engine V2](Prediction-Engine-V2.md), not restrictions on V3.

## Can one tournament switch between V2 and V3?

No. A standalone event selects once at setup. A tournament selects once during creation,
and every event, heat, and later round inherits the choice. Different competition roots
may choose different eligible engines, but one root never mixes engines or silently
falls back after numeric work begins.

## Is a pre-field forecast a handicap mark?

No. It is signed field-independent raw-time evidence used to seed or group competitors
before fields and stands exist. Its receipt explicitly states
`purpose=pre_field_seeding_only` and `issued_mark=false`. V3 produces marks only after the
exact field has been synchronized and jointly assembled.

## What does the rehearsal prove?

A current rehearsal proves that its exact committed source, built and installed wheel,
machine, dependencies, commands, and twelve executable proof classes passed. It uses a
development ephemeral signing key and cannot authorize production. It becomes stale
when the source changes, and the production verifier deliberately rejects it.

The focused five-run result-to-ready benchmark recorded a post-format 3.414-second
maximum. It is one source-bound component of the full rehearsal.

## Is STRATHEX ready to consume V3 approvals?

STRATHMARK exposes the required V7 endpoints, but this repository does not certify the
external adapter. Its installed rehearsal must prove the exact V7 pin, competition
selection and inheritance, pre-field/field boundary, durable outbox, idempotent
forwarding, immutable acknowledgment, and restart recovery.
