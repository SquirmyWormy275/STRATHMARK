# REST API

Choose the API that matches your integration. The V2 ASGI app and the V3 V7 service
are separate applications; starting one does not start the other.

| Interface | Purpose |
| --- | --- |
| V2 `/calculate` | Stateless field calculation using the supplied history. |
| V2 `/simulate` | Audit a supplied mark sheet under assumed performance variation. |
| V2 ledger and `/v1/shadow` routes | Authenticated stored prediction/settlement evidence. |
| V3 `/v3/*` service | Authenticated competition lifecycle under the frozen V7 contract. |
| Linux V3 subprocess | Local competition runtime used by STRATHEX; not the V2 HTTP app. |

## Start the V2 API locally

Install the maintained rc12 [API extra](Installation). Use a new dedicated data
directory under your account; do not select a shared directory, checkout or home
root. Provision this directory explicitly before opening its database. Existing
authority paths require trusted ancestry; shared parents are never changed.

Linux shell:

```bash
export STRATHMARK_DB_PATH="$HOME/strathmark-api-data/results.db"
python -c "import os; from pathlib import Path; from strathmark.private_storage import private_directory; private_directory(Path(os.environ['STRATHMARK_DB_PATH']).parent)"
python -m uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
$env:STRATHMARK_DB_PATH = Join-Path $env:USERPROFILE "strathmark-api-data/results.db"
python -c "import os; from pathlib import Path; from strathmark.private_storage import private_directory; private_directory(Path(os.environ['STRATHMARK_DB_PATH']).parent)"
python -m uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for request examples and `/health` for model status.
Public `/calculate` is stateless and unauthenticated; keep this example on loopback.
The stored ledger/shadow routes require their own credentials and configuration.
This plain-HTTP example is for isolated local API exploration. STRATHEX HTTP
clients require a certificate-verified HTTPS endpoint even on loopback. Provision
a certificate matching the configured hostname, serve TLS directly or through a
trusted local TLS endpoint, and configure `STRATHMARK_API_CA_BUNDLE` when using a
private CA. See the [consumer transport requirements](https://github.com/SquirmyWormy275/STRATHEX/blob/main/docs/SECURITY_CLOUD_2026-10-19.md).

## Integrate the V7 service

V7 has 18 routes and an exact OpenAPI checksum. Pin its contract and source identity,
verify signed receipts, and use a bearer service credential plus `Idempotency-Key`
for trusted POSTs. STRATHEX requires verified HTTPS even on loopback and an
independently provisioned `STRATHMARK_V3_SIGNER_IDENTITY_DIGEST`. Configure
`STRATHMARK_V3_CA_BUNDLE` for a private CA. The readiness identity must match the
local pin; never learn that pin from a network response. Non-loopback service
operation additionally requires pinned mutual TLS. Actor headers are audit
metadata, not human permissions.

Pre-field forecasts supply seed times and say `issued_mark=false`. Exact-field
assembly produces marks. `/v3/approvals/decide` records review decisions;
`/v3/issues/acknowledge` is the separate issue action.

Use the [V3 route table and examples](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V3.md)
and [consumer migration guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/STRATHEX_CONSUMER_MIGRATION.md).
The [frozen OpenAPI](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.json)
and [checksum](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.sha256)
are the integration reference. Windows production qualification remains incomplete.

## Request limits in 3.0.0rc12

V2 request bodies are checked before JSON parsing and limited to 1 MiB. History
is limited to 1,000 rows per competitor and 4,000 rows per field. Oversized input
returns an explicit error; public work has bounded admission and does not consume
the reserved shadow-recovery worker pools.

V3 health returns without waiting for a request body. Trusted body reads have a
10-second total deadline and a 2-second idle deadline. A timed-out synchronous
operation keeps its worker slot until it actually exits, so recover a write by
its original idempotency identity. Pre-field receipt creation uses the service
clock; only an existing timely receipt can be recovered after its hard deadline.

See [Security maintenance](Security-Maintenance) for artifact, archive and storage
limits and the separate installation-verification requirements.
