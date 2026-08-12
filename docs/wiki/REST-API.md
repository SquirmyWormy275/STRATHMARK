# REST API

Start with `uvicorn strathmark.api:app --host 127.0.0.1 --port 8000`; interactive
OpenAPI docs are at `/docs`.

| Route | Authentication | Persistence |
| --- | --- | --- |
| `GET /health` | public | none |
| `POST /predict` | public | none |
| `POST /calculate` | public | none |
| `POST /simulate` | public | none |
| `POST /ledger/calculate` | Bearer token | append-only trusted field |
| `POST /ledger/predictions/{prediction_id}/settle` | Bearer token | immutable settlement revision |
| `POST /results` | Bearer token | local result history |
| `GET /results/{competitor_name}` | Bearer token | local result history read |

Set `STRATHMARK_API_TOKEN` to enable protected routes. If it is absent they return 503;
an invalid token returns 401.

`/calculate` accepts competitors, wood, event code, and optional exclusive
`prediction_as_of`. Results include the predicted time, mark, method, forecast interval,
performance `std_dev`, versions, optimizer metadata, warnings, and degraded state.

`/ledger/calculate` adds `request_id` and requires every `competitor_id`. Identical
retries return original prediction IDs; a changed payload under the same key returns
409. Settlement verifies prediction/competitor/event, deduplicates exact retries, and
requires a reason for corrections.

`/simulate` defaults to and caps at 250,000 races. It is a post-mark audit, separate
from the optimizer's fixed 2,048 samples.

See the repository's `STRATHMARK API.txt` for request/response details and [Prediction
Engine V2](Prediction-Engine-V2) for numeric semantics.
