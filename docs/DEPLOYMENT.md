# STRATHMARK Live Deployment Guide

This document covers everything required to run STRATHMARK at a live event,
including the pre-event checklist, post-event ingestion, environment
configuration, LLM setup, and common failure modes.

It is written for the 2026 Missoula Pro-Am (April 24-25, 2026) but applies
to any event that uses STRATHMARK as its handicap engine.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `STRATHMARK_SUPABASE_URL` | yes (DB writes/reads) | Supabase project URL |
| `STRATHMARK_SUPABASE_KEY` | yes (DB writes/reads) | Supabase service or anon key |
| `STRATHMARK_DB_PATH` | no | Override the local SQLite store path (default `~/.strathmark/results.db`) |

When `STRATHMARK_SUPABASE_*` are missing, the prediction cascade still
works: it falls through to the local store, baseline, or panel-mark
fallback. Only ingestion (`push_results_dicts`, `register_competitor`)
strictly requires Supabase credentials.

## Ollama setup (event laptop)

Hardware target: RTX 4070 Laptop, 8GB VRAM. Recommended model:
`qwen3.5:9b` quantised to Q4_K_M (~6.6GB on disk, fits in VRAM).

```bash
# One-time setup
ollama pull qwen3.5:9b

# At event start
ollama serve
```

Verify Ollama is healthy:

```bash
curl http://localhost:11434/api/tags
```

If Ollama is not running, the cascade automatically skips the LLM level
and proceeds Manual -> ML -> Baseline -> Panel without hanging. The
connection failure is cached for 60 seconds so retries don't slow down
mark generation.

### Cloud fallback

When the event laptop has no GPU or Ollama crashes mid-event, the LLM
level can be skipped entirely (set `llm_client=None`). A future revision
will route to Gemini 2.0 Flash-Lite as a cloud fallback; until that ships,
treat the cascade as Manual -> ML -> Baseline -> Panel only.

## Pre-event checklist (run morning of Day 1)

1. Set the Supabase env vars on the event laptop.
2. Start Ollama: `ollama serve` (in a dedicated terminal).
3. Run the validation script:
   ```bash
   python scripts/validate_deployment.py
   ```
   Expected output:
   ```
   Supabase:           [OK]
   Predictions (base): [OK]
   Predictions (LLM):  [OK]
   Mark sheet:         [OK]
   Result ingestion:   [OK]
   READY FOR DEPLOYMENT: [YES]
   ```
4. If any check is `[FAIL]`, follow the remediation hint printed beneath
   the summary block. Do not start the event with any critical check
   failing.
5. Confirm `pip show strathmark` matches the version pinned in the
   Pro-Am Manager dependencies.

## Post-event checklist

1. Export results from the Pro-Am Manager (CSV or JSON, columns:
   `competitor_name, event_name, time, species, date`).
2. Run a dry-run ingestion to validate format:
   ```bash
   python scripts/ingest_proam_results.py --input results.csv \
       --show "Missoula Pro-Am 2026"
   ```
3. Resolve any unmapped competitors when prompted (enter an existing
   `competitor_id`, type `r` to register a new competitor, or `s` to
   skip).
4. Re-run with `--commit` to actually write to Supabase:
   ```bash
   python scripts/ingest_proam_results.py --input results.csv \
       --show "Missoula Pro-Am 2026" --commit
   ```
5. Verify the new rows landed:
   ```python
   from strathmark.db import pull_results
   df = pull_results()
   print(df[df["show_name"] == "Missoula Pro-Am 2026"].shape)
   ```
6. Check `sync_log` in Supabase for the new ingestion entry.

## Result ingestion programmatic API

If you'd rather call STRATHMARK from another process instead of the CLI
script, both helpers are exported from the package root:

```python
from strathmark import push_results_dicts, register_competitor

push_results_dicts(
    [
        {
            "competitor_id": "C0042",
            "event_code": "SB",
            "time_seconds": 24.7,
            "size_mm": 275,
            "species_code": "S05",
            "date": "2026-04-25",
        },
    ],
    source="pro-am-manager",
    show_name="Missoula Pro-Am 2026",
)

register_competitor("New Competitor", country="USA", state="MT", gender="M")
```

`push_results_dicts` returns `{'inserted', 'skipped', 'errors', 'dry_run'}`
and never silently drops a row. Validation rules:

- Required fields: `competitor_id, event_code, time_seconds, size_mm, species_code, date`.
- `event_code` must be `SB` or `UH`.
- `time_seconds` must be in `[3.0, 180.0]` (the system mark window).
- `competitor_id` must already exist in the `competitors` table.
- Duplicates (`competitor_id+event+time+size+date`) are silently skipped.

## Troubleshooting

### `RuntimeError: STRATHMARK_SUPABASE_URL is not set`
The env vars are missing in the current shell. On Windows PowerShell:
```powershell
$env:STRATHMARK_SUPABASE_URL = "https://<project>.supabase.co"
$env:STRATHMARK_SUPABASE_KEY = "<key>"
```
On bash/Git Bash:
```bash
export STRATHMARK_SUPABASE_URL="https://<project>.supabase.co"
export STRATHMARK_SUPABASE_KEY="<key>"
```

### Cascade returns `panel` for an experienced competitor
The competitor's history did not load. Confirm `pull_results()` returns
rows for that `competitor_id`, and that the rows have valid `Event`
values (`SB` or `UH`).

### `validate_deployment.py` says Ollama is NOT RUNNING but `ollama serve` is up
Verify the model is pulled (`ollama list` should include `qwen3.5:9b`)
and that nothing else is bound to port 11434. The validation script
forces a fresh connection check on every run, so a stale 60-second cache
is not the cause.

### `ingest_proam_results.py` reports "competitor_id not found"
The Pro-Am Manager exported a name that has no match in the Supabase
`competitors` table. Either:
- Re-run interactively and choose `r` to register the new competitor, or
- Pre-load the competitor with `register_competitor()` and re-run
  ingestion.

### Marks look wrong but no errors are printed
Run `pytest tests/test_calculator.py` to confirm the mark invariants
(floor 3, ceiling 183, gap logic) still hold. If those pass but a
specific event still looks wrong, capture the input and open a ticket
with the exact `CompetitorRecord`/`WoodProfile` payload.
