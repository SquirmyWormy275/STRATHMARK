"""
Expanded LLM Roles
==================

Narrative-only LLM features outside the numeric prediction core. All functions use
JSON schema enforcement and gracefully return None if Ollama is unavailable. They
cannot change a V2 predicted time or mark.

Roles:
    generate_competitor_profile()  -- spectator-facing competitor narrative
    generate_race_commentary()     -- post-heat sports commentary
    detect_result_anomaly()        -- flag unusual performance patterns
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from strathmark.llm import call_ollama

# ---------------------------------------------------------------------------
# JSON schemas for each role
# ---------------------------------------------------------------------------

COMPETITOR_PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "recent_form": {"type": "string"},
        "prediction_confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "watch_factors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narrative", "strengths", "recent_form", "prediction_confidence"],
}

RACE_COMMENTARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "commentary": {"type": "string"},
        "standout_performer": {"type": "string"},
        "upset": {"type": "boolean"},
    },
    "required": ["headline", "commentary", "standout_performer", "upset"],
}

ANOMALY_DETECTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_anomalous": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["normal", "notable", "significant", "extreme"]},
        "explanation": {"type": "string"},
        "recommended_action": {"type": "string"},
    },
    "required": ["is_anomalous", "severity", "explanation"],
}


# ---------------------------------------------------------------------------
# Competitor profile generation
# ---------------------------------------------------------------------------


def generate_competitor_profile(
    name: str,
    event_code: str,
    history_summary: str,
    predicted_time: float,
    confidence: str,
) -> Optional[Dict]:
    """
    Generate a spectator-facing competitor profile narrative.

    Args:
        name: Competitor display name.
        event_code: 'SB' or 'UH'.
        history_summary: Plain-text summary of recent performance.
        predicted_time: Predicted time for this event (seconds).
        confidence: Prediction confidence level.

    Returns:
        Dict with 'narrative', 'strengths', 'recent_form', etc. or None.
    """
    event_name = "Standing Block" if event_code == "SB" else "Underhand"
    prompt = f"""Generate a brief spectator-facing profile for a woodchopping competitor.

Competitor: {name}
Event: {event_name}
Predicted time: {predicted_time:.1f}s
Prediction confidence: {confidence}
Recent history: {history_summary}

Write a 2-3 sentence narrative suitable for a PA announcer or event program.
Include strengths, recent form assessment, and any watch factors."""

    response = call_ollama(
        prompt,
        num_predict=500,
        format_schema=COMPETITOR_PROFILE_SCHEMA,
    )

    if response is None:
        return None

    try:
        return json.loads(response)
    except (json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Post-heat race commentary
# ---------------------------------------------------------------------------


def generate_race_commentary(
    event_code: str,
    competitors: List[Dict],
    results: List[Dict],
) -> Optional[Dict]:
    """
    Generate sports-commentary style analysis after a heat.

    Args:
        event_code: 'SB' or 'UH'.
        competitors: List of dicts with 'name', 'mark', 'predicted_time'.
        results: List of dicts with 'name', 'actual_time', 'finish_position'.

    Returns:
        Dict with 'headline', 'commentary', 'standout_performer', 'upset'.
    """
    event_name = "Standing Block" if event_code == "SB" else "Underhand"

    comp_text = "\n".join(
        f"  {c['name']}: Mark {c['mark']}, predicted {c['predicted_time']:.1f}s"
        for c in competitors
    )
    result_text = "\n".join(
        f"  {r['finish_position']}. {r['name']}: {r['actual_time']:.1f}s"
        for r in sorted(results, key=lambda x: x["finish_position"])
    )

    prompt = f"""Generate exciting sports commentary for a woodchopping heat result.

Event: {event_name}

Pre-race predictions:
{comp_text}

Results:
{result_text}

Write a compelling 2-3 sentence commentary. Note any upsets (back-marker winning)
or tight finishes. Name the standout performer."""

    response = call_ollama(
        prompt,
        num_predict=300,
        format_schema=RACE_COMMENTARY_SCHEMA,
    )

    if response is None:
        return None

    try:
        return json.loads(response)
    except (json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def detect_result_anomaly(
    name: str,
    event_code: str,
    actual_time: float,
    predicted_time: float,
    historical_avg: float,
    historical_std: float,
    wood_species: str,
    diameter_mm: float,
) -> Optional[Dict]:
    """
    Detect if a competition result is anomalous.

    Args:
        name: Competitor name.
        event_code: 'SB' or 'UH'.
        actual_time: Recorded time (seconds).
        predicted_time: System's predicted time (seconds).
        historical_avg: Competitor's historical average (seconds).
        historical_std: Competitor's historical std dev (seconds).
        wood_species: Species used in this event.
        diameter_mm: Block diameter (mm).

    Returns:
        Dict with 'is_anomalous', 'severity', 'explanation', etc.
    """
    deviation = actual_time - predicted_time
    z_score = abs(deviation) / max(historical_std, 1.0)

    prompt = f"""Analyze whether this woodchopping result is anomalous.

Competitor: {name}
Event: {event_code}
Wood: {wood_species}, {diameter_mm:.0f}mm

Actual time: {actual_time:.1f}s
Predicted time: {predicted_time:.1f}s
Deviation: {deviation:+.1f}s
Historical average: {historical_avg:.1f}s (std: {historical_std:.1f}s)
Z-score: {z_score:.1f}

Is this result anomalous? Consider: equipment issues, wood defects, injury,
exceptional form, or handicapper prediction error. Rate severity and explain."""

    response = call_ollama(
        prompt,
        num_predict=300,
        format_schema=ANOMALY_DETECTION_SCHEMA,
    )

    if response is None:
        # Statistical fallback when LLM unavailable
        return {
            "is_anomalous": z_score > 2.5,
            "severity": "significant"
            if z_score > 3.0
            else ("notable" if z_score > 2.0 else "normal"),
            "explanation": f"Statistical deviation: {z_score:.1f} standard deviations from expected.",
            "recommended_action": "Review prediction inputs"
            if z_score > 2.5
            else "No action needed",
        }

    try:
        return json.loads(response)
    except (json.JSONDecodeError, KeyError):
        return None
