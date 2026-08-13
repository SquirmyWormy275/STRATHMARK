"""Shared Prediction Engine V2 provenance admission rules."""

from __future__ import annotations

from strathmark.prediction_v2 import ENGINE_VERSION

__all__ = ["ELIGIBLE_TRAINING_SOURCES", "ENGINE_VERSION", "is_v2_training_source"]

ELIGIBLE_TRAINING_SOURCES = frozenset(
    {
        "baseline",
        "ml",
        "hierarchical_dynamic_core",
        "conditional_population_prior",
        "hierarchical_dynamic_core+catboost_residual",
        "conditional_population_prior+catboost_residual",
    }
)


def is_v2_training_source(source: object) -> bool:
    """Return whether a persisted source is an exact approved V2 model source."""

    return str(source).strip() in ELIGIBLE_TRAINING_SOURCES
