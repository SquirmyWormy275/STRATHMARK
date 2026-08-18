# STRATHEX Consumer Migration: 0.4.1 to 2.0.0

This is the downstream contract for STRATHEX 7.0.0. STRATHMARK 2.0.0 is published as
the immutable `v2.0.0` Git tag and GitHub release. STRATHEX records the exact commit
behind that release in its dependency pin for reproducible race-day installs.

## Runtime boundary

- Race-day default: direct Python `HandicapCalculator.calculate()`.
- Optional demonstration transport: one HTTP `POST /calculate` per field.
- The two transports use the same fixed exclusive `prediction_as_of` date and must
  return identical rounded predictions and marks.
- HTTP mode is explicit and fail-closed. It does not silently fall back to Python.
- Public `/calculate` is stateless. STRATHEX sends the complete eligible history on
  every request.

Do not call `/predict` once per competitor and then pass the selected values as
`manual_overrides`. That labels model output as manual authority, discards calibrated
intervals, and bypasses field-level joint optimization.

## Required inputs

Every field calculation supplies:

- stable `competitor_id` values;
- dated historical results strictly before the cutoff;
- event code (`SB` or `UH`);
- target species and diameter;
- one persisted `prediction_as_of` cutoff for the event.

Undated, same-day, future, and invalid-date rows are not V2 evidence. STRATHEX must not
silently claim they contributed.

## Changed semantics

V2 uses a prior-only hierarchical core and deterministic joint optimizer. The following
0.4.1 concepts are retired or inactive:

- numeric LLM prediction;
- the legacy XGBoost input;
- expected-error selection across three numeric methods;
- 65/80/90/97 percent same-tournament weighting;
- numeric wood-quality adjustment;
- numeric division, heat, field-strength, and tournament-context effects.

Manual overrides remain authoritative operator inputs. An inactive residual may be
promoted only through STRATHMARK's model-governance process.

## Required outputs

STRATHEX surfaces or retains each field result's forecast interval, performance
standard deviation, engine/model/calibration versions, evidence cutoff, optimizer and
metadata, warnings, degraded flag, provenance, and ignored factors. A rounded-gap
optimizer fallback or degraded result must be visible to the operator.

## Persistence and authority

Calculation and persistence are separate:

- Python and public HTTP calculation are stateless.
- `ResultStore` is local historical evidence.
- `PredictionLedger` is an immutable trusted calculation/settlement ledger.
- STRATHEX's Excel workbook remains its canonical race record; its local ResultStore
  write is best-effort and is not cross-store atomic.

STRATHEX does not use `/ledger/calculate` in this migration. That requires a later,
explicit rollout of request IDs, stable event identity, authentication, cutoff policy,
and settlement operations.

## Upgrade checks

Before changing the pinned STRATHMARK commit:

1. Run fixed-cutoff Python/HTTP parity tests.
2. Verify every response reports the expected engine version.
3. Confirm `/health` reports core and calibration `compatible_with_cutoff` and is not
   degraded for the operational cutoff.
4. Rehearse any ResultStore schema migration on a copied database.
5. Review mark changes against a representative, non-production fixture.
6. Update STRATHEX's runtime contract, release notes, help text, and wiki together.
