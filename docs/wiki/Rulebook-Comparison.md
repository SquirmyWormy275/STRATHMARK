# Rulebook Comparison

STRATHMARK is deliberately rulebook-neutral — the engine enforces
mathematical invariants (floor 3, ceiling 183, gap logic) that are
compatible with every major woodchopping sanctioning body, while
staying out of the parts of handicapping that the rulebook leaves to
the officials (book-mark administration, penalty/award points, panel
overrides, drawing of heats, and so on).

This page cross-references the written rules of:

- **ALA** — American Lumberjack Association, *Rules & Regulations
  Effective 2022–2025*.
- **AAA** — Australian Axemen's Association, *Competition Rules and
  Code of Conduct*, revised August 2024.
- **QAA** — Queensland Axemen's Association, *By Laws* (QAA is an AAA
  member association, so it adopts the AAA competition rules and adds
  the handicap administration layer).

Wherever a rulebook says something STRATHMARK implements, the code
citation is linked. Wherever a rulebook leaves a decision to the
officials, the code stays out of it.

## 1. Handicap mark floor

- **ALA** — does not specify an explicit floor but runs the start-cadence
  on either "One, Two, Go" or STIHL-format "Three, Two, One, Go" (sect.
  13 of Sanctioning Shows). The front-marker effectively starts on Mark
  3 via the cadence count.
- **AAA** — "*The minimum handicap for a Competitor is 3 seconds*"
  (Rule 18).
- **QAA** — "*The minimum book mark in all handicap woodchopping and
  sawing events is 3*" (section 1, Handicap Book).

**STRATHMARK:** `HandicapCalculator.MARK_FLOOR = 3` (hard-coded, not
configurable). Every cascade level enforces it.

## 2. Mark ceiling / time limit

- **ALA** — does not publish a system-wide numeric ceiling; individual
  event rules (e.g. Hot Saw) specify warm-up and cut windows.
- **AAA** — Rule 91(b): "*a Competitor who has not completely severed
  his log within 3 minutes from the Start Time may be directed by the
  Judge to cease chopping or sawing*"; Rule 92 extends this to 12
  minutes for Tree Felling.
- **QAA** — no numeric ceiling in the By Laws (AAA rule applies).

**STRATHMARK:** `MARK_CEILING = 183` seconds — a deliberate choice of
180 s limit + 3 s floor, derived from the 3-minute AAA chopping limit.
Events may configure a tighter ceiling (for example, Hot Saw or
Stock Saw events capped at 60–90 s) via
`HandicapCalculator(event_ceiling=60)`.

## 3. Handicap calculation and authority

- **ALA** — handicapping is not codified into a formula; the Grand
  Finals use a points-based ranking (`10-7-5-3-1`), not start-time
  equalisation. Shows are free to handicap however they wish under
  Rule 3.
- **AAA** — "*Competitors in Events conducted in heats or divisions
  shall be handicapped at the discretion of the Committee or, if
  appointed, the Handicapper*" (Rule 12). "*Handicaps will be
  determined based on form, inherent ability and performances and such
  other information as may be deemed by the Committee, or Handicapper,
  to be relevant*" (Rule 13). Handicaps are calculated to the nearest
  second (Rule 17) and can be adjusted at any time (Rule 16).
