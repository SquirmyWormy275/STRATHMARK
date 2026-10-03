# Linux setup and portable verification

Linux and Windows share the V2 library/API and current source demonstration command. Python 3.13 runs current development and portable V3 tests. No separate Linux fork is needed.

For complete Formula + trained ML competitions with STRATHEX 7.3.1, use the
[Linux local competition runbook](V3_LINUX_COMPETITION.md). It covers the separate
Python 3.13 interpreter, `v3-candidate` dependencies, verified trained model,
persistent authority, required independent recovery directory, and full
approval/issue/results/next-round workflow. The older
[numeric candidate](V3_LINUX_CANDIDATE.md) remains a separate retained preview.

The following source setup runs the synthetic V2 demo and portable tests:

```bash
python3.13 -m venv .venv-linux
.venv-linux/bin/python -m pip install -e ".[dev,api]"
.venv-linux/bin/python -m pip install -r requirements/v3-release.lock
.venv-linux/bin/strathmark demo
```

The demo uses synthetic competitors and the released V2 calculation API; it opens no live database. Current package metadata is `3.0.0rc4`; the demo does not grant Windows CNG production eligibility. For the trusted published V2 library, install `strathmark==2.0.1` in a separate environment. The V2 API starts with `python -m uvicorn strathmark.api:app --host 127.0.0.1 --port 8000`; configure an absolute `STRATHMARK_DB_PATH` first. That ASGI app is V2. V3 services require explicit authenticated, source-bound composition described in [deployment](DEPLOYMENT.md).

Use a fresh scratch directory per test run, separate from operator data:

```bash
scratch=$(mktemp -d)
export STRATHMARK_TEST_DB=1
export STRATHMARK_DB_PATH="$scratch/v2.sqlite3"
export STRATHMARK_V3_DB_PATH="$scratch/v3.sqlite3"
export STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION=0
.venv-linux/bin/python -m pytest tests --basetemp "$scratch/pytest" -p no:cacheprovider
.venv-linux/bin/python scripts/replay_v3.py
.venv-linux/bin/python scripts/freeze_v3_consumer_contract.py --check
```

The explicit `0` marks a portable test run and skips the independent exact-artifact workbook rebuild. Its cached workbook/numeric/causality tests still run. For designated qualification, use [formula runtime verification](FORMULA_RUNTIME.md), remove the portable flag or set it to `1`, and retain exact installed-source evidence. The production native optimizer DLL, Windows CNG identities, installed model family executors, ACLs, capacity evidence, and signed eligibility handoff remain Windows installation gates. Portable tests and replay do not provision them.

Run `ruff check .`, `ruff format --check .`, `python scripts/check_docs.py`, `python -m build`, and `python scripts/smoke_installed_distribution.py --kind wheel` plus `--kind sdist` for packaging delivery. Installed smokes use disposable data outside the checkout. Read [ONBOARDING](../ONBOARDING.md) before changing prediction behavior.
