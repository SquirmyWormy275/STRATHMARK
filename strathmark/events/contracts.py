"""Small, closed managed-event contract. No framework or environment access."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

CONTRACT = "strathmark.managed-event.v1"
SOURCE_ID_VERSION = "canonical-tuple.v1"
POLICIES = ("v2-optimizer.v1", "mark3-ceil180.v1")


class EventError(ValueError):
    """An explicit validation, authority, concurrency or recovery refusal."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def closed(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise EventError("invalid_fields: expected " + ", ".join(sorted(required)))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,223}", value):
        raise EventError("invalid_identity")
    return value


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise EventError(f"integer_required: {lower}..{upper}")
    return value


def number(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise EventError("invalid_seconds")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EventError("invalid_seconds") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0) or result > 86400:
        raise EventError("invalid_seconds")
    if result.as_tuple().exponent < -6:
        raise EventError("seconds_precision_exceeds_microseconds")
    return result


def decimal_text(value):
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class Principal:
    """Supplied by a trusted adapter after authentication, never a request body."""

    actor_id: str
    shows: frozenset[str]
    role: str

    def authorize(self, show_id, write=False):
        if show_id not in self.shows or self.role not in {"director", "judge", "viewer"}:
            raise EventError("forbidden_show")
        if write and self.role not in {"director", "judge"}:
            raise EventError("forbidden_role")
        identifier(self.actor_id)


