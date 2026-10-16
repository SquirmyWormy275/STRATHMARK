"""Installed CLI portability verification on generated synthetic evidence only."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from .contracts import digest
from .examples import synthetic_event


def verify(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"mode": "synthetic_verification", "official_use": False, "shows": []}
    for show in ("synthetic:missoula", "synthetic:second-show"):
        directory = output / show.replace(":", "-")
        directory.mkdir()
        config = synthetic_event(show)
        database = directory / "authority.sqlite3"
        counter = 0

        def run(action, payload=None, state=None, *, command="event"):
            nonlocal counter
            counter += 1
            request = [sys.executable, "-I", "-m", "strathmark", command]
            if command == "event":
                request += [
                    action,
                    "--database",
                    str(database),
                    "--event-id",
                    config["event_id"],
                    "--show",
                    show,
                    "--request-id",
                    str(uuid4()),
                ]
                if state:
                    request += ["--expected-revision", str(state["revision"])]
            if payload is not None:
                path = directory / f"input-{counter}.json"
                path.write_text(json.dumps(payload))
                request += ["--input", str(path)]
            result = subprocess.run(
                [*request, "--json"],
                cwd=directory,
                text=True,
                capture_output=True,
                timeout=60,
                check=True,
            )
            return json.loads(result.stdout)

        calculation = {
            key: config[key]
            for key in (
                "engine",
                "mark_policy",
                "discipline",
                "wood",
                "quality_policy",
                "prediction_as_of",
            )
        }
        calculation.update(schema_version="strathmark.calculation.v1", competitors=config["roster"])
        predicted = run("", calculation, command="calculate")
        assert predicted["purpose"] == "calculation_only" and predicted["issued_mark"] is False
        assert not database.exists()
        state = run("create", config)
        state = run("forecast", {}, state)
        assert all("start_mark_seconds" not in row for row in state["forecasts"][-1]["rows"])
        eligible = [row["entry_id"] for row in config["roster"]]
        for ordinal, rule in enumerate(config["round_rules"]):
            capacity = rule["capacity"]
            fields = [
                {
                    "heat_id": f"r{ordinal}h{n // capacity}",
                    "entries": eligible[n : n + capacity],
                    "stands": config["stands"][: len(eligible[n : n + capacity])],
                }
                for n in range(0, len(eligible), capacity)
            ]
            state = run("fields", {"fields": fields}, state)
            for field in fields:
                state = run("calculate", {"heat_id": field["heat_id"]}, state)
                heat = next(
                    h for h in state["rounds"][-1]["fields"] if h["heat_id"] == field["heat_id"]
                )
                sheet_digest = heat["sheet"]["digest"]
                assert all(row["model_version"] for row in heat["sheet"]["rows"])
                state = run(
                    "review", {"heat_id": field["heat_id"], "sheet_digest": sheet_digest}, state
                )
                state = run(
                    "issue",
                    {"heat_id": field["heat_id"], "sheet_digest": sheet_digest, "confirm": True},
                    state,
                )
                heat = next(
                    h for h in state["rounds"][-1]["fields"] if h["heat_id"] == field["heat_id"]
                )
                outcomes = [
                    {
                        "entry_id": row["entry_id"],
                        "status": "finished",
                        "timer1_seconds": str(100 + i - row["start_mark_seconds"]),
                        "timer2_seconds": str(100 + i - row["start_mark_seconds"]),
                    }
                    for i, row in enumerate(heat["sheet"]["rows"])
                ]
                state = run(
                    "outcomes",
                    {
                        "heat_id": field["heat_id"],
                        "issue_id": heat["issue_id"],
                        "outcomes": outcomes,
                    },
                    state,
                )
                heat = next(
                    h for h in state["rounds"][-1]["fields"] if h["heat_id"] == field["heat_id"]
                )
                state = run(
                    "accept",
                    {
                        "heat_id": field["heat_id"],
                        "result_digest": heat["result_revisions"][-1]["digest"],
                    },
                    state,
                )
            if ordinal < 2:
                state = run("advance", {}, state)
                eligible = state["rounds"][-1]["advancing"]
        state = run("finalize", {}, state)
        exported = run("export")
        assert len(exported["summary"]) == 8 and len(exported["deliveries"]) == 14
        report["shows"].append(
            {
                "show_id": show,
                "event_revision": state["revision"],
                "attempts": len(exported["deliveries"]),
                "summary_entries": len(exported["summary"]),
                "numeric_identity": state["numeric_identity"],
                "export_digest": digest(exported),
                "cli_processes": counter,
            }
        )
    (output / "report.json").write_text(json.dumps(report, indent=2))
    (output / "report.txt").write_text(
        "Synthetic installed CLI verification; no real-world accuracy or official-use claim.\n"
        + "\n".join(
            f"{r['show_id']}: {r['attempts']} attempts, {r['summary_entries']} summary entries, real V2"
            for r in report["shows"]
        )
        + "\n"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.output), indent=2))


if __name__ == "__main__":
    main()
