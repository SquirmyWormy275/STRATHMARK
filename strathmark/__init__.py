"""
strathmark — Woodchopping Handicap Engine
==========================================

A pip-installable Python package that exposes the STRATHEX handicap calculation
engine for use in external applications (tournament software, scoring apps, etc.).

Public API
----------
    from strathmark import HandicapCalculator
    from strathmark import CompetitorRecord, WoodProfile

Design Rules (invariants enforced in all submodules)
-----------------------------------------------------
    - Mark floor:    3 seconds (never lower, under any circumstances)
    - Mark ceiling:  system-wide 183 seconds (180s time limit + 3s minimum mark)
                     event configs may enforce a lower ceiling
    - Variance:      absolute ±3 seconds ONLY — proportional variance is forbidden
    - Prediction cascade: Manual > LLM > ML > Panel mark fallback
    - Time-decay:    exponential decay, 2-year half-life (730 days)
    - Output:        plain text only — no emojis, no ANSI color codes
    - Style:         lean and simple, no unnecessary complexity
"""

from strathmark.calculator import HandicapCalculator
from strathmark.predictor import CompetitorRecord, WoodProfile

__all__ = [
    "HandicapCalculator",
    "CompetitorRecord",
    "WoodProfile",
]

__version__ = "0.1.0"
