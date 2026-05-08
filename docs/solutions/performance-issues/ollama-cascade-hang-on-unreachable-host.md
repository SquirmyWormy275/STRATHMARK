---
type: bug
problem_type: performance_issue
severity: critical
symptoms:
  - "Prediction cascade hung ~120s per call when Ollama was unreachable"
  - "Pro-Am Manager on Railway could not reach local Ollama; calls timed out instead of falling through"
  - "Race-day mark generation blocked on LLM tier despite ML/baseline fallbacks being available"
tags:
  - "llm"
  - "ollama"
  - "cascade"
  - "race-day"
  - "railway"
confidence: high
created: 2026-04-15
source: "internal knowledge"
---

# Ollama Cascade Hang on Unreachable Host

## Problem
When STRATHMARK ran in environments that could not reach the local Ollama host (Railway, container deploys, event laptop with Ollama crashed), every prediction call hung ~120s on TCP timeouts before the cascade fell through to ML / Baseline / Panel. On race day, this made the system unusable.

## Root Cause
The Ollama HTTP client used default socket timeouts and had a retry loop (MAX_RETRIES=2), so a single dead-host call paid 3 x full-timeout before returning None. There was no explicit `(connect, read)` timeout tuple, no single-attempt mode, and no cloud fallback between the LLM tier and ML.

## Solution
V0.4.0 (commit c7f5600):
- Explicit `(3s, 15s)` connect/read timeout tuple on every Ollama HTTP call
- `MAX_RETRIES=0` default — single attempt only for race-day
- Broad except clause catches `ConnectionRefusedError`, `ConnectionError`, `Timeout`, `RequestException`, plus any unexpected exception; logs a warning, returns None, lets the cascade fall through cleanly
- New Gemini 2.0 Flash-Lite tier between Ollama and ML, gated on `GEMINI_API_KEY`
- New env vars resolved at call time (not import time) so tests can monkeypatch and Railway can swap hosts without redeploy: `OLLAMA_HOST`, `STRATHMARK_OLLAMA_CONNECT_TIMEOUT`, `STRATHMARK_OLLAMA_READ_TIMEOUT`, `GEMINI_API_KEY`
- Set `OLLAMA_HOST=""` or `"disabled"` to skip the Ollama tier entirely

Total wall-clock when Ollama is down: <5s (was ~120s).

## Prevention
- Any external-service call in the prediction hot path MUST have an explicit `(connect, read)` timeout tuple — never rely on defaults
- Race-day code paths default to `retries=0`; retries are a development-time convenience, not a production one
- When adding a new tier to the cascade, test the "tier is down" path explicitly with a mocked failure — full-suite tests pass while the race-day path hangs
- Env vars that may change between dev and prod (hosts, keys) should be resolved at call time, not frozen at module-import
