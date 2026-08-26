# Deployment, Recovery, and V2-to-V3 Cutover

## Authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements; implementation is under final audit. The checked-in **rehearsal** receipt
must not be treated as current until the release verifier regenerates and revalidates it
from the final documentation commit and exact candidate artifact. V2 remains the trusted
production authority until an explicit cutover. No production authority has changed, no
consumer endpoint has switched, and V2 remains the recovery authority.
The external STRATHEX durable outbox/adapter is not implemented.

This runbook distinguishes four states that must never be collapsed:

1. code and tests exist;
2. development-key rehearsal passes;
3. production CNG-backed cutover preparation is ready;
4. an explicitly authorized consumer switch has occurred.

The repository is between states 1 and 2 until executable evidence is regenerated from
the final documentation commit. The older checked-in release attestation is deliberately
stale, cannot reach states 3 or 4, and is not current evidence for this changed tree.
No production CNG identity is currently provisioned.

Use Python 3.13 and install `requirements/v3-release.lock` for every V3 rehearsal,
deployment, migration, recovery drill, and release-verification run. Python 3.10-3.12
remain supported for the normal package and V2, not for V3 authority. Do not enable
SQLite `trusted_schema` to bypass older bundled SQLite rejection of V3's JSON expression
indexes; that changes the security contract instead of satisfying it.

## Mandatory safety boundaries

- Read [`wiki/Handicap-Mark-Math.md`](wiki/Handicap-Mark-Math.md) before operating marks.
- Never run tests, replay, migration rehearsal, factory evaluation, or recovery drills on
  a production or operator database.
- Keep V2 and V3 database paths separate. Never configure concurrent trusted writers.
- Preserve issued receipts and official results. Recovery may rebuild projections but
  cannot rewrite authority history.
- Do not require cloud, Ollama, or an archive for issue, receipt lookup, or settlement.
- Bootstrap and last-key recovery are offline operations and require the listener to be
  stopped. Ordinary authenticated key rotation is online: install the next credential,
  verify overlap, move clients, then revoke the old credential.
- Do not bind V3 outside loopback without pinned mutual TLS and expected-host validation.
- Never copy a development private key or ephemeral signer into a production role.
- No verifier, test, or signed pre-switch handoff performs the final endpoint switch.

## Isolated rehearsal

Confirm `python --version` reports Python 3.13 before running these commands.

```powershell
$env:STRATHMARK_TEST_DB = '1'
$env:STRATHMARK_DB_PATH = "$PWD\.tmp\deployment-v2.sqlite3"
$env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\deployment-v3.sqlite3"
$env:STRATHMARK_V3_TEMP_PATH = "$PWD\.tmp\deployment-runtime"
$env:STRATHMARK_V3_BLOB_ROOT = "$PWD\.tmp\deployment-blobs"
$env:STRATHMARK_V3_BUNDLE_ROOT = "$PWD\.tmp\deployment-bundles"
$env:STRATHMARK_V3_ARCHIVE_ROOT = "$PWD\.tmp\deployment-archive"
$env:STRATHMARK_V3_BACKUP_ROOT = "$PWD\.tmp\deployment-backup"
$env:STRATHMARK_V3_RECOVERY_ROOT = "$PWD\.tmp\deployment-recovery"
$env:STRATHMARK_V3_INTEGRITY_KEY_ROOT = "$PWD\.tmp\deployment-keys"

python -m pytest tests/v3 -q --basetemp .tmp/deployment-pytest -p no:cacheprovider
python scripts/replay_v3.py
python scripts/run_v3_release_evidence.py --local-model qwen3.5:9b --local-model ministral-3:8b
$source = (git rev-parse HEAD).Trim()
python scripts/verify_v3_release.py --evidence benchmarks/v3/v3_executable_evidence.json --emit-rehearsal $source --output-attestation benchmarks/v3/v3_release_attestation.json
python scripts/verify_v3_release.py
```

`resolve_runtime_config()` validates the snapshot without creating directories, opening
storage, loading models, or starting workers. Test mode rejects known production
identifiers and default operator paths.

The release verifier must execute or validate a class-specific result receipt for the
exact dependency lock, installed wheel, frozen consumer contract, causal replay,
equity/manipulation slices, provider failures, race-day recovery, Windows capacity and
stress, backup/restore, and bundle/model integrity. A digest of test source, a
preconstructed record, or a self-declared `passed` row is not execution evidence. Every
result must report `authority_changed: false`; the verifier must fail while any checked-in
receipt is stale.