def validate_config(config):
    closed(
        config,
        {
            "schema_version",
            "show_id",
            "tournament_id",
            "event_id",
            "source_show_id",
            "synthetic",
            "engine",
            "selection_reason",
            "mark_policy",
            "rule_version",
            "discipline",
            "format",
            "wood",
            "quality_policy",
            "prediction_as_of",
            "event_date",
            "roster",
            "round_rules",
            "stands",
            "timer_policy",
            "tie_policy",
            "publisher",
        },
    )
    if config["schema_version"] != CONTRACT:
        raise EventError("unsupported_contract")
    for name in ("show_id", "tournament_id", "event_id", "source_show_id", "rule_version"):
        identifier(config[name])
    if config["synthetic"] is not True:
        raise EventError("official_use_qualification_required: managed v1 is synthetic-only")
    if config["engine"] != "v2":
        raise EventError("unsupported_engine: existing V3 profiles retain their separate contracts")
    if (
        not isinstance(config["selection_reason"], str)
        or not 1 <= len(config["selection_reason"].strip()) <= 240
    ):
        raise EventError("selection_reason_required")
    if config["mark_policy"] not in POLICIES:
        raise EventError("unsupported_mark_policy")
    if config["discipline"] not in {"UH", "SB"} or config["format"] != "handicap":
        raise EventError("unsupported_event_family")
    if config["quality_policy"] != "record_only_v2":
        raise EventError("unsupported_quality_policy: V2 quality has no numeric effect")
    if config["publisher"] not in {"host", "standalone"}:
        raise EventError("publisher_required")
    if config["timer_policy"] not in {"dual_mean", "single"} or config["tie_policy"] not in {
        "include",
        "judge",
    }:
        raise EventError("unsupported_result_policy")
    closed(config["wood"], {"species", "diameter_mm", "quality"})
    identifier(config["wood"]["species"])
    diameter = number(config["wood"]["diameter_mm"])
    if not 225 <= diameter <= 500:
        raise EventError("unsupported_diameter")
    quality = config["wood"]["quality"]
    if quality is not None:
        integer(quality, 0, 10)
    try:
        cutoff = date.fromisoformat(config["prediction_as_of"])
        event_date = date.fromisoformat(config["event_date"])
    except (TypeError, ValueError) as exc:
        raise EventError("invalid_cutoff") from exc
    if cutoff > event_date:
        raise EventError("cutoff_after_event")
    stands = config["stands"]
    if not isinstance(stands, list) or not 1 <= len(stands) <= 64:
        raise EventError("invalid_stands")
    for stand in stands:
        identifier(stand)
    if len(set(stands)) != len(stands):
        raise EventError("duplicate_stand")
    roster = config["roster"]
    if (
        not isinstance(roster, list)
        or not 1 <= len(roster) <= 512
        or not all(isinstance(row, dict) for row in roster)
    ):
        raise EventError("invalid_roster")
    for row in roster:
        closed(
            row,
            {
                "entry_id",
                "engine_id",
                "host_ref",
                "display_name",
                "gender",
                "history",
                "gear_group",
            },
            {"show_person_ref"},
        )
        for key in ("entry_id", "engine_id", "host_ref"):
            identifier(row[key])
        if row.get("show_person_ref") is not None:
            identifier(row["show_person_ref"])
            if row["show_person_ref"] == row["engine_id"]:
                raise EventError("identity_scopes_must_differ")
        if not isinstance(row["display_name"], str) or not 1 <= len(row["display_name"]) <= 120:
            raise EventError("invalid_display_name")
        if row["gender"] not in {"M", "F", None}:
            raise EventError("invalid_gender")
        if row["gear_group"] is not None:
            identifier(row["gear_group"])
        if not isinstance(row["history"], list) or len(row["history"]) > 2000:
            raise EventError("history_capacity")
        for history in row["history"]:
            closed(
                history,
                {
                    "event_code",
                    "time_seconds",
                    "species",
                    "diameter_mm",
                    "result_date",
                    "source_ref",
                },
            )
            if history["event_code"] not in {"SB", "UH"}:
                raise EventError("unsupported_history_discipline")
            identifier(history["source_ref"])
            identifier(history["species"])
            number(history["time_seconds"])
            number(history["diameter_mm"])
            try:
                when = date.fromisoformat(history["result_date"])
            except (TypeError, ValueError) as exc:
                raise EventError("dated_history_required") from exc
            if when >= cutoff:
                raise EventError("history_cutoff_violation")
        if len({h["source_ref"] for h in row["history"]}) != len(row["history"]):
            raise EventError("duplicate_history_source_identity")
    for key in ("entry_id", "engine_id", "host_ref"):
        if len({row[key] for row in roster}) != len(roster):
            raise EventError("duplicate_identity")
    rules = config["round_rules"]
    if (
        not isinstance(rules, list)
        or not 1 <= len(rules) <= 8
        or not all(isinstance(rule, dict) for rule in rules)
    ):
        raise EventError("invalid_round_rules")
    for index, rule in enumerate(rules):
        closed(rule, {"round_id", "capacity", "qualifiers_per_heat"})
        identifier(rule["round_id"])
        integer(rule["capacity"], 1, min(len(stands), 64))
        integer(rule["qualifiers_per_heat"], 0, rule["capacity"])
        if (index == len(rules) - 1) != (rule["qualifiers_per_heat"] == 0):
            raise EventError("final_round_must_have_zero_qualifiers")
    if len({rule["round_id"] for rule in rules}) != len(rules):
        raise EventError("duplicate_round_identity")
    return config


def capabilities():
    from strathmark import __version__

    return {
        "schema_version": CONTRACT,
        "package_version": __version__,
        "source_identity_version": SOURCE_ID_VERSION,
        "runtime": {
            "transport": "local_library_or_trusted_process",
            "tested_python": ["3.10", "3.11", "3.12", "3.13"],
            "hosted_production_qualified": False,
        },
        "operations": [
            "create",
            "read",
            "forecast",
            "fields",
            "calculate",
            "review",
            "issue",
            "outcomes",
            "accept",
            "advance",
            "finalize",
            "correct",
            "retain_downstream",
            "configure",
            "handover",
            "export",
            "delivery",
            "fence",
        ],
        "local_recovery_commands": ["backup", "transfer"],
        "calculation_only": True,
        "engines": ["v2"],
        "disciplines": ["SB", "UH"],
        "formats": ["handicap"],
        "mark_policies": list(POLICIES),
        "official_use": False,
        "limitation": "synthetic-only; installation qualification pending",
        "quality": "recorded, numerically ignored",
        "same_day_learning": False,
    }
