# Offline Evidence Snapshot Runbook

Use this runbook before a trusted shadow event. It provisions the dated prior
history that STRATHMARK will use while offline. A model artifact is not a history
snapshot: both must be present locally before race-day operation.

This workflow is deliberately operator-triggered. Calculation, receipt recovery,
and freshness checks never call the source adapter or a cloud service. They keep
using the active SQLite snapshot until another successful refresh atomically
appends a verified activation revision.

## Safety boundary

- Use the durable, backed-up, single-writer SQLite file selected by
  `STRATHMARK_DB_PATH`. Ephemeral and multi-writer SQLite deployments are not
  supported for trusted shadow operation.
- The source export must contain pseudonymous, namespaced stable IDs. Do not put
  display names, contact details, medical information, fatigue notes, or other free
  text in the export.
- A row must have a dated result strictly before the explicit UTC cutoff. Undated,
  same-day, later, future, malformed, duplicate, and out-of-bound rows are excluded
  and counted without retaining their content.
- Refresh tests and rehearsals use a temporary SQLite file and an in-process source
  double. Do not point a test at an operator or production database.
- Optional Supabase mirroring is unrelated to this refresh. It may be absent or
  offline and must not be added to a source adapter used during race-day calculation.

## Versioned source envelope

The adapter returns `EvidenceSnapshotPayload` with schema
`strathmark.evidence-snapshot-source.v1`. Each row uses
`strathmark.evidence-history-row.v1` and only these fields:

| Field | Rule |
| --- | --- |
| `competitor_id` | namespaced stable pseudonymous identity |
| `event_code` | `SB` or `UH` |
| `time_seconds` | finite result within configured model bounds |
| `species` | bounded code, not free text |
| `diameter_mm` | finite value within configured model bounds |
| `quality` | integer 1 through 10; retained for compatibility but inactive in V2 |
| `competition_id` | namespaced stable tournament/source identity |
| `heat_id` | optional namespaced stable heat identity |
| `result_date` | ISO date strictly before the envelope cutoff |

The envelope also carries a namespaced `source_id`, the exact exclusive UTC
`cutoff`, a timezone-aware `captured_at`, and a lowercase SHA-256 `source_digest`.
Compute that digest with `canonical_evidence_source_digest`; do not invent a second
serialization. `captured_at` may be no more than 300 seconds ahead of the local UTC
clock. A later value is rejected during refresh and treated as integrity failure if
encountered in persisted data; negative age is never silently clamped into readiness.
The adapter row sequence is traversed and projected exactly once. Cardinality,
source-digest verification, row validation, and persistence all use that same frozen
tuple, so a mutable or stateful adapter cannot verify one export and persist another.

## Refresh procedure

1. Stop or quiesce trusted calculation writes so one operator owns the refresh.
2. Back up the durable SQLite file.
3. Export prior results from the approved source using stable IDs and an explicit
   exclusive UTC cutoff.
4. Verify the export outside STRATHMARK according to the tournament's source-control
   procedure. Construct the versioned payload and its canonical source digest.
5. Read and record the currently verified active digest, or `None` for the first
   import. Call `ResultStore.refresh_evidence_snapshot(source, cutoff=cutoff,
   expected_active_snapshot_digest=recorded_digest)` once. The source object must
   implement `load_snapshot(*, cutoff)`; that method is called only during this
   deliberate step. An exact retry of the already-active snapshot is idempotent.
   A competing different refresh fails closed with `EvidenceSnapshotConflictError`
   and leaves no orphan snapshot.
6. Inspect the returned `EvidenceSnapshotStatus`. Record at minimum:
   `snapshot_digest`, `source_digest`, `source_id`, `cutoff`, `captured_at`,
   `completeness`, supplied/accepted/rejected counts, diagnostic counts, age,
   freshness, integrity, and the prior digest it supersedes.
7. Investigate every rejected-row category in the source system. The status never
   includes rejected row content, so correction happens in the controlled source,
   followed by another explicit refresh.
8. Restart STRATHMARK and read `get_evidence_snapshot_status()` again. The digest,
   counts, source, cutoff, and completeness must match. Run one isolated preflight
   calculation for the intended cutoff. A cold public `/health` intentionally reports
   evidence unavailable until this full verification or the authenticated bounded
   preflight populates the in-process health attestation.
9. Disconnect or block the network for the dress rehearsal. Calculation, local
   receipt persistence, restart recovery, and snapshot freshness must still work.

