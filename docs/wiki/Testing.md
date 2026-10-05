# Contributor setup and testing

Use Python 3.13 for V3. Read
[ONBOARDING](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/ONBOARDING.md)
and [how handicaps work](Handicap-Mark-Math) before changing prediction behavior.

## Install development dependencies

In a separate virtual environment, from the repository root:

```bash
python -m pip install -e ".[dev,api]"
python -m pip install -r requirements/v3-release.lock
```

## Run portable tests on Linux

Use a fresh directory so tests cannot open a live workbook or database:

```bash
scratch=$(mktemp -d)
export STRATHMARK_TEST_DB=1
export STRATHMARK_DB_PATH="$scratch/v2.sqlite3"
export STRATHMARK_V3_DB_PATH="$scratch/v3.sqlite3"
export STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION=0
python -m pytest tests --basetemp "$scratch/pytest" -p no:cacheprovider
python scripts/replay_v3.py
```

The documented-example tests in this suite verify the frozen OpenAPI checksum,
route table and examples. `freeze_v3_consumer_contract.py` regenerates the contract;
it is not a verification command.

The portable flag skips the independent workbook-engine rebuild. It is for portable
testing, not designated release qualification. Use [formula qualification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/FORMULA_RUNTIME.md)
for that environment. Python 3.10–3.12 compatibility runs cover V2 and exclude `tests/v3`.
Do not enable SQLite `trusted_schema` to bypass an incompatible V3 interpreter.

## Routine checks

```bash
python -m ruff check .
python -m ruff format --check .
python scripts/check_docs.py
python -m build
python scripts/smoke_installed_distribution.py --kind wheel
python scripts/smoke_installed_distribution.py --kind sdist
```

The installed smokes use disposable inputs outside the checkout. Prediction changes
need a failing regression before the fix; fixtures and tests remain synthetic.
Owner-authorized historical evaluation belongs in an isolated read-only copy, not a
live test database.

## Release evidence

[Deployment](Deployment) links the complete designated rehearsal and production
verification procedure. A rehearsal is bound to its committed source, exact wheel,
dependencies and machine. Passing portable tests does not provision Windows production
keys or qualify the operational council/factory.
