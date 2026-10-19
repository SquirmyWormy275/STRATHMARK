# Designated formula workbook runtime

The independent rebuild is an exact-artifact qualification gate. Its frozen receipt at `benchmarks/v3/formula_engine_verification.json` requires **Node v24.19.0** and **@oai/artifact-tool 2.8.52**, plus the workbook/formula graph, inspection, numeric output, render hashes, and receipt digest. Provision that exact authorized tool environment on the designated host. A newer package with identical numbers cannot satisfy the old artifact identity.

Set `STRATHMARK_ARTIFACT_NODE` to the absolute Node executable and `STRATHMARK_ARTIFACT_NODE_MODULES` to the containing node_modules directory. Also set `STRATHMARK_FORMULA_TOOLCHAIN_MANIFEST` to the independently reviewed tool inventory and `STRATHMARK_FORMULA_TOOLCHAIN_SHA256` to its digest from the trusted provisioning record. Neither version output nor a manifest generated from the proposed installation is sufficient approval. Then run:

```bash
python -B scripts/verify_formula_runtime.py
# In a fresh isolated test environment with an explicit --basetemp:
STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION=1 python -B -m pytest tests/v3/evals/test_formula_replay.py --basetemp /absolute/path/to/scratch/pytest -p no:cacheprovider
```

The manifest is a JSON object with exactly `schema_version` (value `strathmark-formula-toolchain-v1`), `node_sha256` and `module_files`. `module_files` maps every regular file's slash-separated path relative to `node_modules` to its lowercase SHA-256. Internal links are recorded as `symlink:` plus the exact link target; external or broken links are refused. The complete inventory is compared, so added files are rejected as well as missing or altered files. Obtain this manifest and its independently approved digest from the reviewed tool supply/provisioning process; the verifier deliberately has no command that blesses the current installation.

The preflight authenticates manifest, executable and module bytes before invoking Node, checks the frozen versions, then authenticates them again. The designated workbook replay also checks identities after execution and writes a separate `formula-toolchain-verification.json` in its scratch directory binding tool hashes to the proof output. It does not replace the frozen workbook receipt. Child processes receive no ambient `NODE_OPTIONS` or `NODE_PATH`. Missing approvals, modified same-version tools or mismatched versions fail before rebuilding. Required mode remains the default and never silently skips this check. General hosted CI and documented portable runs explicitly set the flag to `0`; those runs skip the designated rebuild even if another machine-local artifact tool happens to be present.

The October Linux audit found tool 2.8.59. An independent scratch rebuild preserved numeric outputs and the formula dependency graph but changed workbook/render hashes and the receipt digest. No frozen workbook or signed receipt was replaced to conceal that mismatch. Refreshing evidence requires a reviewed environment/source change and the full designated evidence workflow in [deployment](DEPLOYMENT.md), with production identity and eligibility verified separately.

See [qualification tool byte identity](QUALIFICATION_TOOLCHAIN.md) for the Python wheel trust boundary and historical evidence policy. No existing designated installation is automatically approved by this change.
