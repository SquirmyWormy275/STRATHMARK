# LLM Narrative Features

LLMs have no numeric authority in Prediction Engine V2. They cannot generate, select,
multiply, or adjust a finish-time prediction or handicap mark. The legacy `llm` result
key is always `None`, and passing `llm_client` does not alter numeric output.

Optional Ollama/Gemini integrations remain useful for narrative-only work such as
commentary, competitor profiles, anomaly explanations, and a clearly labeled prose
fairness assessment. Those outputs must never be parsed back into V2 numbers.

`GET /health` reports Ollama separately so operators can diagnose narrative features.
An unavailable LLM is not a degraded numeric prediction state and does not trigger a
different model.

Install narrative dependencies with:

```bash
pip install "strathmark[llm]"
```

Keep prompts and responses free of unnecessary personal data. Narrative output is not
trusted training evidence and is not stored in the V2 feature ledger.
