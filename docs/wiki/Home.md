# STRATHMARK Wiki

STRATHMARK is a pip-installable woodchopping handicap engine. It takes a
field of competitors, a block of wood, and an event code, and returns a
start sheet that gives every competitor an equal chance of winning — the
front marker starts at Mark 3, the back marker starts later by however
many seconds faster they are predicted to cut.

The engine was extracted from STRATHEX (the full tournament-management
system) so that external applications — scoring apps, tournament
software, analysis tools, the Missoula Pro-Am Manager — can compute
identical handicap marks without depending on the full STRATHEX code
base. Every downstream tool points at the same calculator, so fixing a
bug here fixes it everywhere.

## Wiki contents

- [Installation](Installation)
- [Quick Start](Quick-Start)
- [Architecture Overview](Architecture-Overview)
- [Prediction Cascade](Prediction-Cascade)
- [Handicap Mark Math](Handicap-Mark-Math)
- [Rulebook Comparison](Rulebook-Comparison)
- [Wood and Diameter Scaling](Wood-and-Diameter-Scaling)
- [Time-Decay Weighting](Time-Decay-Weighting)
- [Variance and Monte Carlo Simulation](Variance-and-Monte-Carlo)
- [Fairness Assessment](Fairness-Assessment)
- [Persistence and Database](Persistence-and-Database)
- [LLM Integration (Ollama and Gemini)](LLM-Integration)
- [REST API](REST-API)
- [Deployment](Deployment)
- [Testing](Testing)
- [FAQ](FAQ)

## Design rules (invariants)

These rules are enforced across every cascade level and every release.
They are not negotiable.

- **Mark floor:** 3 seconds. No competitor can ever be given a mark
  lower than 3.
- **Mark ceiling:** 183 seconds system-wide (180 s time limit +
  3 s minimum). Individual events may enforce a lower ceiling.
- **Gap logic:** the slowest predicted competitor gets Mark 3; each
  full second faster than the slowest adds one mark, using standard
  banker's rounding (round half-to-even).
- **Variance:** absolute ±3 seconds only. Proportional variance
  (e.g. ±5 % of predicted time) is forbidden because it gives faster
  competitors a systematic advantage.
- **Prediction cascade:** Manual override > LLM > ML > Weighted
  baseline > Panel mark fallback. Higher priority always wins when
  available.
- **Time-decay:** exponential with a standard half-life of 730 days
  (2 years). Adaptive to 365 / 730 / 1095 days depending on how
  active the competitor has been.
- **Same-tournament weighting:** actual times from earlier rounds on
  the same wood are weighted 65 % / 80 % / 90 % / 97 % as the round
  count rises, with historical data getting the remainder.
- **Output:** plain text only. No emojis, no ANSI colour codes. The
  start sheet is 70 characters wide for terminal and thermal-printer
  compatibility.

## Where STRATHMARK fits

```
+--------------------------+
| STRATHEX                 |  full tournament management (Python)
|                          |
| + Pro-Am Manager (2026)  |  race-day scoring UI
| + future tournament apps |
+-----------+--------------+
            |
            v  import strathmark
+--------------------------+
| STRATHMARK               |  calculation engine (this repo)
|                          |
| + calculator.py          |
| + predictor.py           |
| + variance.py, ...       |
+--------------------------+
```

STRATHMARK is intentionally UI-free, state-free, and tournament-agnostic.
It has no concept of entries, prize money, or scoring; it only knows
how to turn historical results into a fair set of marks.

## Status

Version 1.0.0. The project has automated coverage across calculator,
variance, integration, predictor, fairness, analytics, loader, store,
db, llm, llm_roles, visualization, wood, decay, fallback, config, utils,
API, deployment-fallback, and regression suites.
