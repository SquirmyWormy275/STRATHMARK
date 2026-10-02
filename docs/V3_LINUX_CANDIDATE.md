# Linux V3 numeric candidate

The separate Python 3.13 profile runs the actual Formula assessor, an exported and
verified CatBoost ML bundle, their linear distribution pool, and the V3 joint mark
optimizer. It is usable for numeric previews with STRATHEX's V2/V3 selector.
It has its own `strathmark.v3-linux-numeric-candidate.v1` subprocess contract and
does not claim to implement the authenticated V7 lifecycle.

## Train a candidate

Use an explicitly authorized, independently verified workbook copy. The reader
never writes the workbook. Keep history, models, and reports in private storage
outside the source repository; do not publish production-derived artifacts.
The production-data prohibition in ONBOARDING remains the default unless the
data owner explicitly authorizes this training. Synthetic data needs no such exception.

```bash
python3.13 -m venv .venv-v3
.venv-v3/bin/python -m pip install -r requirements/v3-release.lock
.venv-v3/bin/python -m pip install 'dist/strathmark-3.0.0rc3-py3-none-any.whl[v3-candidate]'
.venv-v3/bin/strathmark-v3-train-candidate \
  --workbook /absolute/private/authorized-history.xlsx \
  --output /absolute/private/new-candidate \
  --source-commit ACTUAL_WHEEL_SOURCE_COMMIT \
  --source-repository /absolute/path/to/STRATHMARK \
  --cutoff-at-utc 2026-10-02T00:00:00.000Z
.venv-v3/bin/strathmark-v3-linux-candidate status \
  --ml-bundle /absolute/private/new-candidate/ml-bundle
```

The legacy schema is Competitor/CompetitorID, Wood/speciesID/spec_gravity,
and Results with CompetitorID, Event, Time (seconds), Size (mm), Species Code,
Date (optional), and optional competition notes. UH and SB become canonical V3
events; specific gravity becomes density in kg/m³. Undated or invalid results
are counted as exclusions. Raw completions never acquire authenticated issue facts.

Defaults use whole annual competition groups through 2022 for training, 2023 for
tuning, 2024 for calibration, and 2025 onward for diagnostic holdout evaluation. Explicit year flags
can select other strictly chronological boundaries. Holdout predictions fit on
training rows only. Feature construction excludes each target and future rows.
A coordinator partitions the copy, then passes only TRAIN/TUNE/CAL facts to a separate builder process. A separate evaluator receives holdout facts after the bundle is frozen. Earlier-role context is retained for causal features; each target and future observation are excluded. Candidate and evaluator use separate development keys. These development processes share an OS identity and do not constitute a production blind audit. Neither can authorize production.
The report records the workbook, installed implementation, roles, model digest,
settings, exclusions, and diagnostic measurements. The Git revision is checked against every installed Python source byte; that revision and implementation digest are bound into model metadata. Insufficient grouped gate evidence omits all specialists. Output directories cannot be overwritten.

## What the profile supports

STRATHEX passes a frozen per-competition workbook snapshot to a separate V3
interpreter. The selection binds the exact Python implementation, Formula manifest,
model bundle, and package version. A changed artifact blocks a saved competition.
Forecasting supports up to 128 competitors and carries no marks. Exact-field
previews support 1–12 competitors and optimize 4,096 deterministic draws per
competitor, with faster competitors receiving later starts and a Mark 3 field reference.
Repeated requests and restart must preserve the numeric evidence.

Formula and ML must both execute; an abstention blocks the request. There is no
V2 substitution. The unavailable LLM council, equal bootstrap weights, and independent
bootstrap dependence remain explicit. Sample spread is reported, not invented as zero.

## Limits

This is an unpromoted candidate, not an accuracy-qualified race-day engine. It cannot
approve, issue, settle results, or learn from the next round. Proposed marks cannot
be official start sheets. The full V7 runtime still requires family composition,
approved bundles, actual lifecycle reactions, and the installation qualification
described in [deployment](DEPLOYMENT.md) and [formula qualification](FORMULA_RUNTIME.md).
Windows production CNG and capacity gates remain intact. Portable preview success
does not satisfy them.

The separate STRATHEX runbook gives the concrete selector launch and installed
synthetic verification command. Keep its V2 environment separate; installing V3
over that pinned V2 dependency breaks the baseline.
