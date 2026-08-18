# STRATHEX Consumer

STRATHEX 7.0 uses STRATHMARK 2.0 as the sole authority for numeric field predictions
and marks.

## Connection

- Direct Python calculation is the offline race-day default.
- An explicitly selected REST mode sends one stateless `POST /calculate` request for
  the entire field.
- Both modes use the same exact STRATHMARK commit and persisted exclusive cutoff.
- REST mode fails closed; there is no silent transport fallback.

STRATHEX sends stable competitor IDs and the complete eligible dated history on every
calculation. It does not call `/predict` competitor by competitor or convert model
outputs into manual overrides.

## V2 behavior

V2 owns the complete field calculation and joint mark optimization. Numeric LLM,
legacy XGBoost input, three-method expected-error selection, same-tournament weighting,
and numeric quality adjustment are retired or inactive. Manual overrides remain real
operator authority.

STRATHEX carries the returned interval, uncertainty, version, cutoff, optimizer,
warning, degraded, provenance, and ignored-factor metadata to its operator display.

## Persistence

Public Python and REST calculations are stateless. STRATHEX's workbook, STRATHMARK's
local `ResultStore`, and the protected `PredictionLedger` have distinct jobs; a public
calculation does not automatically read or write either STRATHMARK store.

See the full [consumer migration contract](../STRATHEX_CONSUMER_MIGRATION.md).

