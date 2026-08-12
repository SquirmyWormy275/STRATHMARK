"""
Deployment fallback path tests.

These tests explicitly verify that the prediction cascade and ingestion
helpers degrade gracefully when external dependencies are missing:

    - Supabase unreachable
    - Ollama unreachable
    - Competitor with zero history (panel mark fallback)
    - Result ingestion with malformed input

They are deliberately written without network access so they run on every
CI environment.
"""

from __future__ import annotations

import time
from datetime import date
from unittest import mock

import pytest

from strathmark.calculator import HandicapCalculator
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    WoodProfile,
    get_best_prediction,
)

# ---------------------------------------------------------------------------
# 4A -- Supabase unreachable
# ---------------------------------------------------------------------------


class TestNoSupabase:
    def test_pull_results_raises_when_env_unset(self, monkeypatch):
        """Without env vars, _get_client() raises a clear RuntimeError."""
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)

        # Force the cached client to be re-created
        import strathmark.db as db

        db._client = None

        from strathmark.db import pull_results

        with pytest.raises(RuntimeError, match="STRATHMARK_SUPABASE"):
            pull_results()

    def test_push_results_dicts_dry_run_works_without_supabase(self, monkeypatch):
        """Dry-run validation must not require a live Supabase connection."""
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        import strathmark.db as db

        db._client = None

        from strathmark.db import push_results_dicts

        result = push_results_dicts(
            [
                {
                    "competitor_id": "C0001",
                    "event_code": "SB",
                    "time_seconds": 25.0,
                    "size_mm": 275,
                    "species_code": "S05",
                    "date": "2026-04-25",
                }
            ],
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["inserted"] == 0
        # Should not raise -- error list may contain a connection warning, that's fine

    def test_packaged_core_handles_no_history_without_supabase(self):
        """A new competitor uses the packaged population prior offline."""
        rec = CompetitorRecord(name="Brand New", history=[], division="Open")
        wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)

        pred = get_best_prediction(rec, wood, "SB")

        assert pred is not None
        assert pred.method == "baseline"
        assert pred.metadata["source"] == "conditional_population_prior"
        assert 3.0 <= pred.value <= 183.0
        assert pred.confidence == "LOW"


# ---------------------------------------------------------------------------
# 4B -- Ollama unreachable
# ---------------------------------------------------------------------------


class TestNoOllama:
    def setup_method(self):
        from strathmark.llm import reset_ollama_status

        reset_ollama_status()

    def test_check_ollama_connection_returns_false_on_connection_error(self):
        import requests

        from strathmark.llm import check_ollama_connection, reset_ollama_status

        reset_ollama_status()
        with mock.patch("strathmark.llm.requests.get", side_effect=requests.ConnectionError):
            assert check_ollama_connection(force=True) is False

    def test_call_ollama_returns_none_on_connection_error(self):
        import requests

        from strathmark.llm import call_ollama, reset_ollama_status

        reset_ollama_status()
        with mock.patch("strathmark.llm.requests.post", side_effect=requests.ConnectionError):
            result = call_ollama("hello")
            assert result is None

    def test_cascade_skips_llm_and_returns_quickly_when_ollama_down(self):
        """Without Ollama, get_best_prediction must still return within seconds."""
        import requests

        from strathmark.llm import reset_ollama_status

        reset_ollama_status()

        rec = CompetitorRecord(
            name="Alice",
            history=[
                HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1)),
                HistoricalResult("SB", 27.9, "Pine", 300, 5, date(2024, 11, 15)),
                HistoricalResult("SB", 29.1, "Pine", 300, 5, date(2024, 6, 20)),
            ],
            division="Open",
        )
        # Use quality != 5 so the LLM call is not short-circuited (predict_with_llm
        # has a fast path for quality=5 that returns without ever touching Ollama).
        wood = WoodProfile(species="Pine", diameter_mm=300, quality=7)
        llm_client = {"url": "http://localhost:11434", "model": "qwen3.5:9b", "timeout": 5}

        with (
            mock.patch("strathmark.llm.requests.post", side_effect=requests.ConnectionError),
            mock.patch("strathmark.llm.requests.get", side_effect=requests.ConnectionError),
        ):
            t0 = time.monotonic()
            pred = get_best_prediction(rec, wood, "SB", llm_client=llm_client)
            elapsed = time.monotonic() - t0

        assert pred is not None
        assert pred.method != "llm"
        assert elapsed < 5.0, f"cascade took {elapsed:.2f}s -- too slow with Ollama down"


# ---------------------------------------------------------------------------
# 4C -- New competitor (no history)
# ---------------------------------------------------------------------------