`completeness=full` means every supplied row was accepted. `partial` means at least
one row was accepted and at least one rejected. `empty` means zero accepted rows; it
is an explicit, verified state, not the same as a missing snapshot. Partial and empty
snapshots remain visible in every attestation and receipt so a broad prior cannot be
mistaken for full evidence.

## Atomicity and recovery

Snapshot metadata and accepted rows are immutable and digest-verified on every
status read. Activation is an append-only, revisioned hash chain; its complete
lineage is verified and the active snapshot is derived from the latest valid
revision. Activation and any new snapshot rows are written in one transaction with
compare-and-swap against the operator's expected active digest. Adapter failure,
schema failure, source-digest mismatch, validation failure outside the row-rejection
policy, a competing refresh, or database failure leaves the prior activation intact.

Do not edit snapshot tables manually. If status raises
`EvidenceSnapshotIntegrityError`, stop trusted calculation, preserve the database,
restore a verified backup or perform a new controlled import into a repaired durable
store, and retain the failed file for investigation.

## Race-day behavior

`ShadowPredictionService` requires a `ResultStore`; trusted calculation cannot be
constructed without one. For every request it first builds a canonical projection
from caller-controlled field identity, ordered entrant IDs/genders, event/cutoff,
schedule, run/operator/seed, observation schema/fingerprint, and wood. Caller
histories are forbidden by the trusted HTTP contract and are excluded from this
projection, calculation, and receipt.

It then:

1. looks up the caller/request and expected run revision before reading current
   evidence; an exact projection match replays the immutable old receipt even if the
   current snapshot is missing, stale, tampered, refreshed, or has another cutoff;
2. rejects reuse of that request identity for a different projection;
3. for a new request, verifies the active local snapshot and requires it to be
   current, integrity-verified, and at the field's exact cutoff;
4. bulk-loads the field's stable IDs after one snapshot verification and uses only
   those local prior rows;
5. freezes the request projection plus snapshot attestation, age, freshness,
   completeness, diagnostics, and lineage into the immutable receipt; and
6. calculates without invoking the refresh adapter or cloud.

Recovery never mutates or withholds the immutable receipt core. After an exact match,
STRATHMARK performs a local-only status read: matching verified current evidence is
`current`; valid but stale, refreshed, cutoff-mismatched, or fingerprint-mismatched
evidence is `stale`; missing or integrity-invalid evidence is `invalid`. Only
`current` is ready for review, and the result-level and receipt-level live views agree.
Changing an observation fingerprint therefore requires a new request identity, even
though observation context remains outside the active numeric/calculation hash.

An explicit successful refresh changes the snapshot digest. Repeating the same
request replays its old receipt; a superseding calculation must use a new request/run
revision and receives a new active fingerprint naming the new snapshot and its
predecessor. Existing receipts remain byte-for-byte replayable from the ledger.

Freshness is derived from the locally stored `captured_at` timestamp. The default
threshold is seven days and can be evaluated with
`get_evidence_snapshot_status(max_age_days=...)`. Staleness does not silently fetch
new data. It blocks every new trusted calculation, while exact old-receipt recovery
continues to work. Refresh deliberately, then issue a new superseding request.

Public `/health` uses only the last in-process fully verified attestation. It derives
freshness from current UTC time without reopening SQLite, and invalidates the attestation
when bounded filesystem metadata shows the database or SQLite sidecars changed. It does
not scan activation history or evidence rows. This makes liveness probes safe under load,
while process restart, cache miss, and observed file mutation fail closed until an
authenticated bounded operation verifies the active snapshot again.

## Isolated verification

From a feature checkout, with no production variables configured:

```powershell
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\offline-evidence-test.db"
Remove-Item Env:STRATHMARK_SUPABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:STRATHMARK_SUPABASE_KEY -ErrorAction SilentlyContinue
python -m pytest tests/test_offline_evidence_snapshot.py -q -p no:cacheprovider --basetemp .tmp/offline-evidence-pytest
```

The focused suite covers full/partial/empty imports, invalid/future/undated rows,
source-envelope bounds and cycles, atomic failure retention, immutable activation
chain verification, idempotent retry and competing refresh, restart, stale/future
age, stored-row tampering, recovery-first offline receipts, mandatory local storage,
caller-history exclusion, explicit refresh supersession, and exclusive cutoffs.
