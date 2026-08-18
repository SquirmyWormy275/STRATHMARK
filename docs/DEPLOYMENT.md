# Deployment Runbook

Use this runbook for STRATHMARK 2.0.0 race-day Python or REST deployments. The default
prediction path is offline V2. Database, cloud, CatBoost, and Ollama availability are
not prerequisites for numeric marks.

## 1. Prepare

Install the exact reviewed commit and only the extras the host actually needs:

```bash
python -m pip install \
  "strathmark @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@da5c44d07311b226c1e9842104477efaf61253fa"
python -m pip install \
  "strathmark[api] @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@da5c44d07311b226c1e9842104477efaf61253fa"  # REST host only
```

No 2.0 tag, GitHub release, or PyPI distribution exists yet. Replace the Git pin only
after a reviewed release is published.

The validated core artifact is packaged at
`strathmark/models/prediction_v2_core.json`. Do not download or train a model during an
event.

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `STRATHMARK_PREDICTION_CORE_ARTIFACT` | operator-approved safe JSON core override |
| `STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT` | optional promoted residual directory |
| `STRATHMARK_PREDICTION_ENGINE=legacy` | temporary deterministic baseline-only rollback |
| `STRATHMARK_DB_PATH` | local result store and ledger SQLite path |
| `STRATHMARK_API_TOKEN` | enables protected result and trusted-ledger routes |
| `STRATHMARK_LEDGER_CALLER` | trusted request caller namespace; default `api` |
| `STRATHMARK_LEDGER_ACTOR` | settlement actor label; default `api` |
| `STRATHMARK_SUPABASE_URL`, `STRATHMARK_SUPABASE_KEY` | optional best-effort cloud mirror |

Ollama/Gemini variables affect narrative features only. They cannot change a V2 median
or mark.

## 2. Verify before the event

From the release checkout:

```bash
python train_model.py
pytest tests/test_deployment_fallbacks.py tests/test_api.py -q
python scripts/validate_deployment.py
```

`python train_model.py` is verify-only: it checks the published report, source checksum,
manifest, and packaged artifact without reopening the locked test.

Do not run `python train_model.py --open-locked-test`. The 2.0.0 locked role has already
been opened once. Do not remove its final report to rerun it.

For the REST service:

