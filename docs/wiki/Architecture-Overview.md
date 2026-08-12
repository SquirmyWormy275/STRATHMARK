# Architecture Overview

One field request follows this path:

```text
validate field
  -> resolve one exclusive UTC cutoff
  -> snapshot one immutable core/residual/calibration bundle
  -> build strictly prior allowlisted evidence
  -> predict positive distributions with partial pooling
  -> apply promoted residual only if active
  -> optimize field marks from 2,048 shared samples
  -> return backward-compatible results
  -> optionally append trusted local ledger transaction
  -> optionally mirror to Supabase best-effort
```

Core modules:

- `features.py`: causal evidence boundary and exclusion diagnostics.
- `prediction_v2.py`: hierarchical log-time core and conformal calibration.
- `residual.py` and `validation.py`: optional learner and promotion evidence.
- `predictor.py`: artifact provider and legacy-key projection.
- `calculator.py`: field orchestration and performance variability.
- `mark_optimizer.py`: deterministic joint mark search.
- `ledger.py`: append-only trusted SQLite evidence.
- `api.py`: stateless public and authenticated persistence routes.
- `variance.py`: independent post-mark Monte Carlo audit.

The base package is offline-capable. Database, LLM, and CatBoost failures cannot remove
the core calculation path. See [Prediction Engine V2](Prediction-Engine-V2) and
[Persistence and Database](Persistence-and-Database).
