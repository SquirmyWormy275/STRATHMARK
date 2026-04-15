---
type: bug
problem_type: runtime_error
severity: medium
symptoms:
  - "Concurrent FastAPI requests could read/write _ollama_status simultaneously"
  - "Intermittent wrong cache state in multi-worker deployments"
tags:
  - "threading"
  - "fastapi"
  - "ollama"
  - "concurrency"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Ollama Status Cache Race Condition

## Problem
Module-level `_ollama_status` dict was read and mutated from `check_ollama_connection()` and `reset_ollama_status()` without synchronization. Under FastAPI concurrent load, two threads could observe and overwrite the cache state simultaneously, causing wrong availability reads.

## Root Cause
Shared module-level mutable state with no lock. The cache check-then-set pattern (`if stale: refresh`) has a classic race window.

## Solution
Added `_ollama_lock = threading.Lock()` and wrapped every read/write of `_ollama_status` in `with _ollama_lock:` in both `check_ollama_connection()` and `reset_ollama_status()`.

## Prevention
- Any module-level mutable state that is touched by FastAPI request handlers MUST be protected by a lock (or converted to request-scoped state)
- When adding a cache to a library function, ask "does this run under a web server?" before relying on single-threaded assumptions
- Prefer immutable/atomic updates (e.g., `dict.update` on a locally-built dict) over mutate-in-place when possible
