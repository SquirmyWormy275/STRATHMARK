---
type: bug
problem_type: configuration_issue
severity: medium
symptoms:
  - "Setting STRATHMARK_OLLAMA_URL after import had no effect"
  - "Tests that monkeypatched OLLAMA_HOST saw no change"
  - "Railway deploy needed a redeploy to pick up new Ollama endpoint"
tags:
  - "config"
  - "env-vars"
  - "testability"
confidence: high
created: 2026-04-15
source: "internal knowledge"
---

# Env Vars Resolved at Module-Import Time

## Problem
The original `LLMConfig` dataclass read env vars (`STRATHMARK_OLLAMA_URL`, `STRATHMARK_OLLAMA_TIMEOUT`, `STRATHMARK_OLLAMA_MAX_RETRIES`) at module-import time and froze them. Any env-var change after the first `import strathmark` had no effect. Tests using `monkeypatch.setenv(...)` saw the frozen value, not their patched value.

## Root Cause
Frozen dataclass populated by `os.getenv()` at import — classic "config is a singleton, evaluated once" anti-pattern in Python. Works fine in single-run CLIs, breaks in tests and long-lived processes that need to reconfigure.

## Solution
V0.4.0 added env vars that are resolved at CALL time, not import time:
- `OLLAMA_HOST` (resolved inside `call_ollama()` via helper)
- `STRATHMARK_OLLAMA_CONNECT_TIMEOUT`, `STRATHMARK_OLLAMA_READ_TIMEOUT`
- `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_CONNECT_TIMEOUT`, `GEMINI_READ_TIMEOUT`

Legacy `STRATHMARK_OLLAMA_URL` still wins for back-compat but is evaluated at import. The DEPLOYMENT.md "Environment variables" section documents which vars are import-time (legacy) vs call-time (new).

## Prevention
- Any env var that may legitimately change between imports (host, API key, timeout tuning) MUST be resolved at call time via a helper
- Import-time evaluation is acceptable ONLY for constants that are truly fixed for the process lifetime
- Test new config knobs with `monkeypatch.setenv(...)` in a subprocess-free pytest — if the patch doesn't take effect, the config is evaluated too early
