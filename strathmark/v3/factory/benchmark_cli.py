"""Read-only date-causal replay through installed numeric prediction implementations."""

import argparse
import json
import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median

import pandas as pd

from strathmark.features import build_prior_evidence
from strathmark.prediction_v2 import PredictionV2Model, PredictionV2Request
from strathmark.v3.contracts.forecasts import PositiveTimeDistribution
from strathmark.v3.domain.credibility import _quantile_crps
from strathmark.v3.domain.pooling import LinearPooledDistribution
from strathmark.v3.factory.candidate_cli import _packets
from strathmark.v3.factory.ml_artifacts import load_ml_bundle
from strathmark.v3.factory.ml_training import _build_causal_matrix_values, mean_pinball_loss
from strathmark.v3.factory.workbook_history import load_workbook_history
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.linux_forecasts import calculate, load_formula_manifest


def main():
    import catboost

    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument("--cutoff-at-utc", required=True)
    p.add_argument("--audit-after-year", type=int, default=2024)
    p.add_argument("--v2-training-cutoff", type=date.fromisoformat, default=date(2023, 1, 1))
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("benchmark output exists; refusing to overwrite evidence")
    wb = args.workbook.resolve(strict=True)
    h = load_workbook_history(wb, cutoff_at_utc=args.cutoff_at_utc)
    bundle = load_ml_bundle(
        args.bundle, installed_catboost_version=catboost.__version__, installed_python_abi="cp313"
    )
    by = defaultdict(list)
    for o in h.observations:
        by[str(o.competitor_id)].append(o)
    rows = [
        r
        for r in _build_causal_matrix_values(_packets(by, "a" * 64))
        if int(r.occurred_at_utc[:4]) > args.audit_after_year
    ]
    if not rows:
        raise ValueError("no eligible held-out rows in the requested years")
    if args.v2_training_cutoff > min(date.fromisoformat(r.occurred_at_utc[:10]) for r in rows):
        raise ValueError("V2 fitting cutoff must not include benchmark targets")
    formula_digest = load_formula_manifest(args.bundle).digest
    observations = {str(o.evidence_id): o for o in h.observations}
    local = {v: k for k, v in h.competitor_ids}
    signer = P256EphemeralSigner.generate("integrity-key:private-benchmark")
    trust = IntegrityTrustStore((signer.identity,))
    groups = defaultdict(list)
    for r in rows:
        groups[(r.occurred_at_utc, observations[r.row_id].context.digest)].append(r)
    results = []
    for (when, context_digest), targets in sorted(groups.items()):
        context = observations[targets[0].row_id].context
        roster = sorted(set(local[r.competitor_id] for r in targets))
        result = calculate(
            bundle_root=args.bundle,
            signer=signer,
            trust=trust,
            scope_id="tournament:benchmark",
            round_id="round:benchmark",
            round_snapshot={
                "history_path": str(wb),
                "cutoff_at_utc": when,
                "history_sha256": h.source_sha256,
                "live_results": [],
                "frozen_at_utc": when,
                "weights": {"formula": "0.5", "ml": "0.5"},
            },
            competitor_ids=roster,
            target_context=context.to_dict(),
        )
        forecasts = {f["competitor_id"]: f for f in result["forecasts"]}
        for r in targets:
            f = forecasts[r.competitor_id]
            features, _ = bundle.normalize_features(r.feature_dict)
            ordered = [[features[n] for n in bundle.feature_names]]
            try:
                from strathmark.v3.factory.ml_training import predict_model_log_quantiles
            except ImportError:
                pred = bundle.universal_model.predict(ordered)[0]
            else:
                pred = predict_model_log_quantiles(bundle.universal_model, ordered)
            raw = math.exp(float(r.target_log_seconds))
            entry = {
                "row_id": r.row_id,
                "tournament_id": r.tournament_id,
                "event": context.event_code,
                "species": context.material_code,
                "size_mm": context.size_mm,
                "history_depth": r.feature_dict["history_depth"],
                "actual_seconds": raw,
                "universal": math.exp(float(pred[3])),
                "pinball": mean_pinball_loss(float(r.target_log_seconds), pred),
                "formula": f["formula"]["forecast"]["distribution"],
                "ml": f["ml"]["forecast"]["distribution"],
                "pool_seconds": f["predicted_time_ms"] / 1000,
                "pool": LinearPooledDistribution.from_dict(f["pool"]).quantile_summary().to_dict(),
            }
            for name in ("formula", "ml", "pool"):
                quant = entry[name]["quantiles"]
                entry[name + "_seconds"] = next(
                    q["time_ms"] / 1000 for q in quant if q["probability"] == "0.5"
                )
                lower = next(q["time_ms"] / 1000 for q in quant if q["probability"] == "0.05")
                upper = next(q["time_ms"] / 1000 for q in quant if q["probability"] == "0.95")
                entry[name + "_coverage90"] = lower <= raw <= upper
                losses = []
                for q in quant:
                    if q["probability"] in {"0.05", "0.25", "0.5", "0.75", "0.95"}:
                        probability = float(q["probability"])
                        residual = math.log(raw) - math.log(q["time_ms"] / 1000)
                        losses.append(max(probability * residual, (probability - 1) * residual))
                entry[name + "_log_pinball"] = sum(losses) / len(losses)
                entry[name + "_quantile_crps_seconds"] = (
                    float(
                        _quantile_crps(
                            PositiveTimeDistribution.from_dict(entry[name]),
                            Decimal(str(raw * 1000)),
                        )
                    )
                    / 1000
                )
            results.append(entry)
        print(json.dumps({"completed_rows": len(results), "total_rows": len(rows)}), flush=True)

    # V2 model is refit from exactly the same pre-2023 training population, using
    # the unchanged V2 fitting algorithm. It receives strictly prior person history.
    source = pd.read_excel(wb, sheet_name="Results")
    wood = pd.read_excel(wb, sheet_name="Wood")
    competitors = pd.read_excel(wb, sheet_name="Competitor")
    gender = dict(
        zip(
            competitors["CompetitorID"].astype(str),
            competitors.get("Gender", pd.Series("", index=competitors.index)),
        )
    )
    source["gender"] = source["CompetitorID"].astype(str).map(gender)
    v2 = PredictionV2Model.fit(
        build_prior_evidence(source, args.v2_training_cutoff, wood_df=wood),
        training_cutoff=args.v2_training_cutoff,
    )
    for entry in results:
        o = observations[entry["row_id"]]
        cutoff = date.fromisoformat(o.occurred_at_utc[:10])
        identity = local[str(o.competitor_id)]
        properties, missing = v2.resolve_species_properties(o.context.material_code)
        request = PredictionV2Request(
            identity,
            "UH" if o.context.event_code == "underhand" else "SB",
            o.context.size_mm,
            o.context.material_code,
            str(gender.get(identity, "")),
            cutoff,
            **properties,
            species_missing=missing,
        )
        forecast = v2.predict(request, history=build_prior_evidence(source, cutoff, wood_df=wood))
        entry["v2_seconds"] = forecast.median

    def metrics(selected):
        result = {"row_count": len(selected)}
        for name, key in [
            ("universal", "universal"),
            ("formula", "formula_seconds"),
            ("ml_calibrated", "ml_seconds"),
            ("v3_pool", "pool_seconds"),
            ("v2", "v2_seconds"),
        ]:
            errors = [abs(e[key] - e["actual_seconds"]) for e in selected]
            result[name] = {
                "mean_absolute_error_seconds": sum(errors) / len(errors),
                "median_absolute_error_seconds": median(errors),
                "p90_absolute_error_seconds": sorted(errors)[math.ceil(0.9 * len(errors)) - 1],
            }
        result["mean_log_pinball_loss"] = sum(e["pinball"] for e in selected) / len(selected)
        for name, key in (("formula", "formula"), ("ml_calibrated", "ml"), ("v3_pool", "pool")):
            result[name]["interval_90_coverage"] = sum(
                e[key + "_coverage90"] for e in selected
            ) / len(selected)
            result[name]["mean_common_quantile_log_pinball"] = sum(
                e[key + "_log_pinball"] for e in selected
            ) / len(selected)
            result[name]["mean_quantile_crps_seconds"] = sum(
                e[key + "_quantile_crps_seconds"] for e in selected
            ) / len(selected)
        return result

    summary = {
        "schema_version": "strathmark-private-native-benchmark-v1",
        "workbook_sha256": h.source_sha256,
        "bundle_digest": bundle.digest,
        "formula_digest": formula_digest,
        "audit_after_year": args.audit_after_year,
        "v2_training_cutoff": str(args.v2_training_cutoff),
        "v2_comparison": "unchanged V2 algorithm, chronologically refit on the declared training population",
        "tournament_count": len({r.tournament_id for r in rows}),
        "excluded": dict(h.excluded),
        "replay": "strictly-prior date-causal historical completions; no issued marks or legal-field claims",
        "overall": metrics(results),
        "slices": {
            category: {
                str(v): metrics([e for e in results if e[category] == v])
                for v in sorted({e[category] for e in results})
            }
            for category in ("event", "species", "size_mm")
        },
    }
    if load_workbook_history(wb, cutoff_at_utc=args.cutoff_at_utc).source_sha256 != h.source_sha256:
        raise ValueError("workbook changed during the benchmark")
    if load_formula_manifest(args.bundle).digest != formula_digest:
        raise ValueError("Formula component changed during the benchmark")
    if (
        load_ml_bundle(
            args.bundle,
            installed_catboost_version=catboost.__version__,
            installed_python_abi="cp313",
        ).digest
        != bundle.digest
    ):
        raise ValueError("model changed during the benchmark")
    args.output.mkdir(exist_ok=False, mode=0o700)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    (args.output / "private-row-receipts.json").write_text(json.dumps(results, indent=2))
    (args.output / "summary.json").chmod(0o600)
    (args.output / "private-row-receipts.json").chmod(0o600)
    print(json.dumps(summary["overall"], indent=2), flush=True)


if __name__ == "__main__":
    main()
