# Installation

> **Authority status.** The commands below install the trusted V2.0.0 release. The V3
> release candidate is in later source and is under exact-source verification, but no production authority
> has changed. Installing a V3-capable wheel does not switch a consumer.

Python 3.10 or newer is required for the normal package and trusted V2 engine. V3
rehearsal and race-day authority require the designated Python 3.13 environment plus
`requirements/v3-release.lock`; Python 3.10-3.12 are not supported V3 authority
environments. Never enable SQLite `trusted_schema` to bypass that boundary.

```bash
python -m pip install "strathmark @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
python -m pip install "strathmark[api] @ git+https://github.com/SquirmyWormy275/STRATHMARK.git@v2.0.0"
```

Version 2.0.0 is published as the immutable `v2.0.0` Git tag and GitHub release;
there is no PyPI distribution for this version. High-assurance consumers may pin the
exact release commit. Optional extras are `api`, `ml`, `db`, `llm`, and `dev`.

The V2 NumPy/Pandas core and validated JSON artifact are in the base wheel. Installing
`ml` does not activate a residual model; promotion evidence and a compatible artifact
are still required. Installing `llm` does not add numeric prediction authority.

Verify an install:

```bash
python -c "import strathmark; print(strathmark.__version__)"
python train_model.py
```

The second command is for a source checkout and verifies the published artifact/report
without reopening locked rows.

For API use:

```bash
uvicorn strathmark.api:app --host 127.0.0.1 --port 8000
```

Check `/health`; core and calibration should be available. Residual inactive and Ollama
unavailable are expected/acceptable for numeric 2.0.0 operation.
