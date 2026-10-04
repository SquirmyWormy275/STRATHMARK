"""Runnable, separate preview and private historical development audit."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    PURPOSE,
    SCHEMA,
    forecast,
    load_candidate,
    read_json,
    readonly_workbook,
    sha,
    snapshot,
    validate_calibration,
    write_new_json,
)


def pack(root: Path, bundle: Path, formula: Path, calibration: Path, provenance: Path):
    from strathmark.v3.contracts.canonical import canonical_digest
    from strathmark.v3.runtime_identity import implementation_digest

    value = read_json(calibration)
    validate_calibration(value)
    lineage = read_json(provenance)
    if root.exists():
        raise FileExistsError("candidate directory already exists")
    root.mkdir(parents=True, mode=0o700)
    try:
        components = root / "components"
        components.mkdir(mode=0o700)
        if (
            bundle.is_symlink()
            or any(p.is_symlink() for p in bundle.rglob("*"))
            or formula.is_symlink()
        ):
            raise ValueError("component sources must be regular files without symlinks")
        shutil.copytree(bundle, components / "ml-bundle")
        shutil.copyfile(formula, components / "formula_manifest.json")
        write_new_json(root / "calibration.json", value)
        from strathmark.v3.assessors.formula import FormulaManifest

        body = {
            "schema_version": SCHEMA,
            "purpose": PURPOSE,
            "eligible_for_official_use": False,
            "baseline_bundle_digest": sha(components / "ml-bundle/manifest.json"),
            "formula_digest": FormulaManifest.load(components / "formula_manifest.json").digest,
            "implementation_digest": implementation_digest(),
            "files": {
                p.relative_to(root).as_posix(): sha(p)
                for p in sorted(root.rglob("*"))
                if p.is_file()
            },
            "provenance": lineage,
        }
        write_new_json(root / "manifest.json", {**body, "artifact_digest": canonical_digest(body)})
        load_candidate(root)
        for p in root.rglob("*"):
            p.chmod(0o700 if p.is_dir() else 0o400)
        return {
            "candidate": str(root.resolve()),
            "candidate_digest": canonical_digest(body),
            "purpose": PURPOSE,
            "eligible_for_official_use": False,
        }
    except Exception:
        shutil.rmtree(root)
        raise


def metrics(rows, key):
    errors = sorted(abs(r["actual_seconds"] - r[key]) for r in rows)
    if not errors:
        return {"rows": 0, "mae_seconds": None, "p90_seconds": None}
    return {
        "rows": len(rows),
        "groups": len({r["group"] for r in rows}),
        "mae_seconds": sum(errors) / len(errors),
        "p90_seconds": errors[math.ceil(0.9 * len(errors)) - 1],
        "maximum_error_seconds": errors[-1],
    }


def audit(candidate, workbook, since, until, output):
    from strathmark.v3.factory.workbook_history import load_workbook_history

    readonly_workbook(workbook)
    if output.exists():
        raise FileExistsError("audit output already exists")
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    stop = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if start.tzinfo is None or stop.tzinfo is None or start >= stop:
        raise ValueError("audit requires increasing timezone-aware cutoffs")
    history = load_workbook_history(workbook, cutoff_at_utc=until)
    reverse = {identifier: local for local, identifier in history.competitor_ids}
    selected = [
        o
        for o in history.observations
        if datetime.fromisoformat(o.occurred_at_utc.replace("Z", "+00:00")) >= start
    ]
    if not selected:
        raise ValueError("no dated completions in the selected development cohort")
    rows = []
    for i, o in enumerate(selected, 1):
        result = forecast(
            candidate,
            workbook,
            o.occurred_at_utc,
            [reverse[str(o.competitor_id)]],
            o.context.to_dict(),
        )
        item = result["forecasts"][0]
        rows.append(
            {
                "row_id": str(o.evidence_id),
                "group": str(o.tournament_id),
                "occurred_at_utc": o.occurred_at_utc,
                "actual_seconds": o.result.raw_time_ms / 1000,
                "baseline_seconds": item["baseline_time_ms"] / 1000,
                "candidate_seconds": item["candidate_time_ms"] / 1000,
                "same_event_history_depth": item["same_event_history_depth"],
                "interval90_ms": item["candidate_interval90_ms"],
                "covers_actual": item["candidate_interval90_ms"][0]
                <= o.result.raw_time_ms
                <= item["candidate_interval90_ms"][1],
            }
        )
        if i % 10 == 0 or i == len(selected):
            print(f"Development forecasts: {i}/{len(selected)}", file=sys.stderr, flush=True)
    if sha(workbook) != history.source_sha256:
        raise ValueError("workbook changed during development audit")
    cold = [r for r in rows if not r["same_event_history_depth"]]
    supported = [r for r in rows if r["same_event_history_depth"]]
    result = {
        "schema_version": SCHEMA,
        "purpose": "historical_development_audit",
        "candidate_digest": candidate.manifest["artifact_digest"],
        "history_sha256": history.source_sha256,
        "since_at_utc": since,
        "until_at_utc": until,
        "baseline": metrics(rows, "baseline_seconds"),
        "candidate": metrics(rows, "candidate_seconds"),
        "cold_baseline": metrics(cold, "baseline_seconds"),
        "cold_candidate": metrics(cold, "candidate_seconds"),
        "supported_baseline": metrics(supported, "baseline_seconds"),
        "supported_candidate": metrics(supported, "candidate_seconds"),
        "interval90_coverage": sum(r["covers_actual"] for r in rows) / len(rows),
        "cold_interval90_coverage": sum(r["covers_actual"] for r in cold) / len(cold)
        if cold
        else None,
        "excluded": dict(history.excluded),
        "rows": rows,
        "qualification_assessed": False,
        "eligible_for_official_use": False,
        "issued_mark": False,
    }
    write_new_json(output, result)
    return {k: v for k, v in result.items() if k != "rows"}


def show_report(report: dict):
    print("\nHISTORICAL DEVELOPMENT COMPARISON — raw cutting seconds")
    print(
        f"Results: {report['baseline']['rows']}   Competition groups: {report['baseline']['groups']}"
    )
    for label, before, after in (
        ("Average error", report["baseline"], report["candidate"]),
        ("First-appearance error", report["cold_baseline"], report["cold_candidate"]),
        ("Prior-history error", report["supported_baseline"], report["supported_candidate"]),
    ):
        if before["mae_seconds"] is not None:
            print(f"{label}: {before['mae_seconds']:.2f} -> {after['mae_seconds']:.2f} s")
    print("Examined historical data. Candidate has not met installation requirements.")


def menu(candidate, workbook, report_path):
    readonly_workbook(workbook)
    print("\nSTRATH ACCURACY PREVIEW")
    print("Compare baseline and candidate raw cutting times. Experimental; no issued marks.")
    while True:
        print(
            "\n1  Saved accuracy comparison\n2  Preview cutting times\n3  Competitor IDs\n0  Exit"
        )
        choice = input("Select: ").strip()
        try:
            if choice == "0":
                return
            if choice == "1":
                if report_path is None:
                    print("No saved historical report configured.")
                else:
                    report = read_json(report_path, 10_000_000)
                    if report.get("candidate_digest") != candidate.manifest[
                        "artifact_digest"
                    ] or report.get("history_sha256") != sha(workbook):
                        raise ValueError("saved report belongs to another candidate or snapshot")
                    show_report(report)
            elif choice in {"2", "3"}:
                from strathmark.v3.factory.workbook_history import load_workbook_history

                default = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
                cutoff = input(f"Exclusive cutoff UTC [{default}]: ").strip() or default
                history = load_workbook_history(workbook, cutoff_at_utc=cutoff)
                if choice == "3":
                    print("\n".join(local for local, _ in history.competitor_ids))
                    continue
                ids = [c.strip() for c in input("Competitor IDs (comma separated): ").split(",")]
                event = {"UH": "underhand", "SB": "standing_block"}.get(
                    input("Event (UH/SB): ").strip().upper()
                )
                if event is None:
                    raise ValueError("select UH or SB")
                species = input("Timber species code: ").strip().lower()
                size = int(input("Log diameter mm: ").strip())
                properties = next(
                    (
                        o.context.to_dict()["properties"]
                        for o in reversed(history.observations)
                        if o.context.material_code == species
                    ),
                    [],
                )
                from strathmark.v3.contracts.evidence import ContextProperty, TargetContext

                context = TargetContext(
                    event,
                    size,
                    species,
                    "strathex:v1",
                    "strathex:v1",
                    tuple(ContextProperty.from_dict(p) for p in properties),
                ).to_dict()
                result = forecast(candidate, workbook, cutoff, ids, context)
                print("\nID               Baseline    Candidate    Candidate 90% interval")
                for row in result["forecasts"]:
                    lo, hi = row["candidate_interval90_ms"]
                    print(
                        f"{row['local_competitor_id']:<16} {row['baseline_time_ms'] / 1000:8.2f}s  "
                        f"{row['candidate_time_ms'] / 1000:8.2f}s  {lo / 1000:.2f}–{hi / 1000:.2f}s ({row['route']})"
                    )
            else:
                print("Select 0, 1, 2 or 3.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Preview refused: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Separate frozen-calibration cutting-time preview")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("pack", help="package existing frozen components; never train")
    for name in ("candidate", "bundle", "formula", "calibration", "provenance"):
        prepare.add_argument("--" + name, type=Path, required=True)
    snap = sub.add_parser("snapshot", help="create a separate verified read-only workbook")
    snap.add_argument("--source", type=Path, required=True)
    snap.add_argument("--output", type=Path, required=True)
    for command in ("status", "forecast", "audit", "menu"):
        p = sub.add_parser(command)
        p.add_argument("--candidate", type=Path, required=True)
        if command == "forecast":
            p.add_argument("--request", type=Path, required=True)
            p.add_argument("--output", type=Path)
        if command in {"audit", "menu"}:
            p.add_argument("--workbook", type=Path, required=True)
        if command == "audit":
            p.add_argument("--since", required=True)
            p.add_argument("--until", required=True)
            p.add_argument("--output", type=Path, required=True)
        if command == "menu":
            p.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args.source, args.output)
        elif args.command == "pack":
            result = pack(
                args.candidate, args.bundle, args.formula, args.calibration, args.provenance
            )
        else:
            candidate = load_candidate(args.candidate)
            if args.command == "menu":
                menu(candidate, args.workbook, args.report)
                return 0
            if args.command == "status":
                result = {
                    "schema_version": SCHEMA,
                    "purpose": PURPOSE,
                    "available": True,
                    "candidate_digest": candidate.manifest["artifact_digest"],
                    "issued_mark": False,
                    "eligible_for_official_use": False,
                }
            elif args.command == "audit":
                result = audit(candidate, args.workbook, args.since, args.until, args.output)
            else:
                request = read_json(args.request)
                if set(request) != {
                    "workbook",
                    "cutoff_at_utc",
                    "competitor_ids",
                    "target_context",
                }:
                    raise ValueError("preview request accepts cutting-time fields only")
                result = forecast(
                    candidate,
                    Path(request["workbook"]),
                    request["cutoff_at_utc"],
                    request["competitor_ids"],
                    request["target_context"],
                )
                if args.output:
                    write_new_json(args.output, result)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (EOFError, KeyboardInterrupt):
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Preview refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
