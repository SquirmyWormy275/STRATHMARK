"""Calculation-only boundary: no event, database or issue authority."""

from .contracts import CONTRACT, EventError, closed, validate_config
from .numerics import calculate


def calculate_request(request):
    closed(
        request,
        {
            "schema_version",
            "engine",
            "mark_policy",
            "discipline",
            "wood",
            "quality_policy",
            "prediction_as_of",
            "competitors",
        },
    )
    if request["schema_version"] != "strathmark.calculation.v1":
        raise EventError("unsupported_calculation_contract")
    config = {
        **{k: v for k, v in request.items() if k not in {"schema_version", "competitors"}},
        "schema_version": CONTRACT,
        "event_date": request["prediction_as_of"],
        "selection_reason": "Calculation-only request; no operational selection created",
        "show_id": "calculation",
        "tournament_id": "calculation",
        "event_id": "calculation",
        "source_show_id": "calculation",
        "synthetic": True,
        "format": "handicap",
        "rule_version": "calculation:v1",
        "roster": request["competitors"],
        "stands": ["1"],
        "timer_policy": "single",
        "tie_policy": "include",
        "publisher": "standalone",
        "round_rules": [{"round_id": "calculation", "capacity": 1, "qualifiers_per_heat": 0}],
    }
    validate_config(config)
    if len(config["roster"]) > 64:
        raise EventError("calculation_field_capacity")
    return {
        "schema_version": request["schema_version"],
        "purpose": "calculation_only",
        "issued_mark": False,
        "engine": request["engine"],
        "mark_policy": request["mark_policy"],
        "rows": calculate(config, [r["entry_id"] for r in config["roster"]]),
    }
