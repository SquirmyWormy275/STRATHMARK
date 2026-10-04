# STRATH accuracy preview 0.1.0

This separately packaged tool compares the installed baseline and a frozen calibration
candidate in **raw cutting seconds**. It runs the actual native Formula and universal
ML forecasters on a verified read-only workbook snapshot. The two distributions stay
separate and their pool retains equal weights. Supported-history distributions receive
one frozen scale; first appearances receive their separate frozen PIT maps and interval
floors. Original native forecasts are retained alongside the candidate output.

The tool never fits a model or recalibrates while forecasting. It provides no field
assembly, marks, issue, settlement or competition-engine selection operations. It does
not load live authority keys or databases. Its in-memory signer is only the native
historical projection's consistency mechanism. SHA digests establish component
consistency; they do not establish competition authority.

## Install and build

Use a separate Python 3.13 environment with the exact STRATHMARK 3.0.0rc7 runtime,
CatBoost 1.2.10 and cryptography 50.0.2 already installed:

```bash
python -m pip install --no-deps ./tools/accuracy-preview
python -m build --no-isolation tools/accuracy-preview
strath-accuracy-preview --help
```

The tool has its own package namespace. Adding it does not change STRATHMARK's
implementation digest or the installed STRATHEX competition profile. The public wheel
contains code only; private model weights, workbook data and calibration artifacts
remain outside either source repository.

## Prepare frozen private inputs

Training or evaluating owner data requires explicit owner authorization. Copy the
workbook without modifying the original; an existing destination is refused:

```bash
strath-accuracy-preview snapshot --source /private/authorized-history.xlsx \
  --output /private/preview/history-copy.xlsx
strath-accuracy-preview pack --candidate /private/preview/candidate \
  --bundle /private/frozen/ml-bundle --formula /private/frozen/formula_manifest.json \
  --calibration /private/frozen/calibration.json --provenance /private/frozen/provenance.json
strath-accuracy-preview status --candidate /private/preview/candidate
```

`calibration.json` has exactly `supported_scale`, `supported_rows`, `supported_groups`,
`cold_rows`, `cold_groups`, `cold_formula` and `cold_ml`. The two cold objects use the
existing native PIT calibrator schema. Each segment needs at least 20 actual calibration
observations from five actual competition groups; pseudo observations do not count.
Both cold interval floors must be positive. The provenance JSON records the original
frozen model, coefficient and map digests and the experiment's qualification status.
Packing copies components, refuses symlinks and verifies the model before activation.

## Run

```bash
strath-accuracy-preview menu --candidate /private/preview/candidate \
  --workbook /private/preview/history-copy.xlsx --report /private/preview/audit.json
strath-accuracy-preview forecast --candidate /private/preview/candidate \
  --request /private/request.json --output /private/new-preview.json
```

The forecast request contains exactly `workbook`, `cutoff_at_utc`, `competitor_ids` and
`target_context`. Context uses STRATHMARK's existing `TargetContext.to_dict()` schema.
The menu constructs it from the selected event, log diameter and species code.
An exclusive cutoff admits only earlier dated completions, including when several
target results share the same date. Undated legacy rows remain excluded; no dates or
round boundaries are invented. Changed inputs, incompatible native source, component
tampering, invalid requests and reconstruction differences stop the preview without
falling back to another engine. Outputs refuse overwrite and use private permissions.

## Historical development comparison

```bash
strath-accuracy-preview audit --candidate /private/preview/candidate \
  --workbook /private/preview/history-copy.xlsx \
  --since 2025-01-01T00:00:00.000Z --until 2026-02-07T00:00:00.000Z \
  --output /private/preview/new-audit.json
```

This runs every admitted row in the selected cohort through the native forecast route
at that row's prior-only cutoff. Actual times are read only for scoring. Private row
receipts retain every result, uncertainty coverage and source identities. This command
does not assess qualification or turn examined history into an independent future test.

The October 2026 development candidate reduced historical MAE from 23.12494 to
22.47473 seconds on 278 already-examined results in 20 competition groups (2.81%).
Its 20 first-appearance results decreased from 85.04505 to 76.51895 seconds; substantial
errors remain. This fails the original 5% overall improvement requirement and has no
independent future qualification. Installing this tool leaves the competition model
and each root's deliberate V2/V3 choice intact.

## Verify with synthetic data

```bash
STRATHMARK_TEST_DB=1 STRATHMARK_DB_PATH=/tmp/preview-v2.sqlite \
STRATHMARK_V3_DB_PATH=/tmp/preview-v3.sqlite \
PYTHONPATH=tools/accuracy-preview python -m pytest tools/accuracy-preview/tests \
  --basetemp /tmp/preview-tests -p no:cacheprovider
```

These tests generate synthetic workbooks and synthetic distributions. They never open
operator paths, owner workbooks, real model artifacts, live storage or signing keys.
