---
type: knowledge
problem_type: best_practice
severity: medium
tags:
  - "config"
  - "env-vars"
  - "testing"
  - "pytest"
  - "monkeypatch"
confidence: high
created: 2026-04-21
source: "Apr 7 deployment-readiness session — TestLLMConfigEnvOverrides"
---

# Testing Import-Time Env Vars with `importlib.reload`

## Context
STRATHMARK resolves some env vars at module-import time (for performance and immutability — frozen dataclasses callers can rely on without per-call overhead). This is fast and correct in production, but it creates a test-isolation trap: `monkeypatch.setenv(...)` does not affect values that were already frozen during a prior import.

The trap:
```python
def test_timeout_override(self, monkeypatch):
    monkeypatch.setenv("STRATHMARK_OLLAMA_TIMEOUT", "2")
    from strathmark.config import llm_config

    assert llm_config.TIMEOUT_SECONDS == 2  # FAILS — still shows the default 30
```

Python caches modules in `sys.modules`. The `@dataclass(frozen=True)` for `LLMConfig` ran once, at first import, before the monkeypatch took effect. All later imports return the same cached instance.

## Pattern

Use `importlib.reload` to re-execute the module after setting the env var:

```python
# tests/test_config.py:90-142
import importlib


class TestLLMConfigEnvOverrides:
    def _reload_config(self):
        import strathmark.config as cfg

        return importlib.reload(cfg)

    def test_timeout_override(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_TIMEOUT", "2")
        cfg = self._reload_config()
        assert cfg.llm_config.TIMEOUT_SECONDS == 2

    def test_invalid_int_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_TIMEOUT", "not-a-number")
        cfg = self._reload_config()
        assert cfg.llm_config.TIMEOUT_SECONDS == 30

    def teardown_method(self):
        # Restore the unmodified module so later tests see clean config
        import strathmark.config as cfg

        importlib.reload(cfg)
```

Three non-obvious requirements:
1. **Reload the module, not the dataclass instance.** `importlib.reload(llm_config)` does not exist; you reload the containing module (`strathmark.config`).
2. **Use the returned module.** `importlib.reload()` returns the re-executed module. Capture it as `cfg = self._reload_config()`; do not re-import after the reload because the module reference hasn't moved in the calling scope until the next `import`.
3. **Restore in teardown.** Other tests that import `llm_config` after this test runs see whatever state monkeypatch left. `teardown_method` reloads with the env var cleared (monkeypatch auto-reverts at end-of-test) so global module state returns to defaults.

## Rationale
Import-time resolution is a deliberate trade-off: frozen dataclass config is fast (no per-call env lookup), immutable (callers can trust the value won't change mid-process), and self-documenting (the config surface is a single class). The cost is that tests require `importlib.reload`.

STRATHMARK uses both patterns:
- **Import-time** — `LLMConfig` fields (timeouts, retries, URLs that don't change mid-process). Requires reload to test.
- **Call-time** — `get_ollama_url()` and `is_ollama_disabled()` helpers that re-read env vars on every call. Testable directly with `monkeypatch.setenv` + no reload. Used for operationally hot-swappable values like `OLLAMA_HOST` on Railway.

Pick import-time for stability, call-time for operational flexibility.

## When to Apply
- Any test for a new `STRATHMARK_*` env var read inside `config.py`
- Any frozen-dataclass config where fields come from `os.getenv()`
- Tests using `monkeypatch` on env vars that the source reads once at import

## When to skip
- If the value is read call-time via a helper function — `monkeypatch.setenv` works directly
- If the env var only affects behavior at a specific entry point (e.g., CLI arg parsing) — test that entry point, not the config module

## Examples
- [`tests/test_config.py:90-142`](../../../tests/test_config.py) — `TestLLMConfigEnvOverrides` — the canonical pattern. `test_defaults_when_env_unset` is the companion test that verifies `teardown_method`'s reload actually restores defaults (including the race-day fail-fast `MAX_RETRIES == 0`).
- [`tests/test_config.py:TestOllamaHostHelpers`](../../../tests/test_config.py) — contrast case: `get_ollama_url()` is call-time, tested WITHOUT reload

## Related
- [`../configuration-issues/env-vars-resolved-at-import-time.md`](../configuration-issues/env-vars-resolved-at-import-time.md) — the original bug that prompted the helper + pattern
