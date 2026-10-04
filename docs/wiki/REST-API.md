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

Install the [API extra](Installation), then choose an explicit database path.

Linux shell:

```bash
mkdir -p api-data
export STRATHMARK_DB_PATH="$PWD/api-data/results.db"
python -m uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force api-data | Out-Null
$env:STRATHMARK_DB_PATH = Join-Path (Get-Location) "api-data/results.db"
python -m uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for request examples and `/health` for model status.
Public `/calculate` is stateless and unauthenticated; keep this example on loopback.
The stored ledger/shadow routes require their own credentials and configuration.

## Integrate the V7 service

V7 has 18 routes and an exact OpenAPI checksum. Pin its contract and source identity,
verify signed receipts, and use a bearer service credential plus `Idempotency-Key`
for trusted POSTs. Loopback is the default; non-loopback service operation additionally
requires pinned mutual TLS. Actor headers are audit metadata, not human permissions.

Pre-field forecasts supply seed times and say `issued_mark=false`. Exact-field
assembly produces marks. `/v3/approvals/decide` records review decisions;
`/v3/issues/acknowledge` is the separate issue action.

Use the [V3 route table and examples](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V3.md)
and [consumer migration guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/STRATHEX_CONSUMER_MIGRATION.md).
The [frozen OpenAPI](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.json)
and [checksum](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/strathmark/v3/contracts/v3_consumer.openapi.sha256)
are the integration reference. Windows production qualification remains incomplete.
