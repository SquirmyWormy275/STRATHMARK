# Stray Validation Row Cleanup Candidates — 2026-05-04

Status: NO CLEANUP NEEDED. Audit returned zero candidates.

## Audit method

All 1311 rows in `results` were paged via PostgREST (full content access via the
service-role key). Each row was inspected against three filters:

1. `source_app` matches anything containing `'validate'` (e.g., `'validate_deployment'`)
2. `show_name` matches anything containing `'validation'` (e.g., the `'VALIDATION'`
   string used by `scripts/validate_deployment.py:295`)
3. `notes` contains the marker `'do not keep'` (case-insensitive), matching the
   string emitted by `scripts/validate_deployment.py:288`:
   `"validation script test row -- DO NOT keep"`

## Findings

- Rows matching filter 1 (validation source_app): **0**
- Rows matching filter 2 (validation show_name): **0**
- Rows matching filter 3 ("do not keep" marker): **0**

## Source_app distribution (full population)

```
'initial-seed'      1311
```

Every row has `source_app='initial-seed'` and `show_name='Historical'`. These all
came in via a single insert batch on 2026-03-10 01:41 UTC, recorded in the only
`sync_log` row (`sync_id=1`, `records_written=1311`).

## Recommendation

No rows need to be deleted. The recon assessment correctly flagged stray validation
rows as a possible risk, but `validate_deployment.py --write` was apparently never
run against this database. The system has been de-facto read-only since the initial
seed.

## What changed in this PR

Nothing. This file documents an absence-of-finding and is the Phase 2 deliverable.
The `validate_deployment.py --write` flag still exists in the script. It is
explicitly OUT OF SCOPE for this PR (per the task constraints, "Cleanup of the
stray validation test rows surfaced in Phase 2... is an operational task
post-human-review"). Removing the `--write` flag is part of the controlled-write
follow-on PR that lands the RLS reframe.
