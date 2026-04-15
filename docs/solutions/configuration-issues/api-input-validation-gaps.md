---
type: bug
problem_type: configuration_issue
severity: high
symptoms:
  - "API accepted negative times, out-of-range diameters, invalid quality scores"
  - "Invalid date strings silently parsed as None instead of returning 422"
  - "API version field hardcoded to 0.1.0, drifted from package __version__"
tags:
  - "api"
  - "fastapi"
  - "validation"
  - "boundary"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# API Input Validation Gaps

## Problem
Three QA findings against `strathmark/api.py`:
- ISSUE-001: `app = FastAPI(version="0.1.0", ...)` was hardcoded and drifted from `__version__`
- ISSUE-002: `time_seconds`, `diameter_mm`, `quality` were accepted without range validation at the API boundary
- ISSUE-003: `_parse_date()` silently returned None on invalid date strings, so malformed dates were accepted and stored

## Root Cause
API layer treated Pydantic models as structural validation only and delegated semantic validation to the calculator. Values that were structurally valid but semantically nonsense (negative time, quality=99) passed through.

## Solution
- Import `__version__` from package and use it in the FastAPI constructor
- Add Pydantic `Field(..., gt=0, le=180)` style constraints on `time_seconds`, `diameter_mm`, `quality`
- Add `strict=True` kwarg to `_parse_date()` that raises `HTTPException(422, ...)` on `record_result` instead of silently returning None

## Prevention
- Every API endpoint MUST validate numeric ranges at the Pydantic model, not rely on calculator invariants
- Every version-like constant MUST be read from a single source of truth (`__version__`) — grep for string literals like `"0.1.0"` before each release
- `_parse_date`-style helpers with a permissive mode should default to strict; opt-in permissive, not opt-in strict
