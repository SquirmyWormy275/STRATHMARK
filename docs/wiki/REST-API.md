# REST API

STRATHMARK ships with an optional FastAPI service for clients that cannot use
the Python package directly. Python consumers such as STRATHEX use the import
API instead.

## Install and run

```bash
pip install strathmark[api]
export STRATHMARK_API_TOKEN="replace-with-a-long-random-secret"
uvicorn strathmark.api:app --host 127.0.0.1 --port 8000 --workers 2
```

Deploy the service behind TLS and a reverse proxy. Bind to `0.0.0.0` only when
the network boundary is controlled. FastAPI publishes the complete, generated
contract at `http://localhost:8000/docs`; that schema is authoritative when it
differs from an example below.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, store availability, and Ollama status |
| POST | `/calculate` | Calculate marks for a field |
| POST | `/predict` | Return cascade predictions for one competitor |
| POST | `/simulate` | Run a bounded Monte Carlo fairness simulation |
| POST | `/results` | Record a protected, competition-identified result |
| GET | `/results/{competitor_name}` | Retrieve protected result history |

## Validation rules

All result and historical-time inputs must use event code `SB` or `UH`, a time
from 3 through 180 seconds, a diameter from 225 through 500 mm, and wood
quality from 1 through 10. ISO dates are parsed by FastAPI, so malformed dates
return HTTP 422 rather than silently becoming undated history.

`/simulate` accepts 2 through 64 competitors and 1 through 250,000
simulations. The product of competitors and simulations cannot exceed
4,000,000. These limits protect the service from an allocation that would
degrade other live-event requests. Each worker admits at most two simulations
at once; when a worker is at capacity, `/simulate` returns HTTP 429 and
callers should retry shortly.

## GET /health

```json
{
  "status": "ok",
  "ollama_available": false,
  "ollama_model": "qwen3.5:9b",
  "store_available": true,
  "store_results_count": 42
}
```

The response intentionally does not expose the server's local database path.

## POST /calculate

```json
{
  "competitors": [
    {
      "name": "Alice Smith",
      "history": [
        {
          "event_code": "SB",
          "time_seconds": 28.4,
          "species": "Pine",
          "diameter_mm": 300,
          "quality": 5,
          "result_date": "2025-03-01"
        }
      ]
    }
  ],
  "wood": {"species": "Pine", "diameter_mm": 300, "quality": 5},
  "event_code": "SB"
}
```

The response is an array ordered slowest to fastest:

```json
[
  {
    "name": "Alice Smith",
    "mark": 3,
    "predicted_time": 28.47,
    "method_used": "baseline",
    "confidence": "MEDIUM",
    "explanation": "Weighted historical average.",
    "std_dev": 2.1
  }
]
```

## POST /predict

Use the same competitor, wood, and event-code shapes as `/calculate`. The
response contains `best` plus an `all_predictions` object keyed by prediction
method. `best.mark` is always `3` because a mark is relative to a field and is
only meaningful after calling `/calculate`.

## POST /simulate

```json
{
  "competitors": [
    {"name": "Alice", "mark": 3, "predicted_time": 28.47, "std_dev": 2.1},
    {"name": "Bob", "mark": 8, "predicted_time": 33.47, "std_dev": 2.4}
  ],
  "num_simulations": 100000
}
```

The response includes winner and podium counts, percentages, finish-position
statistics, spread statistics, and a fairness assessment. Large raw
finish-spread arrays are omitted from the HTTP response.

## Protected result endpoints

Both `/results` endpoints require this header:

```text
Authorization: Bearer <STRATHMARK_API_TOKEN>
```

If `STRATHMARK_API_TOKEN` is absent, the endpoints return HTTP 503. An absent
or incorrect bearer token returns HTTP 401.

### POST /results

`competition_id` is required. It must identify the source show or a stable
upstream competition record so the same heat label at different events is not
discarded as a duplicate.

```json
{
  "competitor_name": "Alice Smith",
  "event_code": "SB",
  "time_seconds": 28.4,
  "species": "Pine",
  "diameter_mm": 300,
  "quality": 5,
  "competition_id": "missoula-pro-am-2026",
  "heat_id": "SB-H3",
  "result_date": "2026-04-25"
}
```

```json
{"inserted": true, "message": "Result recorded."}
```

### GET /results/{competitor_name}

Pass optional `event_code=SB` to filter the returned JSON array. The endpoint
returns every matching stored result; consumers that need pagination should
apply it locally until a versioned pagination contract is introduced.

## Testing

`tests/test_api.py` runs the API contract against a temporary SQLite store and
never touches `~/.strathmark/results.db`. CI installs the `api` extra before
running that test file, and separately imports the built wheel in an isolated
environment.