- **QAA** — full handicap scale tables by bookmark and wood type
  (Underhand and Standing Block scale goes from book mark 3 → 43
  seconds across 300 mm hardwood through to softwood; separate tables
  for Veterans, Junior, Novice, Women's; separate sawing scale).

**STRATHMARK** implements the *predictor* of the time a competitor
would cut, which is the hardest and most subjective step the AAA
Handicapper has to do. The output of the cascade is a predicted time,
converted to a mark via the gap formula. Officials can still adjust
individual marks at the Committee's discretion — STRATHMARK accepts
`manual_overrides={name: time}` for exactly this reason.

## 4. Rounding

- **ALA** — silent on rounding.
- **AAA** — Rule 17: "*Handicaps are to be calculated to the nearest
  second.*" The AAA rounding-to-nearest-second rule is the literal
  definition of standard rounding, which in Python is banker's
  rounding (round-half-to-even) for floats.
- **QAA** — handicap scales are integer-second lookups; rounding is
  implicit in the table.

**STRATHMARK:** `round()` built-in, round-half-to-even. Matches AAA
Rule 17 and produces integer-second marks that can be looked up in the
QAA scales.

## 5. Penalties and awards (book-mark administration)

This is where the sanctioning bodies differ the most, and STRATHMARK
deliberately stays out of it — the engine does not write to handicap
books.

- **ALA** — does not publish a central penalty/award scale.
- **AAA** — Rule 13 allows the Handicapper to use any information
  relevant; no explicit penalty/award table in the rulebook.
- **QAA** — a full penalty/award system in By Laws section 3:
  - Open Underhand / Standing: 1 s penalty per $60 prize money
    (max 8 s); 1 s award per 3 unplaced performances.
  - Treefelling: 1 s per $20 (max 10 s); 6 s per unplaced.
  - Novice / Junior / Veterans / Women's: each with their own
    penalty tables.
  - Sawing: 1 s per $20 (max 5 s); combined back mark capped at 20
    (see By Laws 3.7 for the scaling method).

**STRATHMARK** does not track prize money or apply these penalties.
The downstream tournament manager (STRATHEX or the Missoula Pro-Am
Manager) is the right place to drive the QAA penalty machinery, and
STRATHMARK's role is to provide an *unbiased* predicted time for the
next event that the penalty layer can sit on top of.

If you need QAA-compliant book-mark administration, run the penalty
rules *after* the STRATHMARK prediction — use `HistoricalResult` to
update the stored mark, then feed the adjusted history back into
STRATHMARK on the next call.

## 6. Panel marks and fallbacks

- **ALA** — no explicit panel marks in the rulebook.
- **AAA** — no explicit panel marks.
- **QAA** — full panel mark table in section 2:
  - Underhand / Standing — 15
  - Treefelling — 70
  - Novice — 35
  - Junior — 15
  - Sawing — 15
  - Women's Underhand — 35
  - Veterans — set by handicapping committee

**STRATHMARK:** `fallback.get_panel_mark()` implements a
division-based default for competitors with zero historical data.
The defaults align with the QAA table for the typical AAA event set
and degrade gracefully — calling code never crashes on a
not-in-the-database competitor.

## 7. Novice / Junior / Veterans / Women's divisions

- **AAA** — defines Junior in two divisions (Div A 5–12, Div B 13–17;
  Rule 93), Novice (Rule 137), Women's (same rules apply unless
  gender-specific), and unofficial Master's events at 55+ (end of
  Event Rules).
- **QAA** — explicitly separates handicaps for each division and sets
  max handicaps per division:
  - Open — 43 s (300 mm UH/SB)
  - Novice — 60 s (275 UH / 250 SB)
  - Junior U18 / U13 — 60 s (250 UH/SB)
  - Veterans (60+) — 60 s (275 UH / 250 SB)
  - Women's — 60 s (275 UH)

**STRATHMARK:** `CompetitorRecord.division` accepts `'Open'`,
`'Novice'`, `'Junior'`, `'Veterans'`, `'Womens'`. The panel-mark
fallback uses the division string to choose the right default. The
cascade itself is division-neutral — a 14-year-old with 10 years of SB
history is modelled with the same ML/LLM/baseline tiers as an Open
competitor.

## 8. Wood species and diameter

- **ALA** — Sect. 5, Crosscut Sawing recommends specific wood sizes
  by gender and event (e.g. Men's Single 24 in max, Women's Single
  12–18 in). Chopping events suggest 12–14 in diameter.
- **AAA** — Rule 63 allows any diameter from 225 mm up with a tolerance
  of ±2 mm (±1 mm for Championship events). Rule 64 lists approved
  metric diameters: 250, 275, 300, 325, 350, 375, 400, 450, 500, 600,
  750 mm.
- **QAA** — handicap scales run from 225 mm (9 in) to 350 mm (14 in)
  for chopping, and 325 mm to 600 mm for sawing. Different scales for
  hardwood, medium wood, and softwood.

**STRATHMARK:** `WoodProfile.diameter_mm` accepts any float between
225 and 500 mm (`MIN_DIAMETER_MM = 225`, `MAX_DIAMETER_MM = 500`).
Species properties are loaded from the `Wood` sheet of
`woodchopping_clean.xlsx` — Janka hardness, specific gravity, shear
strength, MOR, MOE, and an empirical species time multiplier.
Diameter is handled by a power-law scaler with event-specific
exponents (SB 1.8, UH 2.1). See
[Wood and Diameter Scaling](Wood-and-Diameter-Scaling).

