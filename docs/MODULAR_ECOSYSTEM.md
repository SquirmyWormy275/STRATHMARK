# Modular tournament agreement

Approved implementation direction, 2026-10-15. Missoula [PR 125](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/pull/125)
retains its independent backup/recovery merge gate. Execution evidence lives in
[Missoula's status record](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/blob/feature/modular-events-20261015/docs/MODULAR_EXECUTION_STATUS.md).

STRATHMARK has two distinct boundaries. Calculation-only clients retain their
own event workflow. Delegated clients use `strathmark.events` for persistent
fields, review, issue, outcomes, advancement and correction. Presentations call
these operations; they do not calculate their own marks or progression. Existing
V2, V3 Linux and frozen V7 interfaces retain their contracts.

Missoula owns registration, event entries, whole-show scheduling, physical
resources, points and payouts. A managed event has one STRATHMARK authority and
an explicit host mapping, with every heat/attempt retained separately from the
host summary. The host publishes accepted source revisions to MNEMEX. A
standalone event declares its source show and is its own publisher.

MNEMEX owns permanent identity, consent and reconciled career history. Its
internal person IDs, pairwise show references and numerical pseudonyms remain
separate. Reviewed historical snapshots remain explicit inputs; no live MNEMEX
connection is required to run an event. No name-based consequential linking.

The first managed contract is bounded to synthetic SB/UH handicap events using
the real V2 predictor. It is not a replacement for the installed Linux V3
competition lifecycle or a production qualification. Unsupported profiles fail
explicitly, without runtime or transport fallback. Local operations support
Python 3.10–3.13; the existing V3 authority still requires its separate 3.13
environment. GUI dependencies are optional.

## Numerical policy

Numerical engine and mark policy are immutable at the tournament root. Events
and rounds inherit that selection; children cannot substitute another policy.
`v2-optimizer.v1` retains the existing joint optimizer and rounded/clipped
fallback (historical system ceiling 183). The new `mark3-ceil180.v1` policy
uses unchanged V2 raw-time predictions and explicitly computes
`3 + ceil(slowest_prediction - prediction)`. Its legal marks are integers
3–180. An excessive gap blocks preparation; it is never silently compressed.
These policies are distinctly labeled in evidence. Extraction does not edit
the predictor or optimizer.

V2 quality is recorded as known/unknown but has no numerical effect. A request
requiring quality-sensitive predictions is unsupported. V2 history is strictly
before its exclusive date cutoff; later-round completions are retained but do
not become V2 evidence. V3's supported round learning remains version-specific.
Missoula's retained reviewed zero marks remain valid; managed Mark-3 policies
reject zero. No numeric LLM participates in this managed profile.

## Authority and recovery

Every mutation binds a stable request ID, canonical payload, event revision,
authenticated principal and editor identity. Exact retries return the original
receipt before checking a stale version; changed retries conflict. Editor
handover changes event revision but not physical-field identity. Issue freezes
the sheet. Result corrections append revisions and invalidate unissued
descendants; issued descendants require an explicit review and cannot be
silently replaced. Director retention records its reason and exact result/issue
bindings; finalization requires every latest summary result to be accepted. Host acceptance, publication and payout settlement are
separate from event finalization.

SQLite transactions commit state, revision history, accepted command receipts
and export intentions together. Recovery must compare an independently retained
authority head; a copied old database cannot declare itself current. A backup
is not permission to create a second active authority. Hosted operation uses
the same operations with scoped authentication, not the broad V3 service
credential as browser authority.

New managed events remain synthetic-only and default-off in Missoula until
installation, numerical, signing, restore and operator gates are met. A code
merge or package release does not enable official use. Issued sheets must be
saved independently before racing. During an outage, officials may record
outcomes against the exact saved issue identity and reconcile later. Unissued
fields pause; traditional manual authority needs a separately recorded director
decision, never an engine-issued receipt or automatic cloud/local failover.
