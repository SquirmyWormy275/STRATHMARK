# FAQ

Frequently asked questions about STRATHMARK. Answers lean on the
rulebook comparison and the cascade design; see the linked pages for
the full picture.

## General

### What does STRATHMARK do?

It takes a field of woodchopping competitors, a block of wood, and an
event code (SB or UH), and returns a start sheet that gives each
competitor a delayed-start handicap such that everyone has an equal
chance of winning. The slowest predicted competitor starts at Mark 3;
every other competitor starts one second later for every second they
are predicted to cut faster.

### Why is this better than a hand-calculated handicap?

Hand-calculated handicaps are as good as the handicapper. STRATHMARK
produces a prediction through the same reasoning a good handicapper
would — historical average, adjustment for today's wood, adjustment
for recent form — but applies it consistently to every competitor and
documents *exactly* why each mark was chosen. Officials can still
override any mark they disagree with.

See [Rulebook Comparison](Rulebook-Comparison) for how the engine
relates to ALA, AAA, and QAA rules.

### Is it pip-installable?

Yes. `pip install strathmark`. Extras for FastAPI, Ollama + Gemini
LLM, XGBoost + LightGBM + scikit-learn ML, and Supabase backend are
all optional and lazily imported.

### Why the name "STRATHMARK"?

*STRATH* from the STRATHEX family (the tournament management system
this engine was extracted from). *MARK* because the output is a
handicap mark. Fits on a thermal-printer header line.

## Data requirements

### How much data do I need per competitor?

- **Minimum** — 3 historical times for the specific event (SB or UH).
- **Ideal** — 8+ historical times for high-confidence baseline
  predictions.
- **ML tier** — the global database needs 100+ results total and 75+
  per event type before XGBoost trains.

See [Prediction Cascade](Prediction-Cascade) for what the cascade
does below each threshold.

### What if a competitor has zero history?

`fallback.get_panel_mark()` returns a division-based default time
(Open / Novice / Junior / Veterans / Womens). Confidence is `VERY
LOW` and the explanation records that no history was available. The
competitor can still compete — the mark just has wider uncertainty.

### Can a competitor be in more than one division at the same event?

Yes. `CompetitorRecord.division` is a string; pass whatever division
label the event uses. The cascade is division-neutral (an experienced
competitor is modelled the same way whether Junior or Open); division
only affects panel-mark fallback.

## Methodology

### Why round using banker's rounding (round-half-to-even)?

Python's built-in `round()` is banker's rounding. Two reasons:

1. **Unbiased.** Ceiling rounding introduces a systematic +0.5 s bias
   per competitor. Over a heat of 8, that is 4 s of artificial spread.
2. **AAA-compliant.** The AAA rulebook (Rule 17) says handicaps are
   calculated to the nearest second — the literal Python definition
   of "nearest second" for a float is banker's rounding.

