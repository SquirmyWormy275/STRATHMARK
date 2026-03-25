# Changelog

All notable changes to STRATHMARK will be documented in this file.

## [0.3.1] - 2026-03-24

### Added
- 221 new tests across 9 test files, bringing total from 446 to 667
- Regression tests for fixed bugs: banker's rounding, decay weights, timeout filtering, absolute variance, mark floor/ceiling enforcement
- Boundary tests for extreme values: 50-competitor fields, diameters 50-600mm, quality clamping, consistency rating thresholds
- Config invariant tests: frozen dataclass enforcement, rules consistency, threshold ordering, ML/LLM config sanity
- Extended decay tests: exponential precision, date type handling, adaptive vs fixed weighting, robust MAD clipping
- Extended store tests: CRUD, duplicate detection, DataFrame round-trips, column aliases, date handling
- Extended visualization tests: ASCII bar chart accuracy, line width, percentage sums, large field rendering
- Predictor regression tests: cascade priority, tournament weighting with num_tournament_rounds, division fallback
- Full pipeline integration tests: predict-calculate-simulate round-trips, multi-event days, store round-trips, simulation determinism
- Wood boundary tests: species multipliers, Janka hardness monotonicity, event scaling exponents