class TestNewCompetitor:
    def test_zero_history_returns_population_or_broad_prior(self):
        rec = CompetitorRecord(name="First Timer", history=[], division="Novice")
        wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)

        pred = get_best_prediction(rec, wood, "SB")

        assert pred is not None
        assert pred.value > 0
        assert pred.method in {"baseline", "panel"}
        assert pred.metadata["source"] in {
            "conditional_population_prior",
            "broad_event_prior",
        }

    def test_zero_history_in_calculate_produces_valid_mark(self):
        rec_new = CompetitorRecord(name="Newbie", history=[], division="Open")
        rec_pro = CompetitorRecord(
            name="Pro",
            history=[
                HistoricalResult("SB", 22.0, "Pine", 300, 5, date(2025, 3, 1)),
                HistoricalResult("SB", 21.5, "Pine", 300, 5, date(2024, 11, 15)),
                HistoricalResult("SB", 22.3, "Pine", 300, 5, date(2024, 6, 20)),
            ],
            division="Open",
        )
        calc = HandicapCalculator()
        wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)
        results = calc.calculate([rec_new, rec_pro], wood, "SB")

        assert len(results) == 2
        for r in results:
            assert 3 <= r.mark <= 183


# ---------------------------------------------------------------------------
# 4D -- push_results_dicts validation rules (no network required)
# ---------------------------------------------------------------------------


class TestPushResultsDictsValidation:
    def _good_row(self, **overrides):
        row = {
            "competitor_id": "C0001",
            "event_code": "SB",
            "time_seconds": 25.0,
            "size_mm": 275,
            "species_code": "S05",
            "date": "2026-04-25",
        }
        row.update(overrides)
        return row

    def test_missing_field_is_reported(self):
        from strathmark.db import push_results_dicts

        bad = self._good_row()
        del bad["event_code"]
        result = push_results_dicts([bad], dry_run=True)
        assert any("missing required" in e for e in result["errors"])

    def test_invalid_event_code_is_reported(self):
        from strathmark.db import push_results_dicts

        result = push_results_dicts(
            [self._good_row(event_code="XX")],
            dry_run=True,
        )
        assert any("invalid event_code" in e for e in result["errors"])

    def test_time_below_floor_is_rejected(self):
        from strathmark.db import push_results_dicts

        result = push_results_dicts(
            [self._good_row(time_seconds=2.0)],
            dry_run=True,
        )
        assert any("outside" in e for e in result["errors"])

    def test_time_above_ceiling_is_rejected(self):
        from strathmark.db import push_results_dicts

        result = push_results_dicts(
            [self._good_row(time_seconds=200.0)],
            dry_run=True,
        )
        assert any("outside" in e for e in result["errors"])

    def test_empty_input_is_safe(self):
        from strathmark.db import push_results_dicts

        result = push_results_dicts([], dry_run=True)
        assert result["inserted"] == 0
        assert result["skipped"] == 0


# ---------------------------------------------------------------------------
# 4E -- format_proam_results parser
# ---------------------------------------------------------------------------


class TestFormatProamResults:
    def test_event_code_parsing_sb(self):
        from strathmark.db import format_proam_results

        out = format_proam_results(
            [
                {
                    "competitor_name": "Alice",
                    "event_name": "275mm Standing Block",
                    "time": 30.0,
                    "species": "S05",
                    "date": "2026-04-25",
                }
            ],
            competitor_lookup={"Alice": "C0001"},
        )
        assert out[0]["event_code"] == "SB"
        assert out[0]["size_mm"] == 275.0
        assert out[0]["competitor_id"] == "C0001"

    def test_event_code_parsing_uh(self):
        from strathmark.db import format_proam_results

        out = format_proam_results(
            [
                {
                    "competitor_name": "Bob",
                    "event_name": "300 Underhand",
                    "time": 22.0,
                    "species": "S05",
                    "date": "2026-04-25",
                }
            ],
            competitor_lookup={"Bob": "C0002"},
        )
        assert out[0]["event_code"] == "UH"
        assert out[0]["size_mm"] == 300.0

    def test_unmapped_competitor_returns_none_id(self):
        from strathmark.db import format_proam_results

        out = format_proam_results(
            [
                {
                    "competitor_name": "Stranger",
                    "event_name": "275mm SB",
                    "time": 28.0,
                    "species": "S05",
                    "date": "2026-04-25",
                }
            ],
            competitor_lookup={},
        )
        assert out[0]["competitor_id"] is None
        assert out[0]["_competitor_name"] == "Stranger"
