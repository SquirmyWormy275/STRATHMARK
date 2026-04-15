---
type: bug
problem_type: test_failure
severity: medium
symptoms:
  - "Tests mocked predictor.call_ollama but code imported llm.call_ollama"
  - "Mock never applied; real Ollama HTTP calls leaked into tests"
  - "Tests passed in CI but behaved differently locally with Ollama running"
tags:
  - "testing"
  - "mocking"
  - "imports"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Mocking the Wrong Import Target

## Problem
Tests used `mock.patch('strathmark.predictor.call_ollama', ...)` but `predictor.py` imported the function as `from strathmark.llm import call_ollama`. The patch targeted the wrong symbol; the real function ran in tests, leaking network calls.

## Root Cause
Python's `mock.patch` replaces the attribute on a specific module, not the function itself. If module A does `from B import foo`, patching `B.foo` does NOT affect calls made through `A.foo`.

## Solution
Changed patches to target the import site: `mock.patch('strathmark.predictor.call_ollama', ...)` — this patches the name bound in the importing module.

## Prevention
- Rule of thumb: `mock.patch` the symbol as it is referenced in the code under test, not where it is defined
- When a mock "isn't working," first check whether the SUT uses `from X import foo` (patch at SUT) vs `import X; X.foo()` (patch at source)
- Add a test assertion that the mock was called (`mock.assert_called_once()`) — silent no-ops from wrong patch targets surface immediately
