"""Plainly labeled synthetic inputs, usable outside this source checkout."""

from .contracts import CONTRACT


def synthetic_event(show="synthetic:missoula"):
    second = show != "synthetic:missoula"
    return {
        "schema_version": CONTRACT,
        "show_id": show,
        "tournament_id": show + ":2027",
        "event_id": show + ":underhand",
        "source_show_id": show,
        "synthetic": True,
        "engine": "v2",
        "selection_reason": "Synthetic event using the explicitly selected packaged V2 engine",
        "mark_policy": "mark3-ceil180.v1",
        "rule_version": "synthetic-rules:v1",
        "discipline": "UH",
        "format": "handicap",
        "wood": {"species": "S01", "diameter_mm": 300 if second else 275, "quality": None},
        "quality_policy": "record_only_v2",
        "prediction_as_of": "2026-10-15",
        "event_date": "2026-10-15",
        "stands": ["1", "2", "3", "4"],
        "timer_policy": "dual_mean",
        "tie_policy": "judge",
        "publisher": "standalone" if second else "host",
        "round_rules": [
            {
                "round_id": "qualifying",
                "capacity": 2 if second else 4,
                "qualifiers_per_heat": 1 if second else 2,
            },
            {"round_id": "semifinal", "capacity": 2, "qualifiers_per_heat": 1},
            {"round_id": "final", "capacity": 2, "qualifiers_per_heat": 0},
        ],
        "roster": [
            {
                "entry_id": f"e{i}",
                "engine_id": f"{show}:engine:{i}",
                "host_ref": f"{show}:entry:{i}",
                "display_name": "Synthetic Cutter" if i < 2 else f"Synthetic {i}",
                "gender": "M" if i % 2 else "F",
                "gear_group": None,
                "history": [
                    {
                        "event_code": "UH",
                        "time_seconds": str(30 + i * 2 + j),
                        "species": "S01",
                        "diameter_mm": 275,
                        "result_date": f"2025-03-{j + 1:02}",
                        "source_ref": f"{show}:history:{i}:{j}",
                    }
                    for j in range(3)
                ],
            }
            for i in range(8)
        ],
    }
