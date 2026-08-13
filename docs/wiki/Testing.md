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
- exhaustive small-field optimizer oracles plus the bounded 64-competitor capacity gate;
- ledger atomicity, versioned request hashes, fail-closed training eligibility, direct
  issued-interval coverage, privacy allowlist, settlement revisions, and one bounded
  non-blocking mirror worker;
- REST authentication, health metadata, stateless routes, and response fields.

CI also installs the optional ML extra in a focused job, tests coordinated oldest and
current API dependency sets, verifies normalized output on Windows and Linux, and
smoke-tests installed wheels and source distributions outside the checkout. CatBoost
availability alone does not make the residual active.

`python train_model.py` verifies the separately attested published evidence without
rescoring locked rows. Do not regenerate the attestation, delete the report, or invoke
`--open-locked-test` for the 2.0.0 release.