```bash
uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Check `GET /health` (or `GET /health?prediction_as_of=YYYY-MM-DD` for a backdated field).
Before accepting calculations, confirm:

- `prediction_engine.core.available` is true;
- `prediction_engine.core.compatible_with_cutoff` is true for the intended cutoff;
- calibration is available;
- expected core/calibration versions are shown;
- `source` is the intended environment, local, or package source;
- `degraded` is false and warnings are understood.

Residual `active=false` is expected for the 2.0.0 packaged release. Ollama unavailable
is acceptable for numeric calculation.

## 3. Make requests safely

Always send an explicit `prediction_as_of` date for reproducible operations. V2 treats
it as an exclusive UTC cutoff. Same-day and later results cannot influence that request.

Use `/calculate` when marks should be stateless. Use `/ledger/calculate` only when the
caller can supply a stable `competitor_id` for every competitor and a durable unique
`request_id`. The protected route also requires `Authorization: Bearer
<STRATHMARK_API_TOKEN>`.

Legacy fields such as division, tournament results, heat ID, field strength, and wood
quality may still be accepted, but they are numeric no-ops in V2. Do not promise that
they affect marks.

One response item should expose interval and engine/model/calibration versions. Treat
`interval` as forecast uncertainty and `std_dev` as simulation variability. Inspect
`warnings`, `degraded`, `optimizer`, and `optimizer_metadata` before printing a start
sheet.

## 4. Trusted-ledger behavior

The local SQLite file from `STRATHMARK_DB_PATH` (default
`~/.strathmark/results.db`) is the race-day authority. A complete field is recorded in
one transaction after marks are final. Identical retries return original prediction
IDs; a request-key payload conflict returns HTTP 409.

Cloud mirroring is best-effort and off the calculation response path.
`ledger_status=recorded_cloud_pending` means local trust evidence and a replayable mirror
outbox entry exist. The ledger's single bounded background worker reclaims overflowed
and restart-surviving rows; `flush_mirror_outbox()` remains an explicit bounded replay.
Shadow receipts and numeric outcome revisions use a versioned delivery envelope. The
cloud copy contains the immutable receipt core, identity namespace, observation
fingerprint, eligible numeric settle/void rows, and delivery metadata only. Operational
DNF/DQ/penalty/context history, names, narrative notes, and secrets remain outside the
STRATHMARK mirror. A mirror outage never weakens a committed local receipt.
`ledger_recorded=false`
means marks are still valid but no trusted local record was made; preserve the request
and investigate disk/path/permission state before settlement.

Settlements must reference the returned `prediction_id`, matching competitor ID and
event. Corrections require a reason and append a new revision. Never edit ledger rows.

## 5. Degraded and fallback states

| Symptom | Meaning | Action |
| --- | --- | --- |
| `core_artifact_missing` | no environment/local/package core found | verify wheel contents and override path; result uses broad event prior |
| `core_artifact_invalid` | JSON/schema/checksum/size check failed | remove the bad override and restart; packaged core should load |
| `artifact_newer_than_prediction_cutoff` | backdated request cannot use this core | use a compatible historical artifact or accept labeled broad prior |
| `residual_*` warning | optional residual absent/incompatible | continue with core; do not activate without promotion evidence |
| `calibration_unavailable` | core has no usable calibrator | treat interval as degraded; restore validated artifact |
| `core_prediction_failed` | request could not be evaluated by core | inspect request and artifact; broad event prior remains available |
| optimizer `rounded_gap_fallback` | joint search unavailable or rejected | marks remain bounded and deterministic; record fallback reason |
| `ledger_status=write_failed` | trusted local write failed | marks remain usable; retain request externally and repair storage |

## 6. Rollback

If a verified V2 regression affects operations, set the explicit selector and restart
the process:

```powershell
$env:STRATHMARK_PREDICTION_ENGINE = "legacy"
```

This uses the deterministic legacy baseline only. It removes inactive context, applies
the same exclusive cutoff, and never calls an LLM for a number. Confirm `/health` and a
known fixture, document when and why rollback began, and open an incident. Remove the
variable and restart to return to V2.

Do not hot-swap artifact files within an active field. The request snapshot prevents a
mixed field, but operational changes belong between fields and must be logged.

## 7. Post-event

1. Settle trusted predictions using official positive measured times and returned IDs.
2. Include an actor and a reason for every correction.
3. Retain the local SQLite file before syncing or maintenance.
4. Review warnings, degraded results, optimizer fallbacks, and cloud failures.
5. Run drift analysis off the hot path. Drift is advisory; it never auto-activates,
   retrains, or disables a model.
6. Train only on rows explicitly marked eligible; manual, broad-prior, legacy-rollback,
   and degraded rows are excluded.

Supabase migrations 005, 006, and 007 must be reviewed and applied in order, separately from
the application release, before cloud mirroring. Migration 006 preserves old request
rows as `raw-v1` while recording new active-evidence hashes as `active-v2`. The ledger
schema forces RLS and grants its append RPC only to `service_role`; never expose the
service key to browser or mobile clients.
Migration 005 rejects explicit active-v2 payloads until 006 is installed, leaving them
in the durable local outbox. The guarded 006 down migration restores the old RPC but
aborts once any active-v2 cloud row exists; do not use it after active mirroring begins.
Migration 007 adds the separate shadow evidence RPC/tables without rewriting 005/006
rows. Its down file refuses once any shadow delivery exists. After that point, repair
forward or restore from the durable local ledger. A disposable PostgreSQL rehearsal is
required before a separately authorized production window; this repository change does
not apply or authorize a production migration.
