# FAQ

## Is V3 live?

No. V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. V2 remains the trusted production
authority until an explicit cutover. The checked-in development-key rehearsal is
source-bound and must pass the release verifier. No production authority has changed and no
consumer endpoint has switched. No production CNG identity is provisioned.

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

## What does the rehearsal prove?

A current rehearsal proves that its exact committed source, built and installed wheel,
machine, dependencies, commands, and twelve executable proof classes passed. It uses a
development ephemeral signing key and cannot authorize production. It becomes stale
when the source changes, and the production verifier deliberately rejects it.

The focused five-run result-to-ready benchmark recorded a post-format 3.414-second
maximum. It is one source-bound component of the full rehearsal.

## Is STRATHEX ready to consume V3 approvals?

Not yet. STRATHMARK's typed multi-receipt endpoint exists, but STRATHEX still needs a
durable outbox that commits the upstream decision with its delivery record, forwards the
exact request idempotently, and stores the immutable acknowledgment.
