# Testing

```bash
pip install -e ".[dev,api]"
pytest tests -q
ruff check .
ruff format --check .
python train_model.py
```

Tests must use temporary isolated SQLite databases and must never point at production.
Real Supabase tests require an explicit isolated test project opt-in.

V2 coverage includes:

- strict prior-only/same-day/future/undated exclusion and unknown-species handling;
- hierarchical pooling, trend, cross-event state, positive support, and artifact safety;
- chronological calibration and residual-promotion rejection/acceptance rules;
- five-key compatibility, numeric LLM retirement, no-op inactive factors, and bundle
  consistency;
- deterministic 2,048-sample optimizer invariants and fallback;
- ledger atomicity, stable IDs, idempotency, privacy allowlist, settlement revisions,
  and non-blocking mirror failure;
- REST authentication, health metadata, stateless routes, and response fields.

CI also installs the optional ML extra in a focused job. CatBoost availability alone
does not make the residual active.

`python train_model.py` verifies published evidence without rescoring locked rows. Do
not delete the report or invoke `--open-locked-test` for the 2.0.0 release.
