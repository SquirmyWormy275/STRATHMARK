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

import time
from typing import Optional

import requests

from strathmark.config import llm_config


# ---------------------------------------------------------------------------
# Module-level connection state
# ---------------------------------------------------------------------------

_ollama_status: dict = {
    'available': None,   # None = unknown, True = available, False = unavailable
    'last_check': 0.0,
    'error_shown': False,
    'check_interval': 60,  # Re-check every 60 seconds
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

    now = time.time()
    if (
        not force
        and _ollama_status['available'] is not None
        and now - _ollama_status['last_check'] < _ollama_status['check_interval']
    ):
        return _ollama_status['available']

    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        _ollama_status['available'] = resp.status_code == 200
        _ollama_status['last_check'] = now
        _ollama_status['error_shown'] = False
        return _ollama_status['available']
    except Exception:
        _ollama_status['available'] = False
        _ollama_status['last_check'] = now
        return False


def reset_ollama_status() -> None:
    """Reset the connection status cache so the next call does a fresh check."""
    global _ollama_status
    _ollama_status['available'] = None
    _ollama_status['last_check'] = 0.0
    _ollama_status['error_shown'] = False


def call_ollama(
    prompt: str,
    model: Optional[str] = None,
    num_predict: Optional[int] = None,
    ollama_url: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Optional[str]:
    """
    Send a prompt to Ollama and return the response text.

    Features:
        - Connection status caching (avoids repeated error messages)
        - Retry logic with simple backoff (max 2 retries)
        - Single error message per session

    Args:
        prompt: Text prompt to send.
        model: Ollama model name. Defaults to llm_config.DEFAULT_MODEL.
        num_predict: Maximum tokens to generate. Common values:
                     50   -- time prediction (single number)
                     200  -- short analysis (3-4 sentences)
                     800  -- championship race analysis
                     5000 -- full fairness assessment
        ollama_url: Full API URL including path. Defaults to llm_config.OLLAMA_URL.
        timeout: Request timeout seconds. Defaults to llm_config.TIMEOUT_SECONDS.

    Returns:
        Stripped response string, or None if Ollama is unavailable.
    """
    global _ollama_status

    # Fast-path: if we recently confirmed unavailability, skip
    if _ollama_status['available'] is False:
        now = time.time()
        if now - _ollama_status['last_check'] < _ollama_status['check_interval']:
            return None

    if model is None:
        model = llm_config.DEFAULT_MODEL
    if num_predict is None:
        num_predict = 50
    if ollama_url is None:
        ollama_url = llm_config.OLLAMA_URL
    if timeout is None:
        timeout = llm_config.TIMEOUT_SECONDS

    max_retries = llm_config.MAX_RETRIES

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                ollama_url,
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "num_predict": num_predict,
                    },
                },
                timeout=timeout,
            )

            if response.status_code == 200:
                _ollama_status['available'] = True
                _ollama_status['last_check'] = time.time()
                _ollama_status['error_shown'] = False
                return response.json()['response'].strip()

            # Non-200 response — retry
            if attempt < max_retries:
                time.sleep(attempt + 1)
            else:
                if not _ollama_status['error_shown']:
                    print(f"\n[WARN] Ollama returned status {response.status_code}")
                    _ollama_status['error_shown'] = True
                return None

        except requests.exceptions.ConnectionError:
            _ollama_status['available'] = False
            _ollama_status['last_check'] = time.time()
            if not _ollama_status['error_shown']:
                print("\n" + "=" * 60)
                print("[WARN] OLLAMA NOT AVAILABLE")
                print("=" * 60)
                print("Cannot connect to Ollama. LLM predictions will be skipped.")
                print("To enable AI predictions, run: ollama serve")
                print("System will continue with Baseline and ML predictions only.")
                print("=" * 60 + "\n")
                _ollama_status['error_shown'] = True
            return None

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                if attempt == 0:
                    print("  [Ollama timeout, retrying...]")
                time.sleep(2 * (attempt + 1))
            else:
                if not _ollama_status['error_shown']:
                    print(f"\n[WARN] Ollama timeout after {timeout}s")
                    _ollama_status['error_shown'] = True
                return None

        except Exception as exc:
            if attempt < max_retries:
                time.sleep(attempt + 1)
            else:
                if not _ollama_status['error_shown']:
                    print(f"\n[WARN] Ollama error: {exc}")
                    _ollama_status['error_shown'] = True
                return None

    return None
