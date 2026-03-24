"""Tests for strathmark/llm.py — Ollama connection management."""

from strathmark.llm import check_ollama_connection, reset_ollama_status


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
