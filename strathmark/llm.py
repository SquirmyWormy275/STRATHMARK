"""
LLM Integration (Ollama + Gemini)
=================================

Handles communication with a local Ollama instance (primary) and Google
Gemini (cloud fallback) for AI-enhanced predictions and fairness assessments.

Connection status is cached to avoid repeated error messages within a session.
All functions return None gracefully if both LLM tiers are unavailable so the
prediction cascade (Manual > LLM > ML > Baseline > Panel Fallback) can fall
through cleanly.

Race-day discipline:
    - Single attempt per tier, no retries (retries add latency on race day).
    - Explicit (connect, read) timeouts so a dead Ollama costs <5s.
    - OLLAMA_HOST="" or "disabled" skips the Ollama tier entirely.
    - GEMINI_API_KEY unset skips the Gemini tier entirely.

Source references (STRATHEX):
    woodchopping/predictions/llm.py -> call_ollama()
    woodchopping/predictions/llm.py -> check_ollama_connection()
    woodchopping/predictions/llm.py -> reset_ollama_status()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from strathmark.config import (
    get_gemini_api_key,
    get_ollama_url,
    is_ollama_disabled,
    llm_config,
)

logger = logging.getLogger(__name__)

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

    Honours the OLLAMA_HOST="" / "disabled" kill switch — when the LLM tier
    is disabled this returns False without attempting any HTTP call.

    Args:
        base_url: Ollama server base URL (without path).
        force: Ignore cache and perform a fresh check.

    Returns:
        True if Ollama is reachable, False otherwise.
    """
    global _ollama_status

    # Honor the race-day kill switch — never even open a socket if disabled.
    if is_ollama_disabled():
        with _ollama_lock:
            _ollama_status["available"] = False
            _ollama_status["last_check"] = time.time()
        return False

    with _ollama_lock:
        now = time.time()
        if (
            not force
            and _ollama_status["available"] is not None
            and now - _ollama_status["last_check"] < _ollama_status["check_interval"]
        ):
            return _ollama_status["available"]

        try:
            # Explicit (connect, read) tuple — fail-fast on dead hosts.
            resp = requests.get(
                f"{base_url}/api/tags",
                timeout=(llm_config.OLLAMA_CONNECT_TIMEOUT, llm_config.OLLAMA_READ_TIMEOUT),
            )
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
    Send a prompt to the LLM tier and return the response text.

    The LLM tier is a two-step cascade:
        1. Local Ollama instance (single attempt, fail-fast on (3s, 15s) timeout)
        2. Google Gemini cloud fallback — only if Ollama returned None AND
           GEMINI_API_KEY is set in the environment

    Both steps return None on any failure so the parent prediction cascade
    (Manual > LLM > ML > Baseline > Panel Fallback) can fall through cleanly.
    There are no retries — on race day the cost of waiting is far higher than
    the cost of dropping to ML/Baseline.

    Features:
        - Connection status caching (avoids repeated error messages)
        - Single attempt per tier — no retries
        - Explicit (connect, read) timeout tuple
        - JSON schema enforcement via format parameter (Ollama v0.5+)
        - Honors OLLAMA_HOST="" / "disabled" kill switch
        - Lazy-imports google.generativeai so missing package is a no-op

    Args:
        prompt: Text prompt to send.
        model: Ollama model name. Defaults to llm_config.DEFAULT_MODEL.
        num_predict: Maximum tokens to generate. Common values:
                     150  -- quality adjustment (JSON)
                     500  -- competitor profile (JSON)
                     2000 -- full fairness assessment (JSON)
        ollama_url: Full API URL including path. Defaults to get_ollama_url()
                    which honors OLLAMA_HOST and STRATHMARK_OLLAMA_URL env vars.
        timeout: Legacy single-int timeout — IGNORED in favor of the explicit
                 (CONNECT, READ) tuple from llm_config.  Kept in the signature
                 so existing callers (predictor.py, fairness.py) don't break.
        format_schema: Optional JSON schema dict for structured output enforcement.
                       When provided, Ollama constrains generation to valid JSON
                       matching this schema via GBNF grammar logit masking.
                       Temperature is forced to 0.0 for schema compliance.

    Returns:
        Stripped response string from whichever tier responded, or None if
        both Ollama and Gemini are unavailable.
    """
    global _ollama_status

    if model is None:
        model = llm_config.DEFAULT_MODEL
    if num_predict is None:
        num_predict = 150

    # When format_schema is set, force temperature to 0 for deterministic output
    temperature = 0.0 if format_schema is not None else 0.3

    # ----- Tier 1: Ollama -------------------------------------------------
    # Resolve URL at call time so OLLAMA_HOST monkeypatching works in tests
    # and Railway env-var swaps take effect without a redeploy.
    if ollama_url is None:
        ollama_url = get_ollama_url()

    # Empty URL means OLLAMA_HOST is "" or "disabled" — skip the tier.
    if not ollama_url:
        return _call_gemini(
            prompt=prompt,
            num_predict=num_predict,
            temperature=temperature,
            format_schema=format_schema,
        )

    # Fast-path: if we recently confirmed unavailability, skip straight to Gemini.
    if _ollama_status["available"] is False:
        now = time.time()
        if now - _ollama_status["last_check"] < _ollama_status["check_interval"]:
            return _call_gemini(
                prompt=prompt,
                num_predict=num_predict,
                temperature=temperature,
                format_schema=format_schema,
            )

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

    # Single attempt, explicit (connect, read) tuple, broad exception net.
    try:
        response = requests.post(
            ollama_url,
            json=payload,
            timeout=(llm_config.OLLAMA_CONNECT_TIMEOUT, llm_config.OLLAMA_READ_TIMEOUT),
        )
        if response.status_code == 200:
            _ollama_status["available"] = True
            _ollama_status["last_check"] = time.time()
            _ollama_status["error_shown"] = False
            return response.json()["response"].strip()

        # Non-200 — log once, fall through to Gemini.
        if not _ollama_status["error_shown"]:
            logger.warning("Ollama returned HTTP %s; falling through", response.status_code)
            _ollama_status["error_shown"] = True

    except (
        ConnectionRefusedError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.RequestException,
    ) as exc:
        _ollama_status["available"] = False
        _ollama_status["last_check"] = time.time()
        if not _ollama_status["error_shown"]:
            logger.warning("Ollama unreachable (%s: %s); falling through", type(exc).__name__, exc)
            _ollama_status["error_shown"] = True

    except Exception as exc:
        # Defensive: any other error (e.g. malformed JSON in response.json())
        # must not propagate — race day, the cascade is the safety net.
        if not _ollama_status["error_shown"]:
            logger.warning("Ollama unexpected error (%s: %s); falling through", type(exc).__name__, exc)
            _ollama_status["error_shown"] = True

    # ----- Tier 2: Gemini -------------------------------------------------
    return _call_gemini(
        prompt=prompt,
        num_predict=num_predict,
        temperature=temperature,
        format_schema=format_schema,
    )


# ---------------------------------------------------------------------------
# Tier 2: Gemini cloud fallback
# ---------------------------------------------------------------------------

# Cache the warning state so we don't log "no API key" on every cascade call.
_gemini_warned_no_key: bool = False
_gemini_warned_no_pkg: bool = False


def _call_gemini(
    prompt: str,
    num_predict: int,
    temperature: float,
    format_schema: Optional[dict],
) -> Optional[str]:
    """
    Cloud LLM fallback via Google Gemini 2.0 Flash-Lite.

    Triggered when Ollama returns None (unreachable, timeout, non-200, or
    OLLAMA_HOST disabled) AND GEMINI_API_KEY is set in the environment.

    Discipline:
        - Lazy-import google.generativeai so missing package is a no-op
        - Single attempt, no retries
        - 5s connect / 15s read timeouts (slightly longer than Ollama because
          this is a real internet round-trip)
        - Same JSON schema enforcement as Ollama when format_schema is provided
        - Returns None on any failure so the cascade falls through to ML

    Args:
        prompt: Text prompt to send.
        num_predict: Max output tokens (passed as max_output_tokens).
        temperature: Sampling temperature (forced to 0.0 by caller when schema set).
        format_schema: Optional JSON schema dict.  Maps to response_schema +
                       response_mime_type="application/json" for structured output.

    Returns:
        Stripped response string, or None if Gemini is unavailable / unset.
    """
    global _gemini_warned_no_key, _gemini_warned_no_pkg

    api_key = get_gemini_api_key()
    if not api_key:
        if not _gemini_warned_no_key:
            logger.info("GEMINI_API_KEY not set; skipping cloud LLM tier")
            _gemini_warned_no_key = True
        return None

    # Lazy import — google-generativeai is an optional dependency.
    try:
        import google.generativeai as genai  # type: ignore
    except ImportError:
        if not _gemini_warned_no_pkg:
            logger.warning("google-generativeai package not installed; skipping cloud LLM tier")
            _gemini_warned_no_pkg = True
        return None

    try:
        genai.configure(api_key=api_key)

        generation_config: dict = {
            "temperature": temperature,
            "max_output_tokens": num_predict,
        }

        # Structured output: same JSON schema discipline as Ollama.
        if format_schema is not None:
            generation_config["response_mime_type"] = "application/json"
            generation_config["response_schema"] = format_schema

        model_obj = genai.GenerativeModel(
            model_name=llm_config.GEMINI_MODEL,
            generation_config=generation_config,
        )

        # request_options.timeout is a single read timeout in the google client.
        # Connect timeout is handled internally by grpc with a separate dial budget;
        # we use READ_TIMEOUT here as the total round-trip ceiling.
        response = model_obj.generate_content(
            prompt,
            request_options={"timeout": llm_config.GEMINI_READ_TIMEOUT},
        )

        text = getattr(response, "text", None)
        if text is None:
            logger.warning("Gemini returned no text; falling through")
            return None
        return text.strip()

    except Exception as exc:
        # Broad except is intentional — google client raises a wide variety
        # of exceptions (DeadlineExceeded, PermissionDenied, ResourceExhausted,
        # InvalidArgument, GoogleAPICallError…).  Any of them must fall through
        # cleanly to ML on race day.
        logger.warning("Gemini call failed (%s: %s); falling through", type(exc).__name__, exc)
        return None
