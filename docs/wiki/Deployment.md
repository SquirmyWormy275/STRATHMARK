# Deployment

1. Pin `strathmark==2.0.*` and install only needed extras.
2. Run `python train_model.py` from a checkout to verify published checksums/report.
3. Run deployment/API fallback tests and `scripts/validate_deployment.py`.
4. Start the service and inspect `/health`.
5. Supply explicit `prediction_as_of` dates in operational requests.

Core and calibration should be available. Residual inactive is expected in 2.0.0;
Ollama status is narrative-only.

Important variables:

- `STRATHMARK_PREDICTION_CORE_ARTIFACT`: approved JSON override.
- `STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT`: optional residual directory.
- `STRATHMARK_PREDICTION_ENGINE=legacy`: temporary baseline-only rollback.
- `STRATHMARK_DB_PATH`: SQLite result/ledger file.
- `STRATHMARK_API_TOKEN`: protected route token.
- `STRATHMARK_SUPABASE_URL` and `STRATHMARK_SUPABASE_KEY`: optional mirror.

Do not rerun the 2.0.0 `--open-locked-test`; do not train during an event; do not
hot-swap an artifact in a field; never expose a Supabase service key to a client.

If the core fails, V2 returns a labeled broad prior. If the optimizer fails, it returns
bounded rounded-gap marks. If persistence fails, marks remain valid and ledger status
reports the failure. Full procedures are in `docs/DEPLOYMENT.md`.
