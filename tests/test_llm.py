"""Tests for strathmark/llm.py — Ollama connection management + Gemini fallback."""

import time
from unittest import mock

import pytest
import requests

from strathmark.llm import call_ollama, check_ollama_connection, reset_ollama_status


class TestOllamaConnection:
    def test_check_returns_bool(self):
        result = check_ollama_connection()
        assert isinstance(result, bool)

    def test_cached_check_fast(self):
        """Second call within 60s should use cache."""
        first = check_ollama_connection()
        second = check_ollama_connection()
        assert first == second

    def test_force_bypasses_cache(self):
        result = check_ollama_connection(force=True)
        assert isinstance(result, bool)

    def test_reset_clears_cache(self):
        check_ollama_connection()
        reset_ollama_status()
        # After reset, next check should re-query (still returns bool)
        result = check_ollama_connection()
        assert isinstance(result, bool)

    def test_bad_url_returns_false(self):
        reset_ollama_status()
        result = check_ollama_connection(base_url="http://localhost:99999", force=True)
        assert result is False

    def test_disabled_kill_switch_short_circuits(self, monkeypatch):
        """OLLAMA_HOST="disabled" must skip the network call entirely."""
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "disabled")
        reset_ollama_status()
        with mock.patch("strathmark.llm.requests.get") as mocked:
            assert check_ollama_connection(force=True) is False
            mocked.assert_not_called()


class TestCallOllamaFailFast:
    """Race-day fail-fast guarantees on call_ollama()."""

    def setup_method(self):
        reset_ollama_status()

    def test_no_retry_on_connection_error(self, monkeypatch):
        """One attempt only — no retry loop."""
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch(
            "strathmark.llm.requests.post",
            side_effect=requests.exceptions.ConnectionError(),
        ) as mocked:
            result = call_ollama("hello")
            assert result is None
            assert mocked.call_count == 1

    def test_no_retry_on_timeout(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch(
            "strathmark.llm.requests.post",
            side_effect=requests.exceptions.Timeout(),
        ) as mocked:
            result = call_ollama("hello")
            assert result is None
            assert mocked.call_count == 1

    def test_connection_refused_returns_none(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch(
            "strathmark.llm.requests.post", side_effect=ConnectionRefusedError()
        ):
            assert call_ollama("hello") is None

    def test_explicit_timeout_tuple_passed_to_requests(self, monkeypatch):
        """Verify the (CONNECT, READ) tuple is what reaches requests.post."""
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        captured: dict = {}

        def fake_post(url, json=None, timeout=None):
            captured["timeout"] = timeout
            raise requests.exceptions.ConnectionError()

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch("strathmark.llm.requests.post", side_effect=fake_post):
            call_ollama("hello")
        assert isinstance(captured["timeout"], tuple)
        assert len(captured["timeout"]) == 2
        # Default budget: 3s connect, 15s read
        assert captured["timeout"][0] == 3
        assert captured["timeout"][1] == 15

    def test_disabled_skips_ollama_entirely(self, monkeypatch):
        """OLLAMA_HOST="disabled" must skip the network call."""
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.setenv("OLLAMA_HOST", "disabled")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch("strathmark.llm.requests.post") as mocked:
            result = call_ollama("hello")
            assert result is None
            mocked.assert_not_called()

    def test_total_latency_under_5s_when_ollama_down(self, monkeypatch):
        """Race-day budget: dead Ollama must cost <5s wall-clock."""
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch(
            "strathmark.llm.requests.post",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            t0 = time.monotonic()
            call_ollama("hello")
            elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"call_ollama took {elapsed:.2f}s (race-day budget is 5s)"


class TestGeminiFallback:
    """Tier 2 cloud fallback discipline."""

    def setup_method(self):
        reset_ollama_status()
        # Reset the module-level warning flags so tests are isolated.
        import strathmark.llm as llm_mod

        llm_mod._gemini_warned_no_key = False
        llm_mod._gemini_warned_no_pkg = False

    def test_gemini_skipped_when_key_unset(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with mock.patch(
            "strathmark.llm.requests.post",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            assert call_ollama("hello") is None

    def test_gemini_invoked_when_ollama_down_and_key_set(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        # Build a fake google.generativeai module so the lazy import succeeds.
        fake_module = mock.MagicMock()
        fake_response = mock.MagicMock()
        fake_response.text = "GEMINI_RESPONSE_OK"
        fake_model = mock.MagicMock()
        fake_model.generate_content.return_value = fake_response
        fake_module.GenerativeModel.return_value = fake_model

        with (
            mock.patch(
                "strathmark.llm.requests.post",
                side_effect=requests.exceptions.ConnectionError(),
            ),
            mock.patch.dict("sys.modules", {"google.generativeai": fake_module}),
        ):
            result = call_ollama("hello")

        assert result == "GEMINI_RESPONSE_OK"
        fake_module.configure.assert_called_once_with(api_key="fake-key")
        fake_model.generate_content.assert_called_once()

    def test_gemini_failure_returns_none(self, monkeypatch):
        """Gemini exceptions must fall through cleanly to ML/Baseline."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        fake_module = mock.MagicMock()
        fake_module.GenerativeModel.side_effect = RuntimeError("quota exceeded")

        with (
            mock.patch(
                "strathmark.llm.requests.post",
                side_effect=requests.exceptions.ConnectionError(),
            ),
            mock.patch.dict("sys.modules", {"google.generativeai": fake_module}),
        ):
            result = call_ollama("hello")

        assert result is None

    def test_gemini_missing_package_returns_none(self, monkeypatch):
        """Missing google-generativeai package must be a graceful no-op."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        # Force ImportError by stubbing the module to None.
        with (
            mock.patch(
                "strathmark.llm.requests.post",
                side_effect=requests.exceptions.ConnectionError(),
            ),
            mock.patch.dict("sys.modules", {"google.generativeai": None}),
        ):
            result = call_ollama("hello")

        assert result is None
