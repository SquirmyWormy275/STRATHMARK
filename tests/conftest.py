"""Shared test fixtures and helpers.

Centralizes the live-DB guard so multiple test files don't carry duplicate
copies of the production-project-ref check. Tests that hit a real Supabase
project decorate with `@live_db_required` (or `@pytest.mark.skipif(...,
reason=LIVE_DB_SKIP_REASON)` for finer control).

Per the global Test Isolation rule -- "Tests MUST NEVER write to or pollute
the production database." -- live tests refuse to run against the production
project ref. Point STRATHMARK_SUPABASE_URL at an isolated test project AND
set STRATHMARK_TEST_DB=1 to opt in.
"""

from __future__ import annotations

import os

import pytest

PRODUCTION_PROJECT_REF = "iordtvxryrdhqvdkfgzf"


def _is_live_db_test_environment() -> tuple[bool, str]:
    """Return (eligible, reason). Eligible only if explicit opt-in AND non-prod."""
    if not os.environ.get("STRATHMARK_TEST_DB"):
        return False, "Live DB tests require STRATHMARK_TEST_DB=1"
    url = os.environ.get("STRATHMARK_SUPABASE_URL", "")
    if not url:
        return False, "STRATHMARK_SUPABASE_URL is unset"
    if PRODUCTION_PROJECT_REF in url:
        return (
            False,
            f"REFUSING to run live tests against production project ref "
            f"{PRODUCTION_PROJECT_REF!r}. Point STRATHMARK_SUPABASE_URL at an "
            f"isolated test project before setting STRATHMARK_TEST_DB=1.",
        )
    return True, "live DB tests enabled"


_LIVE_OK, LIVE_DB_SKIP_REASON = _is_live_db_test_environment()
live_db_required = pytest.mark.skipif(not _LIVE_OK, reason=LIVE_DB_SKIP_REASON)
