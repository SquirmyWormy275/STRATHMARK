# Competition-Scoped Prediction Engine Authority

- **Status:** Accepted
- **Date:** 2026-08-27
- **Repositories:** STRATHMARK API producer and STRATHEX judge-facing consumer

## Context

The original V3 release design treated adoption as one global cutover: freeze V2,
qualify V3, switch the consumer once, and retain V2 only as historical evidence. That
remains useful release evidence, but it does not fit field evaluation. Judges need to
run real competitions on either engine, compare complete operational outcomes, and keep
one tournament coherent across heats and finals.

A UI-only selector would be false. STRATHEX must persist the judge's choice and route
all numeric work, while STRATHMARK must bind V3 lifecycle evidence to that exact choice.
Availability also cannot decide authority after the fact: silently switching engines on
timeout would mix incompatible prediction and receipt semantics inside one competition.

## Decision

Prediction-engine authority is scoped to one competition root.

- A standalone event owns one deliberate V2 or V3 selection.
- A tournament owns one selection at creation; all child events and rounds inherit it.
- No engine is selected by default and no child tournament event can override the root.
- The selected engine supplies all authoritative numeric work for that scope.
- Failure, timeout, incompatibility, or degraded readiness blocks; it never invokes the
  unselected engine.
- Separate roots may select different eligible engines concurrently.

STRATHEX owns the human choice, persistence, inheritance, and judge display. STRATHMARK
owns engine eligibility, numeric execution, V3 lifecycle evidence, and receipts. Human
actor fields received by STRATHMARK are audit metadata; service credentials remain the
authorization boundary.

The closed selection fact binds the tournament identity, requested engine, execution
mode, selecting actor metadata, timestamp, reason code, consumer-contract digest, and
exact source commit. A selected V3 tournament embeds it in the immutable tournament-open
event. That event is the V3 lock evidence. A V2-selected fact or a selection for another
scope is rejected before V3 writes anything. Exact command retries return the original
event; changed selection payloads conflict.

Existing internal V3 events without a selection remain readable and replayable. New
public consumer entry points will require the explicit fact. Production qualification
continues to require separately verified installation evidence; a rehearsal selection
does not become production merely because a judge chose V3.

## Consequences

- V2 remains a complete supported engine instead of becoming globally audit-only.
- The former global cutover machinery is interpreted as V3 production-installation
  eligibility, not as the selector for every competition.
- A scope never combines V2 and V3 receipts, forecasts, or marks.
- Switching engines after authority locks requires terminating the old unissued scope
  under the later abandonment contract and creating a new scope identity. Issued and
  resulted history is immutable.
- Cross-engine evaluation compares completed scopes after the fact; it does not
  retroactively change winners or award a separate "best handicap" result.

## Rejected alternatives

- **Global V3 cutover:** prevents controlled real-user comparison and unnecessarily
  removes V2 from new competitions.
- **Per-event choice inside a tournament:** can mix evidence and prediction semantics
  between heats and finals.
- **Preview-only V3:** does not test the actual judge workflow or authoritative marks.
- **Automatic fallback:** changes authority because of an outage and makes the audit
  trail ambiguous.
- **Mutable in-place selection:** permits already calculated evidence to be relabeled.

## Verification

Contract tests prove closed serialization and malformed-value rejection. Lifecycle
tests prove an exact V3 selection is embedded once, exact retry is stable, changed retry
conflicts, V2 and cross-scope facts write nothing to V3, and distinct scope selections
have distinct identities. Historical release manifests and cutover tests remain
unchanged and verifiable.