See [Handicap Mark Math](Handicap-Mark-Math#rounding-choice--bankers-rounding)
for the Monte Carlo comparison.

### Why absolute ±3-second variance instead of ±5 %?

Proportional variance systematically advantages fast competitors.
Empirical testing (from STRATHEX, replicated in STRATHMARK):

- Absolute ±3 s: 6.7 % win-rate spread.
- Proportional ±5 %: 31 % win-rate spread.

Real-world factors (knot in the grain, an axe wobble) cost every
competitor the same number of seconds regardless of skill. Full
detail in
[Variance and Monte Carlo](Variance-and-Monte-Carlo#the-absolute-variance-rule).

### Why the 2-year half-life?

The 730-day half-life was calibrated against STRATHEX's historical
database. It balances two concerns:

- **Short half-life** (e.g. 180 days) over-reacts to a single bad
  event and ignores years of consistent performance.
- **No decay** over-weights old results; an aging competitor's 10-year
  history pulls them toward their peak, not their current form.

730 days is the sweet spot for most competitors. Adaptive
365 / 730 / 1095 handles outliers (very active, very inactive).

### Why five cascade levels and not just ML?

ML needs 75+ training records per event. Most real competition
databases cross that threshold only after years of data collection.
The cascade degrades gracefully so the engine works from day 1 (panel
marks) through day 100 (baseline) to day 1000 (full ML-led cascade).

### What happens if ML predicts something weird?

ML predictions outside `[5.0, 300.0]` seconds are rejected; the
cascade falls through to baseline. Predictions inside that range are
passed through an isotonic calibrator (when fitted) that corrects
systematic over- or under-prediction.

## Operational

### Can judges override a mark?

Yes — `calculator.calculate(..., manual_overrides={name: time})` or
set `CompetitorRecord.manual_time_override`. Overrides are recorded
in the start-sheet explanation as the reason the mark was chosen.

### What if Ollama is not running?

The LLM tier returns `None`; the cascade falls through to ML →
baseline → panel. The engine is designed to produce correct marks
without any LLM. See
[LLM Integration](LLM-Integration#race-day-failure-modes).

### Can I run STRATHMARK offline?

Yes. The only tier that requires network connectivity is Supabase
(authority for cross-device sync). The local SQLite store at
`~/.strathmark/results.db` is fully self-contained.

### Can I run it from a web app?

Yes. `pip install strathmark[api]` and
`uvicorn strathmark.api:app --port 8000`. See [REST API](REST-API).

### How fast is it?

- **Baseline cascade** (no LLM, no ML): <1 ms per competitor.
- **ML cascade**: ~1 ms per competitor after the first call (XGBoost
  trains once, ~1–5 s depending on data size).
- **LLM cascade**: 1–5 s per competitor (bottlenecked by Ollama).

For a heat of 8 competitors with the full cascade, expect 5–15 s
total.

## Rulebook questions

### Is this AAA-compliant?

Yes for the mark-math core: floor 3, timing to 0.01 s, integer marks
rounded to the nearest second, ceiling under the 3-minute time limit.
The AAA rulebook leaves the handicap *method* to the Committee or
Handicapper — STRATHMARK is one such method.

See [Rulebook Comparison](Rulebook-Comparison) for the full matrix.

### Is this ALA-compliant?

The ALA rulebook does not define a formal handicap method; individual
shows set their own under Sanctioning Rule 3. STRATHMARK's mark-math
is compatible with any ALA show that wants delayed-start
handicapping. The ALA Grand Finals points system
(10-7-5-3-1) is outside STRATHMARK's scope — that belongs in the
tournament manager.

### Is this QAA-compliant?

STRATHMARK predicts the *time*. QAA publishes *handicap scale
tables* that convert a bookmark into a mark for different wood sizes
and hardness classes. The two layers are complementary — STRATHMARK's
predicted time determines where the competitor sits on the QAA
scale. STRATHMARK does not implement the QAA penalty/award system
(1 s per $60 prize money, etc.) — that is the downstream tournament
manager's job.

### Can I plug in a different rulebook?

The invariants (floor 3, ceiling 183, gap logic, banker's rounding,
absolute variance) are hard-coded because they are the
mathematically-sound choice, not the rulebook-specific choice. Other
knobs are configurable:

- Per-event ceiling via `HandicapCalculator(event_ceiling=N)`.
- Panel marks per division via `fallback.py` data.
- Species and diameter scaling via the wood table.
- Half-lives and thresholds via env overrides on `config.py`.

If your rulebook requires a different gap formula (e.g. gap to the
median instead of the slowest), fork the cascade — `_assign_marks()`
is four lines.

## Development

### How do I add a new species?

Add a row to the `Wood` sheet of `woodchopping_clean.xlsx` with
Janka, specific gravity, shear, crush, MOR, MOE, and an empirical
time multiplier. Reload via `HandicapCalculator.from_xlsx(...)` and
the species is available immediately.

### How do I add a new event type?

Event codes are currently SB and UH. Adding a new code
(e.g. HS = Hot Saw) requires:

1. Extending `config.EventCodes.VALID_EVENTS`.
2. Training a dedicated ML model (ML currently trains two models —
   SB and UH — because diameter scaling differs between events).
3. Setting a scaling exponent in `wood.py`.
4. Adding panel marks in `fallback.py`.

All four live in one commit; nothing else in the cascade assumes
only two event codes.

### How do I run the tests?

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Full detail on [Testing](Testing).

### How do I publish a new version?

1. Bump `__version__` in `strathmark/__init__.py` and
   `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Commit, tag, push.
4. Run the `publish.yml` workflow (manual `workflow_dispatch`).

The workflow builds the wheel and publishes to PyPI via trusted
publishing. Downstream managers then `pip install --upgrade
strathmark`.

## Source of truth

If a statement on this wiki disagrees with the code, the code wins.
Please open an issue — wiki drift is a known risk and the maintainer
welcomes the correction.