The separate post-format result-to-ready benchmark completed five trials on the
designated Windows machine. Its maximum was 3.414 seconds against the 120-second limit,
with exact source bindings and component latency retained in
`benchmarks/v3/result_to_ready_manifest.json`. This focused measurement does not make
the stale exact-wheel rehearsal current.

Confirm the production gate fails closed:

```powershell
python scripts/verify_v3_release.py --require-production
```

Before fresh evidence exists, the ordinary verifier must fail closed for missing or
stale evidence. After fresh rehearsal evidence is emitted, the expected production-gate
result is exit code 2 with `production_attestation_required` and
`authority_changed: false`.

## Runtime storage

V3 uses a dedicated local SQLite event authority plus content-addressed blob and bundle
roots. The default non-test root is `%USERPROFILE%\.strathmark\v3`, but an installation
should set explicit absolute paths on durable local storage:

| Variable | Purpose |
| --- | --- |
| `STRATHMARK_V3_DB_PATH` | authoritative local event database |
| `STRATHMARK_V3_TEMP_PATH` | non-authoritative runtime scratch |
| `STRATHMARK_V3_BLOB_ROOT` | content-addressed large values |
| `STRATHMARK_V3_BUNDLE_ROOT` | installed immutable bundles |
| `STRATHMARK_V3_ARCHIVE_ROOT` | optional asynchronous archive material |
| `STRATHMARK_V3_BACKUP_ROOT` | verified backup sets |
| `STRATHMARK_V3_RECOVERY_ROOT` | recovery-device exchange |
| `STRATHMARK_V3_INTEGRITY_KEY_ROOT` | public identities and CNG key references |
| `STRATHMARK_V3_CANONICAL_MAX_BYTES` | canonical object byte bound |
| `STRATHMARK_V3_CANONICAL_MAX_DEPTH` | canonical object depth bound |

Database, temp, and artifact paths must be distinct. Backups include a consistent event
database, blob inventory, projection/version metadata, and signed manifest. Restore
verification checks digests and event chains before any authority declaration.

## Service deployment

The V3 FastAPI app is created with injected application and credential ports. There is
no safe zero-configuration production global app. Deployment composition must:

1. resolve one immutable runtime configuration;
2. open and migrate the dedicated V3 event store;
3. verify event/global chains and rebuild projections if required;
4. verify bundle, blob, optimizer, and model identities;
5. open installation-owned CNG keys by name, never by exported private bytes;
6. construct the service credential registry;
7. inject the V3 application gateway;
8. bind loopback at `127.0.0.1:8787`, or provide the complete non-loopback mTLS policy;
9. check dependency-specific readiness before accepting trusted work.

`GET /v3/health` is public process health, not proof that issue or settlement is safe.
Use authenticated `GET /v3/status` plus the operation-specific readiness snapshot.

### Factory and evaluator host

The local factory composition and scheduler are runnable, but deployment must inject one
concrete local-configured executor for each formula/ML/LLM family and a local evaluator
that derives settlement metrics from authenticated settled evidence. Test doubles do not
qualify the installation. Run the frozen evaluator through the bounded file exchange and
an existing non-exportable CNG key:

```powershell
strathmark-v3-factory-evaluator REQUEST.json RESPONSE.json `
  --registry C:\ProgramData\STRATHMARK\v3\factory\audit-registry `
  --cng-key-name strathmark-v3-evaluator
```

The command does not create the key or OS boundary. Provision separate builder,
evaluator, and signer identities; enforce their process and filesystem ACLs; deny the
forbidden audit/signing access; and verify the boundary in exact-source evidence and CI.
No factory process may auto-promote a bundle or change V2/API authority.

## Race-day operating sequence

1. Verify the active bundle, tournament epoch, event-chain tip, projection health,
   backup age, job queue, assessor availability, and SLA risk.
2. Prepare rolling competitor cards as soon as future context is plausible. The live
   scheduler must bind the promoted council, card-scoped provider tokens, and deadlines;
   a generic symbolic schedule is not an executable live request.
3. Freeze one epoch for every field in the current round.
4. Assemble each final roster from compatible sealed cards; stale cards regenerate.
5. Present the exception-first green/amber/red projection to the tournament manager.
6. Record the deliberate approval decision through `/v3/approvals/decide`, binding the
   exact snapshot, selected and excluded receipt IDs/digests/revisions, actor metadata,
   timestamp, and idempotency identity. No manual estimate is defaulted.
7. Separately acknowledge issue atomically and retain the exact receipt set.
8. Submit the complete issued roster and settle all valid completions and explicit
   non-completion states in one atomic command.
9. Drive and durably close capability, invalidation, scoring, coverage, weights,
   readiness, and credibility reactions. Do not infer or insert an approval decision.
