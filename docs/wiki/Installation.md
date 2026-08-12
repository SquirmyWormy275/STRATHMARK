# Installation

Python 3.10 or newer is required.

```bash
pip install strathmark
pip install "strathmark[api]"       # FastAPI and Uvicorn
pip install "strathmark[ml]"        # optional residual tooling
pip install "strathmark[db]"        # Supabase helpers
pip install "strathmark[llm]"       # narrative-only Ollama/Gemini features
pip install "strathmark[dev]"       # tests and Ruff
```

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
