# Deployment

1. Install immutable release tag `v2.0.0` (or pin its exact commit) and only the
   needed extras. This version is distributed through GitHub, not PyPI.
2. Run `python train_model.py` from a checkout to verify published checksums/report.
3. Run deployment/API fallback tests and `scripts/validate_deployment.py`.
4. Start the service and inspect `/health`.
5. Supply explicit `prediction_as_of` dates in operational requests.

Core and calibration should be available and compatible with the requested cutoff.
Residual inactive is expected in 2.0.0; Ollama status is narrative-only.

Important variables:

- `STRATHMARK_PREDICTION_CORE_ARTIFACT`: approved JSON override.
- `STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT`: optional residual directory.
- `STRATHMARK_PREDICTION_ENGINE=legacy`: temporary baseline-only rollback.
- `STRATHMARK_DB_PATH`: SQLite result/ledger file.
- `STRATHMARK_API_TOKEN`: protected route token.
- `STRATHMARK_SUPABASE_URL` and `STRATHMARK_SUPABASE_KEY`: optional mirror.
- `STRATHMARK_SHADOW_SERVICE_CREDENTIALS`: consumer-to-bearer-secret JSON map.
- `STRATHMARK_SHADOW_ATTESTATION_KEYS`: consumer-to-v2-signing-key JSON map; values
  must be disjoint from service bearer secrets.
- `STRATHMARK_TRUSTED_TOPOLOGY=offline-single-writer-durable`: explicit supported
  trusted-shadow topology claim.

Production migration is a separate, explicit operator authorization. First run the
disposable PostgreSQL rehearsal, then apply migrations 005, 006, and 007 in that exact
order before enabling the mirror. Migration 005 rejects active-v2 payloads until 006 is
present, so they remain retryable in the local outbox. Migration 007 adds the closed
shadow receipt and numeric-revision mirror contract. Exercise the checked-in guarded
down scripts only in rehearsal: 006 refuses rollback after active-v2 evidence, and 007
refuses rollback after shadow evidence. Recovery after activation is forward repair or
restore, never an improvised destructive rollback.

Before declaring trusted-shadow readiness, verify the durable single-writer ledger,
refresh and attest the local evidence snapshot, and inspect `/health`. The six protected
shadow routes additionally require both service authentication and a request-digest-bound
v2 actor attestation. No documentation or passing local test authorizes a production
database migration, secret change, or deployment.

Public `/health` does not perform a full integrity scan or acquire a SQLite write
reservation. After every process restart, run the authenticated bounded preflight (or
an explicit operator snapshot verification) before expecting trusted-shadow readiness.
The resulting in-process evidence attestation fails closed if filesystem metadata shows
the database changed. Ledger persistence health reflects a cached successful SQLite
initialization/read-write open plus current file identity and permissions; it is not a
claim that the underlying storage is durable.

Do not rerun the 2.0.0 `--open-locked-test`; do not train during an event; do not
hot-swap an artifact in a field; never expose a Supabase service key to a client.

If the core fails, V2 returns a labeled broad prior. If the optimizer fails, it returns
bounded rounded-gap marks. If persistence fails, marks remain valid and ledger status
reports the failure. Full procedures are in `docs/DEPLOYMENT.md`.
