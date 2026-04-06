"""
LLM Integration (Ollama)
========================

Handles communication with a local Ollama instance for AI-enhanced predictions
and fairness assessments.

Connection status is cached to avoid repeated error messages within a session.
All functions return None gracefully if Ollama is unavailable.

Source references (STRATHEX):
    woodchopping/predictions/llm.py -> call_ollama()
    woodchopping/predictions/llm.py -> check_ollama_connection()
    woodchopping/predictions/llm.py -> reset_ollama_status()
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from strathmark.config import llm_config

# ---------------------------------------------------------------------------
# Module-level connection state (thread-safe)
# ---------------------------------------------------------------------------

_ollama_lock = threading.Lock()
_ollama_status: dict = {
    "available": None,  # None = unknown, True = available, False = unavailable
    "last_check": 0.0,
    "error_shown": False,
    "check_interval": 60,  # Re-check every 60 seconds
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_ollama_connection(
    base_url: str = "http://localhost:11434",
    force: bool = False,
) -> bool:
    """
    Check if Ollama is running. Results are cached for 60 seconds.

    Args:
        base_url: Ollama server base URL (without path).
        force: Ignore cache and perform a fresh check.

    Returns:
        True if Ollama is reachable, False otherwise.
    """
    global _ollama_status

    with _ollama_lock:
        now = time.time()
        if (
            not force
            and _ollama_status["available"] is not None
            and now - _ollama_status["last_check"] < _ollama_status["check_interval"]
        ):
            return _ollama_status["available"]

        try:
            resp = requests.get(f"{base_url}/api/tags", timeout=5)
            _ollama_status["available"] = resp.status_code == 200
            _ollama_status["last_check"] = now
            _ollama_status["error_shown"] = False
            return _ollama_status["available"]
        except Exception:
            _ollama_status["available"] = False
            _ollama_status["last_check"] = now
            return False


def reset_ollama_status() -> None:
    """Reset the connection status cache so the next call does a fresh check."""
    global _ollama_status
    with _ollama_lock:
        _ollama_status["available"] = None
        _ollama_status["last_check"] = 0.0
        _ollama_status["error_shown"] = False


def call_ollama(
    prompt: str,
    model: Optional[str] = None,
    num_predict: Optional[int] = None,
    ollama_url: Optional[str] = None,
    timeout: Optional[int] = None,
    format_schema: Optional[dict] = None,
) -> Optional[str]:
    """
    Send a prompt to Ollama and return the response text.

    Features:
        - Connection status caching (avoids repeated error messages)
        - Retry logic with simple backoff (max 2 retries)
        - Single error message per session
        - JSON schema enforcement via format parameter (Ollama v0.5+)

    Args:
        prompt: Text prompt to send.
        model: Ollama model name. Defaults to llm_config.DEFAULT_MODEL.
        num_predict: Maximum tokens to generate. Common values:
                     150  -- quality adjustment (JSON)
                     500  -- competitor profile (JSON)
                     2000 -- full fairness assessment (JSON)
        ollama_url: Full API URL including path. Defaults to llm_config.OLLAMA_URL.
        timeout: Request timeout seconds. Defaults to llm_config.TIMEOUT_SECONDS.
        format_schema: Optional JSON schema dict for structured output enforcement.
                       When provided, Ollama constrains generation to valid JSON
                       matching this schema via GBNF grammar logit masking.
                       Temperature is forced to 0.0 for schema compliance.

    Returns:
        Stripped response string, or None if Ollama is unavailable.
    """
    global _ollama_status

    # Fast-path: if we recently confirmed unavailability, skip
    if _ollama_status["available"] is False:
        now = time.time()
        if now - _ollama_status["last_check"] < _ollama_status["check_interval"]:
            return None

    if model is None:
        model = llm_config.DEFAULT_MODEL
    if num_predict is None:
        num_predict = 150
    if ollama_url is None:
        ollama_url = llm_config.OLLAMA_URL
    if timeout is None:
        timeout = llm_config.TIMEOUT_SECONDS

    max_retries = llm_config.MAX_RETRIES

    # When format_schema is set, force temperature to 0 for deterministic output
    temperature = 0.0 if format_schema is not None else 0.3

    for attempt in range(max_retries + 1):
        try:
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": num_predict,
                },
            }

            # Add JSON schema enforcement if provided
            if format_schema is not None:
                payload["format"] = format_schema

            response = requests.post(
                ollama_url,
                json=payload,
                timeout=timeout,
            )

            if response.status_code == 200:
                _ollama_status["available"] = True
                _ollama_status["last_check"] = time.time()
                _ollama_status["error_shown"] = False
                return response.json()["response"].strip()

            # Non-200 response — retry
            if attempt < max_retries:
                time.sleep(attempt + 1)
            else:
                if not _ollama_status["error_shown"]:
                    print(f"\n[WARN] Ollama returned status {response.status_code}")
                    _ollama_status["error_shown"] = True
                return None

        except requests.exceptions.ConnectionError:
            _ollama_status["available"] = False
            _ollama_status["last_check"] = time.time()
            if not _ollama_status["error_shown"]:
                print("\n" + "=" * 60)
                print("[WARN] OLLAMA NOT AVAILABLE")
                print("=" * 60)
                print("Cannot connect to Ollama. LLM predictions will be skipped.")
                print("To enable AI predictions, run: ollama serve")
                print("Then pull the model: ollama pull qwen3.5:9b")
                print("System will continue with Baseline and ML predictions only.")
                print("=" * 60 + "\n")
                _ollama_status["error_shown"] = True
            return None

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                if attempt == 0:
                    print("  [Ollama timeout, retrying...]")
                time.sleep(2 * (attempt + 1))
            else:
                if not _ollama_status["error_shown"]:
                    print(f"\n[WARN] Ollama timeout after {timeout}s")
                    _ollama_status["error_shown"] = True
                return None

        except Exception as exc:
            if attempt < max_retries:
                time.sleep(attempt + 1)
            else:
                if not _ollama_status["error_shown"]:
                    print(f"\n[WARN] Ollama error: {exc}")
                    _ollama_status["error_shown"] = True
                return None

    return None
