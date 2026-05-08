---
type: knowledge
problem_type: architecture_decision
severity: critical
tags:
  - "predictor"
  - "cascade"
  - "llm"
  - "ml"
confidence: high
created: 2026-04-15
source: "internal knowledge"
last_updated: 2026-04-21
---

# Prediction Cascade Order

## Context
STRATHMARK must produce a predicted time for every competitor in every event, even when data is sparse, LLM is unreachable, the ML model isn't trained, or the competitor is brand new. The cascade is ordered from "most authoritative" to "most universal."

## Pattern
Order (top wins when present and valid):
1. **Manual** — explicit human override (tournament_time, coach-provided target)
2. **LLM** — Ollama (local) then Gemini (cloud fallback). JSON-schema-enforced output. Temperature 0 when schema active. Quality==5 early-return.
3. **ML** — XGBoost model (26 features, temporal CV)
4. **Baseline** — tournament-weighted historical average with decay
5. **Panel fallback** — event-level baseline or panel-assigned mark

Every tier must fail through cleanly (return None, not raise) so the cascade continues. Dead tiers must fail fast (<5s wall-clock) so race-day throughput stays usable.

## Rationale
- Manual wins because it represents a coach's explicit judgment that supersedes any model
- LLM sits above ML because LLM reasoning with JSON schema produces better results on small-N competitors than ML with sparse features (quality==5 specifically short-circuits because a perfect-quality log is unambiguous)
- ML sits above baseline because the 26-feature model captures species, diameter, and form signals baseline can't
- Baseline sits above panel because an individual's history beats a population average
- Panel sits last because it's always available

JSON-schema-enforced output, `temperature=0.0` when schema is active, and the `quality==5` early-return were all introduced in v0.3.0 (Phase 3 LLM integration) — before that, the LLM path was a shadow `predict_with_llm()` that regex-parsed freeform responses and wasn't wired into the canonical cascade. Gemini was added in v0.4.0 inside the LLM tier (not as a separate tier) because the cloud LLM produces the same JSON schema output as Ollama; they're interchangeable within the tier. See [`../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md`](../best-practices/llm-cascade-and-monte-carlo-tuning-2026-04-21.md) for the v0.3.0 hardening narrative.

## Examples
Cascade priority test (`test_llm_beats_cascade_without_manual`): when no manual override is present and LLM is available, LLM output wins over ML. Note: ML has a confidence penalty such that ML beats LLM only in specific low-confidence LLM scenarios — see `select_best_prediction()` and `_expected_error()` in `predictor.py` for the tiebreaker logic.

Never short-circuit the cascade inside a tier's implementation. If Ollama is down, return None — do NOT fall back to ML directly from inside `call_ollama()`. The cascade decides; the tier reports.
