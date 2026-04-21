# LLM Integration (Ollama and Gemini)

The LLM tier of the cascade is optional — STRATHMARK's core handicap
calculation is fully defined without it, and the cascade silently
skips past the LLM when nothing is reachable. When the tier *is*
available, it adds a quality-adjustment step that accounts for
subtleties the baseline formulas cannot capture (recent form
trajectory, unusual wood grain, quality-rating context).

## Design goal

**The LLM does not predict a time from scratch.** It consumes the
weighted baseline and emits a multiplier in `[0.85, 1.15]`. The
cascade enforces the bounds in `predictor.py`:

```
LLMConfig.QUALITY_MULTIPLIER_MIN = 0.85
LLMConfig.QUALITY_MULTIPLIER_MAX = 1.15
```

Anything outside those bounds is rejected and the cascade falls
through. This prevents a hallucinating model from producing absurd
marks.

## Two-tier architecture

```
1. Ollama (local, event laptop)     - primary
2. Gemini (cloud, via API key)      - fallback when Ollama is unreachable
3. Cascade drops past the LLM tier  - when neither responds
```

### Why two tiers?

- **Ollama** is fast, cheap, offline-capable, and keeps all data on
  the event laptop. It is the preferred tier for race day.
- **Gemini** is the safety net for venues where the event laptop has
  no GPU or Ollama crashes. Only invoked when
  `GEMINI_API_KEY` is set *and* Ollama returns `None`.
- **No LLM at all** is the safety net's safety net — the cascade is
  designed to produce correct marks without any LLM.

## Ollama setup

### Target model

`qwen3.5:9b` (released Feb 2026). Quantisation Q4\_K\_M, disk size
~6.6 GB, fits in 8 GB VRAM on the target event-laptop GPU
(RTX 4070 Laptop). Pull:

```bash
ollama pull qwen3.5:9b
ollama serve
```

The choice is not arbitrary — `qwen3.5:9b` is the smallest model in
testing that reliably returned structured JSON multipliers without
hallucinating across the 1–10 quality range.

### Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_HOST` | `http://localhost:11434` | host-only override (preferred) |
| `STRATHMARK_OLLAMA_URL` | `http://localhost:11434/api/generate` | legacy full-URL override |
| `STRATHMARK_OLLAMA_CONNECT_TIMEOUT` | `3` | TCP connect timeout, seconds |
| `STRATHMARK_OLLAMA_READ_TIMEOUT` | `15` | HTTP read timeout, seconds |
| `STRATHMARK_OLLAMA_MAX_RETRIES` | `0` | retries after first failure |

The timeouts are aggressive on purpose — race day prefers a fast
fallthrough over a minute-long hang on a dead laptop.

### Kill switch

Set `OLLAMA_HOST=""` or `OLLAMA_HOST=disabled` and the cascade skips
the Ollama tier entirely without touching code. The legacy
`STRATHMARK_OLLAMA_URL` always wins if set.

## Gemini cloud fallback

### Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_API_KEY` | unset | Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-2.0-flash-lite` | model id |
| `GEMINI_CONNECT_TIMEOUT` | `5` | TCP connect, seconds |
| `GEMINI_READ_TIMEOUT` | `15` | HTTP read, seconds |

`gemini-2.0-flash-lite` is the cheapest Google tier and is fast
enough for the cascade. Invoked only on Ollama failure.

## The LLM prompts

### Quality adjustment prompt

```
You are an expert woodchopping handicapper.

COMPETITOR: Alice Smith
Historical SB times: [28.4, 27.9, 29.1, 28.3, 28.7]
Average: 28.48 s
Weighted baseline: 28.52 s

TODAY'S WOOD:
  Species: Pine
  Janka hardness: 1690 lbf
  Specific gravity: 0.34
  Diameter: 300 mm
  Quality: 7 / 10  (firm — tighter grain than average)

Return a JSON object:
  { "multiplier": 0.85..1.15, "reasoning": "..." }
Multiplier applies to the weighted baseline.
1.0 = no adjustment. >1 = expected slower. <1 = expected faster.
```

Response:

```json
{"multiplier": 1.04, "reasoning": "Quality 7 firm Pine tightens grain by ~4%; Alice's recent trend is steady so no trend adjustment."}
```

The multiplier is clamped and applied. If parsing fails the tier
returns `None`.

### Other LLM roles

`strathmark/llm_roles.py` contains roles beyond quality adjustment:

- **Competitor profile** — short descriptive profile for announcer
  cards, from last 10 results.
- **Race commentary** — two- or three-paragraph narrative given the
  mark sheet and predicted finish times.
- **Anomaly detection** — flag competitors whose last result is far
  outside their historical distribution. Uses a structured JSON
  schema.
- **Championship race analysis** — six-section final-round
  commentary (see [Fairness Assessment](Fairness-Assessment)).

All roles accept a `format_schema` argument that instructs Ollama to
return a structured JSON shape and sets `temperature=0.0` to make the
response deterministic. Roles without a schema use sensible defaults.

## Connection management

`strathmark/llm.py` wraps both providers with a single entry point:

```python
from strathmark import call_ollama, check_ollama_connection

ok = check_ollama_connection()  # cached for 60 seconds
if ok:
    response = call_ollama(prompt, model="qwen3.5:9b")
```

The 60-second cache prevents the cascade from repeatedly probing a
dead Ollama during a heat.

## Testing

- `tests/test_llm.py` — connection caching, timeout handling,
  fallback path, URL resolution.
- `tests/test_llm_roles.py` and `tests/test_llm_roles_extended.py` —
  every role with mocked responses; prompt assembly; schema
  enforcement.

Both suites use `pytest.importorskip("ollama")` (and
`"google.generativeai"` where relevant) so CI without the `llm`
extra still passes.

## Race-day failure modes

| Failure | Behaviour |
|---------|-----------|
| Ollama not running | cascade skips LLM; falls through to ML → baseline → panel |
| Ollama times out | single retry (zero by default); fails fast to Gemini or through |
| Gemini key unset | skips cloud tier; falls through |
| Malformed JSON response | tier returns `None`; cascade falls through |
| Multiplier out of bounds | tier returns `None`; cascade falls through |

The cascade is designed so that the worst LLM failure degrades the
prediction to the baseline tier — never to garbage, never to a crash.
