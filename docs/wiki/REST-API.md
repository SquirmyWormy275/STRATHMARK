# REST API

STRATHMARK ships with an optional HTTP REST API built on FastAPI.
Python clients (STRATHEX, the Missoula Pro-Am Manager) use the direct
Python import API for zero-overhead calls. The REST API is aimed at
web apps, mobile apps, or non-Python clients that cannot embed the
engine directly.

## Install

```bash
pip install strathmark[api]
```

## Run

```bash
uvicorn strathmark.api:app --host 0.0.0.0 --port 8000 --workers 2
```

Swagger / OpenAPI UI is auto-generated at
`http://localhost:8000/docs`.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/health`                 | check store availability and Ollama connection |
| POST   | `/calculate`              | compute handicap marks for a field of competitors |
| POST   | `/predict`                | return every cascade level's prediction for one competitor |
| POST   | `/simulate`               | run Monte Carlo fairness simulation |
| POST   | `/results`                | record a tournament result to the local store |
| GET    | `/results/{competitor}`   | return that competitor's history from the local store |

All request and response bodies are JSON. The FastAPI-generated
Swagger UI is the canonical reference — the examples below are the
common shapes.

## GET /health

```json
{
  "version": "0.4.0",
  "store_available": true,
  "ollama_available": false,
  "ollama_url": "http://localhost:11434/api/generate"
}
```

Use this as the liveness probe for containerised deployments.

## POST /calculate

Request:

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
      ],
      "division": "Open"
    }
  ],
  "wood": {
    "species": "Pine",
    "diameter_mm": 300,
    "quality": 5
  },
  "event_code": "SB",
  "event_ceiling": null,
  "manual_overrides": {}
}
```

Response:

```json
{
  "start_sheet_text": "+===...",
  "results": [
    {
      "name": "Alice Smith",
      "mark": 3,
      "predicted_time": 28.47,
      "method_used": "baseline",
      "confidence": "MEDIUM",
      "std_dev": 2.1,
      "explanation": "Weighted historical average adjusted for Pine 300 mm quality 5."
    }
  ]
}
```

`start_sheet_text` is the 70-character-wide plain-text render ready to
pipe to a thermal printer. `results` is the structured equivalent.

## POST /predict

Returns every cascade level's prediction for a single competitor —
useful for side-by-side operator views.

```json
{
  "competitor": { ... same schema as above ... },
  "wood": { ... },
  "event_code": "SB"
}
```

Response:

```json
{
  "best": {"value": 28.47, "method": "baseline", "confidence": "MEDIUM"},
  "all": [
    {"value": 28.47, "method": "baseline", "confidence": "MEDIUM"},
    {"value": null, "method": "llm", "confidence": "LOW", "explanation": "Ollama unreachable"},
    {"value": null, "method": "ml", "confidence": "LOW", "explanation": "insufficient training data"},
    {"value": 28.50, "method": "panel", "confidence": "VERY LOW"}
  ]
}
```

## POST /simulate

```json
{
  "entries": [
    {"name": "Alice", "mark": 3, "predicted_time": 28.47, "std_dev": 2.1}
  ],
  "num_simulations": 100000
}
```

Response:

```json
{
  "rating": "Very Good",
  "spread_percent": 3.4,
  "win_rates": {"Alice": 0.203, "Bob": 0.195, ...},
  "summary": "..."
}
```

## POST /results

Record a single tournament result to the local SQLite store.

```json
{
  "competitor_name": "Alice Smith",
  "event_code": "SB",
  "raw_time": 28.4,
  "species": "Pine",
  "size_mm": 300,
  "quality": 5,
  "result_date": "2026-04-25",
  "heat_id": "SB-H3"
}
```

Response:

```json
{"inserted": true, "duplicate": false}
```

Duplicates are silently accepted (returned as `"duplicate": true,
"inserted": false`).

## GET /results/{competitor}

Query parameters: `event_code=SB` (optional), `limit=50` (optional,
default 100).

```json
{
  "competitor": "Alice Smith",
  "results": [
    {"event_code": "SB", "time_seconds": 28.4, "species": "Pine",
     "diameter_mm": 300, "quality": 5, "result_date": "2026-04-25"}
  ]
}
```

## Deployment shape

The REST API is designed to run behind a reverse proxy (nginx,
caddy). Typical Docker shape:

```
FROM python:3.12-slim
RUN pip install strathmark[api,llm,ml,db]
CMD ["uvicorn", "strathmark.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

Set the env vars from [Installation](Installation#environment-variables)
before the container starts — they are read at module-import time.

## Testing

- `tests/test_api.py` — FastAPI `TestClient` coverage of every
  endpoint, including 400/422/500 paths.

The suite skips gracefully when `fastapi` is not installed (`pytest.
importorskip("fastapi")`).

## Security note

The API does not implement authentication. Deploy it behind a reverse
proxy with an auth layer (JWT, basic auth, network-level ACL) or
inside a private network. Posting random competitor histories from
the open internet will pollute the local store.