10. Close the round, advance the epoch, and prepare the next round. Never recalculate an
    issued sheet or alter the legal winner.

The live contract is a heat every ten minutes, a sheet ready within two minutes of a
result, field assembly under two seconds, and a five-minute final call-up path.

## Recovery matrix

| Failure | Required response |
| --- | --- |
| Process or worker crash | restart, reconcile leased/ambiguous jobs, recover exact command result |
| Machine or power loss | open durable store, verify WAL/event chains, rebuild projections, reconcile external work |
| Ollama unavailable/OOM | mark local assessor unavailable; use already sealed valid cards or deliberate non-predictive action |
| Cloud unavailable | abstain cloud member; never block local authority or relabel a forecast |
| Blob missing/tampered | fail affected operation closed; recover verified blob before use |
| Disk reserve breached | stop factory/backfill and speculative work; preserve critical open-tournament writes; block new tournaments |
| Queue saturation | reject bounded work with explicit capacity status; preserve exact retries |
| Primary machine unavailable | use only a verified recovery-device package and authority procedure |
| Trusted V3 cannot recover | keep/resume V2 before cutover; after cutover declare traditional/manual authority explicitly |

Support exports are deterministic, size-bounded, signed, and redacted. They contain
allowlisted operational facts and metrics, not credentials, raw evidence, free text, or
private keys.

## Production qualification prerequisites

Before cutover preparation, all of these must be true on the installation that will run
the event:

- the source commit and dependency lock are frozen;
- the installed wheel and all required artifacts match their digests;
- the frozen OpenAPI checksum matches the consumer adapter;
- all eleven release-evidence classes pass on the production candidate;
- the Windows capacity manifest is production-tier for the designated machine;
- candidate/audit/release signing identities are live non-exportable Windows CNG keys;
- concrete local formula/ML/LLM factory executors and the authenticated local
  settlement-metric evaluator are installed and exercised;
- builder/evaluator/signer OS identities, filesystem/process ACLs, and network-denial
  policy are enforced and verified on the designated host;
- the release verifier is given the installation-owned, operator-pinned public release
  identity through `--trusted-production-identity`; it never trusts identity metadata
  supplied by the attestation being verified;
- model and bundle promotion manifests verify under their installed trust stores;
- backup/restore and recovery-device rehearsals pass;
- no tournament is open in either authority boundary;
- explicit release authority is available for the later consumer switch.

The repository intentionally contains no production private key, credential, endpoint,
pre-approved switch, or provisioned production CNG identity.

## Cutover preparation

Cutover is a zero-open-tournament state machine:

1. Freeze V2 trusted writes.
2. Verify `open_tournaments=0`.
3. Drain in-flight requests and resolve every ambiguous operation.
4. Sign and verify the final V2 authority manifest.
5. Verify the production-tier V3 release attestation against the separately pinned
   installation identity:

   ```powershell
   python scripts/verify_v3_release.py --require-production --trusted-production-identity C:\ProgramData\STRATHMARK\v3\keys\release-public-identity.json
   ```

   The identity file is operator-controlled public material outside the attestation. A
   self-supplied or merely relabeled `production_cng` identity is not trusted.
6. Verify initialized V3 database, bundle, consumer-contract, and rehearsal digests.
7. Run the installed tournament-manager adapter rehearsal and match its digest.
8. Sign the pre-switch authority handoff using the production CNG identity.

The handoff is only valid when it still says:

```text
status=cutover_ready
current_authority=v2
next_authority=v3
endpoint_switched=false
v2_audit_only=false
requires_explicit_release_authorization=true
```

Any failure resumes V2. If V2 cannot resume, stop and declare traditional/manual
authority. Never infer permission to switch from a successful preparation.

## Explicit consumer switch and rollback

The final consumer endpoint/contract switch is a separate, explicitly authorized
operation owned with the tournament manager. It must occur once, at the verified
boundary, with an immutable receipt. Afterward V2 becomes audit-only and must not accept
trusted writes.

There is no automatic numeric fallback from V3 to V2 after cutover. A failed V3 service
must be recovered from its authoritative event log or the competition must deliberately
enter traditional/manual authority. Re-enabling V2 trusted writes would be another
signed authority migration, not a runtime retry.

## Historical V2 deployment

V2 deployment and shadow behavior remain defined by
[`PREDICTION_ENGINE_V2.md`](PREDICTION_ENGINE_V2.md) and
[`SHADOW_CONSUMER_CONTRACT.md`](SHADOW_CONSUMER_CONTRACT.md). Existing PostgreSQL mirror
migrations remain separately authorized operational work and do not establish V3
authority.
