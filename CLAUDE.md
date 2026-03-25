# STRATHMARK

Woodchopping handicap engine - pip-installable calculation core.

## Commands

```bash
pip install -e ".[dev]"    # install with dev dependencies
pytest tests/ -v           # run all tests (667 tests)
python train_model.py      # train XGBoost model
python evaluate_llm_prompts.py  # evaluate LLM prompt templates
```

## Design Rules (enforced in all sessions)

- Mark floor: 3 seconds (never lower under any circumstances)
- Mark ceiling: 183 seconds system-wide (180s time limit + 3s minimum mark)
- Gap logic: slowest -> Mark 3; each second faster -> +1 mark; standard rounding (round half-to-even)
- Variance: absolute +/- 3 seconds ONLY -- proportional variance is forbidden
- Prediction cascade: Manual > LLM > ML > Panel mark fallback
- Time-decay: exponential decay, 2-year half-life (730 days)
- Tournament weighting: same-tournament results = 97% weight, historical = 3%
- Output: plain text only, no emojis, no ANSI color codes
- Style: lean and simple, no unnecessary complexity

## Mark Formula

```
gap = predicted_time(competitor) - predicted_time(front_marker)
mark = 3 + round(gap)   # standard rounding (half-to-even)
mark = min(mark, 183)   # system-wide ceiling
```

## Project Structure

```
strathmark/
    __init__.py         Public API (HandicapCalculator, CompetitorRecord, WoodProfile)
    calculator.py       Mark computation, gap logic, start sheet
    predictor.py        Prediction cascade (Manual > LLM > ML > panel fallback)
    variance.py         Absolute variance model, Monte Carlo simulation (500K races)
    wood.py             Species properties, diameter scaling, quality adjustment
    decay.py            Exponential time-decay weighting (2-year half-life)
    fallback.py         Panel marks and event baseline fallbacks
    config.py           All constants as frozen dataclasses
    store.py            SQLite local result store (~/.strathmark/results.db)
    db.py               Supabase/PostgreSQL backend (push/pull results)
    loader.py           Excel workbook loader (woodchopping_clean.xlsx)
    utils.py            Column standardization, prediction accuracy scoring
    analytics.py        Backtesting, competitor profiling, performance history
    fairness.py         AI-assisted fairness assessment (Ollama LLM)
    visualization.py    Plain-text simulation summaries and ASCII bar charts
    llm.py              Ollama connection management and prompt execution
    llm_roles.py        Extended LLM roles (profiles, commentary, anomaly detection)
    api.py              FastAPI REST API (calculate, predict, simulate, results)
tests/
    test_calculator.py  Mark invariants (floor, ceiling, gap logic) -- 28 tests
    test_variance.py    Absolute variance, consistency ratings, Monte Carlo -- 13 tests
    test_integration.py Full pipeline from Excel workbook to mark sheet -- 7 tests
scripts/
    train_model.py          XGBoost training pipeline (26 features, temporal CV)
    evaluate_llm_prompts.py Prompt template evaluation and selection
    import_legacy.py        Legacy Excel import with validation
```

## gstack

This project uses [gstack](https://github.com/garrytan/gstack) for development workflow automation. Available skills:

### Workflow Skills
- `/ship` - Ship workflow: run tests, review diff, bump version, create PR
- `/review` - Pre-landing PR review for structural issues
- `/investigate` - Systematic debugging with root cause analysis
- `/qa` - QA test a web application and fix bugs found
- `/qa-only` - Report-only QA testing (no fixes)
- `/retro` - Weekly engineering retrospective

### Planning & Review Skills
- `/office-hours` - Brainstorm and validate ideas (YC Office Hours style)
- `/plan-ceo-review` - CEO/founder-mode plan review (scope and ambition)
- `/plan-eng-review` - Engineering manager plan review (architecture and execution)
- `/plan-design-review` - Designer's eye plan review (UI/UX critique)
- `/autoplan` - Auto-run all reviews sequentially

### Browser & Testing Skills
- `/browse` - Headless browser for QA, screenshots, and site testing
- `/benchmark` - Performance regression detection
- `/canary` - Post-deploy canary monitoring
- `/setup-browser-cookies` - Import browser cookies for authenticated testing

### Design Skills
- `/design-consultation` - Create a design system from scratch
- `/design-review` - Visual QA audit with iterative fixes

### Safety & Security Skills
- `/careful` - Safety guardrails for destructive commands
- `/freeze` / `/unfreeze` - Restrict/unrestrict file edit scope
- `/guard` - Full safety mode (careful + freeze combined)
- `/cso` - Security audit (OWASP Top 10, STRIDE threat modeling)

### Deployment & Docs Skills
- `/land-and-deploy` - Merge PR, wait for CI, verify production health
- `/setup-deploy` - Configure deployment settings
- `/document-release` - Post-ship documentation update

### Utility Skills
- `/codex` - OpenAI Codex second opinion (review, challenge, consult)
- `/gstack-upgrade` - Upgrade gstack to latest version
