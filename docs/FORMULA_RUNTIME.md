# Designated formula workbook runtime

The independent rebuild is an exact-artifact qualification gate. Its frozen receipt at `benchmarks/v3/formula_engine_verification.json` requires **Node v24.19.0** and **@oai/artifact-tool 2.8.52**, plus the workbook/formula graph, inspection, numeric output, render hashes, and receipt digest. Provision that exact authorized tool environment on the designated host. A newer package with identical numbers cannot satisfy the old artifact identity.

Set `STRATHMARK_ARTIFACT_NODE` to the absolute Node executable and `STRATHMARK_ARTIFACT_NODE_MODULES` to the containing node_modules directory. Then run:

```bash
python scripts/verify_formula_runtime.py
# In a fresh isolated test environment with an explicit --basetemp:
STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION=1 python -m pytest tests/v3/evals/test_formula_replay.py --basetemp /absolute/path/to/scratch/pytest -p no:cacheprovider
```

The preflight refuses missing tools or mismatched versions before rebuilding. Required mode remains the default and never silently skips this check. General hosted CI and documented portable runs explicitly set the flag to `0`; those runs skip the designated rebuild even if another machine-local artifact tool happens to be present.

The October Linux audit found tool 2.8.59. An independent scratch rebuild preserved numeric outputs and the formula dependency graph but changed workbook/render hashes and the receipt digest. No frozen workbook or signed receipt was replaced to conceal that mismatch. Refreshing evidence requires a reviewed environment/source change and the full designated evidence workflow in [deployment](DEPLOYMENT.md), with production identity and eligibility verified separately.
