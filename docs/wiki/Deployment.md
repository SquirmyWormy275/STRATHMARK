# Deployment

This page is the operational run-book for taking STRATHMARK to a live
event. It summarises and extends `docs/DEPLOYMENT.md`; treat that file
as the canonical race-day reference and this wiki page as the
higher-level companion.

The 2026 Missoula Pro-Am (April 24–25, 2026) is the primary target,
but every procedure applies to any event that uses STRATHMARK as its
handicap engine.

## Pre-event checklist (morning of Day 1)

1. **Env vars.** Set on the event laptop before the first
   `import strathmark`:
   ```powershell
   $env:STRATHMARK_SUPABASE_URL = "https://<project>.supabase.co"
   $env:STRATHMARK_SUPABASE_KEY = "<key>"
   ```
   ```bash
   export STRATHMARK_SUPABASE_URL="https://<project>.supabase.co"
   export STRATHMARK_SUPABASE_KEY="<key>"
   ```
2. **Ollama.** `ollama serve` in a dedicated terminal.
   Confirm: `curl http://localhost:11434/api/tags` lists `qwen3.5:9b`.
3. **Validation script.**
   ```bash
   python scripts/validate_deployment.py
   ```
   Expected final line:
   ```
   READY FOR DEPLOYMENT: [YES]
   ```
4. **Fix any `[FAIL]`.** The script prints remediation hints under the
   summary. Do not start the event with any critical check failing.
5. **Version pin.** `pip show strathmark` must match the version
   pinned in the Pro-Am Manager's dependency list. Mismatched versions
   are the number-one cause of "it worked in testing" race-day bugs.

## During the event

1. The Pro-Am Manager calls `HandicapCalculator` directly — no manual
   STRATHMARK interaction is needed.
2. Monitor the Ollama terminal for timeouts. Single-event timeouts
   are expected and benign; a sustained stream means Ollama died and
   the cascade is silently falling back.
3. If Ollama dies mid-event, either (a) let the cascade fall through
   to ML + baseline, or (b) set `OLLAMA_HOST=disabled` and restart the
   Pro-Am Manager to stop attempting the tier at all.

## Post-event checklist

1. Export results from the Pro-Am Manager (CSV or JSON).
2. Dry run ingestion:
   ```bash
   python scripts/ingest_proam_results.py --input results.csv \
       --show "Missoula Pro-Am 2026"
   ```
3. Resolve unmapped competitors when prompted:
   - existing `competitor_id` — enter it.
   - new competitor — press `r` to register via `register_competitor`.
   - skip row — press `s`.
4. Commit:
   ```bash
   python scripts/ingest_proam_results.py --input results.csv \
       --show "Missoula Pro-Am 2026" --commit
   ```
5. Verify:
   ```python
   from strathmark.db import pull_results

   df = pull_results()
   print(df[df["show_name"] == "Missoula Pro-Am 2026"].shape)
   ```
6. Inspect the `sync_log` table in Supabase for the new batch entry.

## Scripts reference

### `scripts/validate_deployment.py`

Read-only pre-event validator. Runs:

- Supabase connectivity test (`pull_competitors()` and
  `pull_results()` with a 1-row limit).
- Baseline prediction smoke test.
- LLM prediction smoke test (if Ollama reachable).
- Mark-sheet build smoke test.
- Result-ingestion validation (dry run, does not write).

Exits 0 on success, 1 on any failure. Safe to run from CI.

### `scripts/ingest_proam_results.py`

Bulk post-event ingestion. CLI options:

- `--input results.csv` — path to CSV export.
- `--show "Missoula Pro-Am 2026"` — show name; recorded in `sync_log`.
- `--commit` — actually write. Default is dry run.
- `--interactive` — prompt for unmapped competitors (default).
- `--strict` — abort on first unmapped row (for CI).

The script never silently drops a row. A summary of inserted,
skipped, and error counts is printed at the end.

### `scripts/measure_baseline_mae.py`

Post-mortem analysis script. Computes the mean absolute error of the
baseline tier across the current result history. Use after a major
data update to confirm the baseline is still calibrated.

## Failure modes

### Supabase unreachable

- Cascade falls through to the local SQLite store.
- Ingestion fails fast; re-run `ingest_proam_results.py` when the
  connection comes back.
- Fix: check venue wifi, fall back to a hotspot.

### Ollama returns garbage

- Tier returns `None`; cascade falls through.
- Fix: confirm the model is pulled (`ollama list`), consider
  restarting `ollama serve`, or kill with `OLLAMA_HOST=disabled`.

### Marks look wrong but no errors

1. `pytest tests/test_calculator.py` — confirms invariants still
   hold.
2. Capture the exact `CompetitorRecord` and `WoodProfile`.
3. Open a ticket with the payload. The issue is either a data bug
   or a mis-rated wood quality.

### ML training fails

- Requires 100+ total records, 75+ per event type.
- Cascade falls through to baseline silently.
- Check `_log` output at `WARNING` level — training failure is
  logged with the exception.

## Environment variable quick reference

Full table in [Installation](Installation#environment-variables).
The race-day set is:

| Variable | Typical value |
|----------|---------------|
| `STRATHMARK_SUPABASE_URL` | `https://<project>.supabase.co` |
| `STRATHMARK_SUPABASE_KEY` | Supabase service or anon key |
| `OLLAMA_HOST` | `http://localhost:11434` or `disabled` |
| `GEMINI_API_KEY` | unset on race day (Ollama is primary) |

## CI/CD

- `.github/workflows/ci.yml` — runs on every push and PR: ruff lint,
  pytest across Python 3.10 / 3.12 / 3.13, Ubuntu and Windows, build
  and install check.
- `.github/workflows/publish.yml` — manual `workflow_dispatch` that
  builds the wheel and publishes to PyPI via trusted publishing.

Downstream tournament managers pin STRATHMARK by version and rely on
the published wheels. For live editing against a dev branch, use
`pip install -e ./STRATHMARK` instead of the wheel.

## Troubleshooting index

Full index with PowerShell and bash examples lives in
`docs/DEPLOYMENT.md`. The entries covered:

- `RuntimeError: STRATHMARK_SUPABASE_URL is not set`
- Cascade returns `panel` for an experienced competitor
- `validate_deployment.py` says Ollama is NOT RUNNING but it is
- `ingest_proam_results.py` reports "competitor_id not found"
- Marks look wrong but no errors are printed