## 9. Wood quality

- **ALA** — not rated.
- **AAA** — not rated (Rule 61 requires all logs in one event to be
  from the same tree so variance within a single event is tightly
  controlled).
- **QAA** — not rated separately; hardness classes are hardwood, medium,
  softwood (three tables).

**STRATHMARK:** `WoodProfile.quality` is a 1-to-10 firmness rating
judged on the day (5 = average, no adjustment). It feeds:

- The LLM tier as a multiplier input in `[0.85, 1.15]`.
- The baseline tier as an effective-Janka adjustment:
  `effective_janka = base_janka × (1 + (quality − 5) × 0.1)`, range
  0.6× at quality 1 to 1.5× at quality 10.
- The ML tier indirectly through `species_mult` and the Janka
  features.

Quality is STRATHMARK's answer to the rulebook gap around real-world
variance — two rounds of "Pine at 300 mm" can cut very differently
depending on moisture, grain, and freshness. See the
`HANDICAP_SYSTEM_EXPLAINED.md` document in STRATHEX for the full
derivation (and the QAA interpolation design doc
`QAA_INTERPOLATION_IMPLEMENTATION.md` for the triangular membership
blending across QAA's three hardness tables).

## 10. Timing precision

- **ALA** — Sect. 12: "*Shows must use watches capable of timing to
  the 100th of a second (0.01).*"
- **AAA** — Rule 208: "*Timing of all Events must be to the nearest
  one hundredth of a second.*"
- **QAA** — adopts AAA Rule 208.

**STRATHMARK:** `HistoricalResult.time_seconds` is a `float`; the store
and the Supabase backend both preserve 0.01-second precision.
Predicted times are returned as floats and printed to two decimal
places in the start sheet.

## 11. Wedger / deputy / same tournament

- **ALA** — Crosscut Sawing Rule 2 allows one manager to oil and
  wedge. Hot Power Saw has a 2-minute warm-up.
- **AAA** — Rule 70 and 125 limit who can be in the arena. Tournament
  results from earlier rounds are not modelled in the rulebook.
- **QAA** — adopts AAA.

**STRATHMARK:** the engine is arena-agnostic but models
same-tournament rounds explicitly with graduated weighting (65 %, 80 %,
90 %, 97 %). This is a modelling choice, not a rulebook rule — when
the rulebook is silent, STRATHMARK picks the option that gives the
smallest residual error on the day.

## 12. Summary table

| Topic                  | ALA                  | AAA                 | QAA                  | STRATHMARK                                     |
|------------------------|----------------------|---------------------|----------------------|------------------------------------------------|
| Mark floor             | implicit 3 (cadence) | 3 s (Rule 18)       | 3 (book)             | 3 (hard-coded)                                 |
| Mark ceiling           | n/a                  | 3 min chop limit    | n/a                  | 183 s system (180 + 3); event-ceiling override |
| Rounding               | silent               | nearest second      | integer scales       | banker's rounding (round-half-to-even)         |
| Handicap authority     | Show Committee       | Handicapper         | QAA committee        | cascade + manual overrides                     |
| Penalty / award rules  | silent               | Handicapper's call  | explicit tables      | not implemented (downstream tooling)           |
| Panel marks            | silent               | silent              | 15 / 70 / 35 / …     | `fallback.get_panel_mark()` by division        |
| Timing precision       | 0.01 s               | 0.01 s              | AAA                  | `float` (preserves 0.01 s)                     |
| Diameter range         | 12–14 in suggested   | 225 mm and up       | 225–600 mm           | 225–500 mm                                     |
| Quality rating         | none                 | none                | hardness class       | 1–10 scale, judge-set                          |
| Same-tournament rounds | silent               | silent              | silent               | graduated 65 / 80 / 90 / 97 %                  |

## 13. What STRATHMARK does *not* do

- Enforce entry fees, membership fees, or affiliation fees.
- Track prize money, points trophies, or championship eligibility.
- Apply any sanctioning-body-specific penalty or award rule
  (QAA sections 3 and 4 are the downstream manager's job).
- Produce a draw or heat-assignment sheet (QAA By Laws section 12 is
  the manager's job).
- Judge slabs, clean cuts, or rule violations (Rules 85, 110, etc.
  belong to the officials).

STRATHMARK's single job is: *given a field and a wood, produce a fair
set of marks*. Everything else is the tournament manager's
responsibility.
