---
type: knowledge
problem_type: best_practice
severity: critical
tags:
  - "testing"
  - "database"
  - "isolation"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Test Isolation: Never Touch Production DB

## Context
STRATHMARK has two persistent stores: local SQLite (`~/.strathmark/results.db`) and Supabase/PostgreSQL (competitor history, ingestion sync log). Tests historically risked writing to both — either through default env-var lookups or by using the user's real SQLite file.

## Pattern
- Tests MUST use a dedicated test DB, an in-memory SQLite instance (`:memory:`), a pytest tmp_path-backed SQLite file, or a transaction that rolls back
- Tests MUST NOT read `STRATHMARK_SUPABASE_URL` / `STRATHMARK_SUPABASE_KEY` from the ambient environment; they must mock or override via fixture
- Before writing any new test that touches persistence, verify the test config uses isolated credentials

## Rationale
A single polluted row in Supabase during a backtest can shift MAE by several seconds and silently corrupt ensemble weight learning. A corrupted local SQLite file loses a competitor's session history mid-event.

## Examples
Local store tests use `tmp_path` fixture:
```python
def test_result_store_insert(tmp_path):
    store = ResultStore(db_path=tmp_path / "test.db")
    ...
```

Supabase tests MUST mock the client, not rely on env vars. If a test requires Supabase, it belongs in a separate `--runslow` or `--integration` marker and must never run in the default `pytest tests/` invocation.

This rule is also in the user's global CLAUDE.md — it applies to every project in the workspace, not just STRATHMARK.
