# Woodchopping Handicap Foundations

> **Mandatory domain reading.** Every human contributor, coding agent, model, and
> downstream system must read this page before interpreting or changing handicap
> predictions, marks, start sheets, simulations, results, or related documentation.

This page is STRATHMARK's source of truth for **what a woodchopping handicap is, what
it is intended to accomplish, and how a handicap race works**. It describes domain
semantics. It is not a product roadmap, a proposed rule change, or a substitute for
the rules and officials governing a particular competition.

Association manuals use different tables, limits, penalties, awards, divisions, and
procedures. Those details are useful context, but they are not universal definitions of
a handicap. When another document conflicts with the domain explanation on this page,
use this page for handicap meaning and the applicable rulebook for competition
authority.

## The essential idea

A woodchopping handicap is a **staggered-start race**. Competitors do not all begin
cutting together. The starter calls increasing numbers, usually seconds, and each
competitor begins when the count reaches that competitor's assigned **mark**.

- A competitor with a **smaller mark starts earlier**.
- A competitor with a **larger mark starts later**.
- The fastest expected competitor is normally the **backmarker** and has one of the
  largest marks.
- The slowest expected competitor is normally the **frontmarker** and has one of the
  smallest marks.

The handicap does not change how much wood a competitor must cut. It changes only the
time at which the competitor is released to begin. The intended effect is to offset
expected differences in cutting speed so that competitors of different abilities have
a meaningful chance to win the same race.

In a championship or other scratch event, everyone starts together and raw cutting
speed decides the result. In a handicap event, the result depends on how each
competitor performs relative to the ability represented by the assigned mark.

## Two clocks must not be confused

A handicap race contains two related but different time coordinates.

1. **Raw elapsed cutting time** is the time from the competitor's first legal strike
   or saw movement until completion. It describes the performance itself.
2. **Race completion on the starter's count** is the competitor's start mark plus the
   raw elapsed cutting time. It determines where that performance finishes relative to
   the other staggered starts.

If a competitor starts on Mark 18 and takes 27 seconds to sever the log, that
competitor finishes at approximately 45 on the common race count:

```text
18-second start mark + 27-second cutting time = 45-second race completion
```

Calling the start mark a cutting time, or comparing cutting times without their start
marks, reverses the meaning of the race.

## The basic handicap geometry

Let:

- `T_i` be competitor `i`'s expected raw cutting time;
- `T_slowest` be the largest expected raw cutting time in the field; and
- `B` be the chosen frontmark or displayed base count.

The simple gap handicap is:

```text
mark_i = B + T_slowest - T_i
expected race completion_i = mark_i + T_i = B + T_slowest
```

The subtraction is intentionally `slowest minus competitor`. A faster competitor has
a smaller expected cutting time, so the faster competitor receives a larger mark and
waits longer to start.

### Worked example

Assume the displayed frontmark is 3:

| Competitor | Expected cutting time | Start mark | Expected completion on count |
| --- | ---: | ---: | ---: |
| Frontmarker | 60 s | 3 | 63 |
| Middlemarker | 45 s | 18 | 63 |
| Backmarker | 30 s | 33 | 63 |

The backmarker is not being given 33 seconds of help. The backmarker must **wait until
33** while the frontmarker begins at 3. The handicap is the separation between their
starts.

If the starter instead begins this race at 15, every mark can be translated by the
same 12 seconds:

| Competitor | Mark from base 3 | Mark from base 15 |
| --- | ---: | ---: |
| Frontmarker | 3 | 15 |
| Middlemarker | 18 | 30 |
| Backmarker | 33 | 45 |

The start separations remain 15 and 30 seconds. Every expected completion moves from
63 to 75. No competitor gains or loses an advantage. The announcer has changed the
origin of the count, not the handicap relationship.

## Rebasing is a common translation, not a new handicap

Adding or subtracting the same constant from every mark preserves the race:

```text
new_mark_i = old_mark_i + K
```

For any two competitors `i` and `j`:

```text
new_mark_i - new_mark_j = old_mark_i - old_mark_j
```

This is why a field may be displayed with its frontmarker at Mark 3 even when a
different starting number was used elsewhere. A start at 3, 10, or 15 can describe the
same handicap if every competitor is shifted equally.

Rebasing must never be mistaken for evidence that a competitor's ability changed. The
following are different facts:

- **Relative handicap:** the start separation between competitors.
- **Displayed field mark:** the number called for a competitor in this particular
  field after applying a common offset.
- **Ability evidence:** the historical performances used to estimate how quickly the
  competitor can cut.

A displayed mark without its field, base, conversion, and underlying performance
context is not a portable measurement of ability.

## Book marks, reference marks, and displayed marks

Traditional systems often maintain a **book mark** or another persistent reference
mark. Performances, placings, prize money, and mark changes may be entered in a
competitor's handicap book. Conversion tables then translate that reference mark for
different events, timber classes, or log sizes.

The book mark is not necessarily the number announced in every heat. It is a durable
reference from which an event-specific mark can be derived. Different associations use
different reference events, panel marks, maximums, conversion tables, and adjustment
rules.

The important domain distinction is universal even when the terminology is not:

```text
recorded ability/reference
        -> event and log conversion
        -> field-wide common offset
        -> displayed start mark
```

Agents must not compare displayed marks from different independently rebased fields as
though they share the same origin. Compare the underlying performance or reference
representation, then construct the new field on one common basis.

## Heats, finals, and multi-round competitions

A competition may progress through heats, quarterfinals, semifinals, divisional
finals, and a grand final. Competitors arriving in a later round may have come from
fields with different frontmarkers and different displayed offsets.

The local mark from an earlier heat cannot safely be copied into a later mixed field
unless its original basis is also preserved. For example, Mark 12 in one heat and Mark
12 in another heat do not prove equal ability if the two heats were rebased from
different frontmarkers.

The mathematically meaningful information is the relative ability or expected cutting
time represented before the local field offset. A later field is formed by placing all
of its competitors on one shared basis and then choosing one common displayed base.

Advancement and handicapping are also separate concepts:

- **Advancement rules** decide who reaches the next round.
- **Handicap marks** decide when those competitors start.

An association may use placings, handicap rankings, previous results, or a seeded draw
to construct rounds. Those administrative choices do not change the definition of the
start mark.

## Wood, event, and dimension conversions

Raw times and marks cannot be compared blindly across materially different tasks. A
larger log normally takes longer to sever. Standing Block and Underhand are distinct
disciplines. Timber species and physical properties can change expected cutting time.
Sawing, Tree Felling, combination events, and relay formats have their own task
structures.

Traditional manuals address this with event-specific book marks and conversion tables
for log diameter and broad timber classes such as hardwood, medium wood, or softwood.
These tables are administrative approximations that make a reference mark usable in
another sanctioned configuration.

The enduring principle is:

> A handicap comparison is meaningful only after the performances have been placed in
> a common event and material context.

This does not mean every observed difference in wood can be measured perfectly. It
means an agent must not treat a 250 mm Standing Block result and a 325 mm Underhand
result as interchangeable merely because both are recorded in seconds.

## What a fair handicap means

A fair handicap does **not** mean that every competitor is equally skilled. It means
the staggered starts compensate for the best available estimate of their different
abilities.

There are two closely related fairness descriptions:

- **Deterministic description:** if every competitor exactly matches the expected raw
  time, their completions align on the common count.
- **Probabilistic description:** because real performances vary, the assigned starts
  should give competitors defensibly comparable chances of winning rather than
  systematically favoring a raw-speed class.

These descriptions are not promises of a dead heat. Woodchopping contains genuine
variation: strike placement, break behavior, technique execution, and other race
effects change actual times. Integer-second marks add further granularity. Even an
excellent handicap will produce a distribution of winners and finish spreads.

The visible close finish is therefore an intended consequence of good handicapping,
not the complete definition of fairness. A sheet that produces a close finish only by
using information unavailable before the race is not a legitimate pre-race handicap.

## Why honest effort matters

Handicapping estimates ability from observed performance. Its integrity depends on
performances being genuine competitive efforts.

If a competitor deliberately performs below demonstrated capability, the slow result
can make the competitor appear entitled to an earlier start later. This practice is
commonly described as **foxing**, **sandbagging**, or concealing form. It attacks the
evidence on which the handicapper relies; it is not merely ordinary race tactics.

A slow result by itself cannot prove motive. The same time might reflect ordinary
variation, a poor cut, or deliberate concealment. Traditional rulebooks therefore use
a mixture of sportsmanship duties, performance books, disclosure requirements,
minimum-effort or completion rules, success penalties, unplaced-performance records,
and handicapper discretion.

These mechanisms differ by association. Their common purpose is to keep the recorded
handicap aligned with demonstrated capability and to prevent competitors from gaining
an unfair future advantage by misrepresenting that capability.

The inverse is equally important: a materially faster performance is evidence about
capability even if it occurred in a preliminary round or did not win the event. A
placing alone does not express the magnitude of that information. Winning by a
fraction of a second and outperforming the assigned expectation by 15 seconds are not
the same evidentiary event, even if a traditional prize table assigns the same
placing-based penalty.

## What traditional handicap books accomplish

The reviewed association manuals illustrate several recurring functions of traditional
handicapping:

- record performances and changes across competitions;
- require competitors to disclose recent form;
- establish a minimum mark and event-specific maximums;
- use panel or reference marks when individual evidence is limited;
- convert marks between approved log sizes and timber classes;
- move successful competitors toward a later start;
- move repeatedly unplaced competitors toward an earlier start;
- rank or distribute fields using marks and recorded non-placings;
- authorize handicappers to consider form, inherent ability, and other relevant
  evidence;
- allow officials to correct marks when the recorded representation is no longer
  credible.

These are examples of how associations pursue the handicap's purpose. Exact prize
thresholds, `X` systems, panel marks, caps, tables, and committee procedures are league
rules, not the universal meaning of a handicap.

## What a handicap is not

A woodchopping handicap is not:

- a reduction in the amount of wood to be cut;
- a time bonus subtracted from the official result after the race;
- a declaration that slower competitors are better competitors;
- a guarantee that everybody finishes together;
- permission for a competitor to begin before the assigned mark;
- a championship or scratch result, where all competitors start together;
- a complete measurement of ability when separated from event and wood context;
- a portable number when only a locally rebased displayed mark is retained;
- a substitute for judges, starters, handicappers, or governing competition rules;
- proof that an unexpectedly slow performance was deliberate;
- proof that an unexpectedly fast performance was dishonest.

## Officials and system boundaries

The handicapper estimates ability and constructs or approves the start sheet. The
starter controls the release count. Judges determine legal completion, placings, and
rule violations. The governing body decides eligibility, protests, penalties, event
formats, and official authority.

A calculation engine can assist with prediction, comparison, uncertainty, mark
construction, and auditability. It does not acquire competition authority merely by
producing a number. A model output, a reviewed handicap sheet, the announced marks, and
the official result are distinct records.

## Product implementations belong in versioned engine documentation

This page deliberately stops at domain meaning. Formulae, statistical models, model
ensembles, evidence cutoffs, uncertainty calculations, optimization objectives,
operational limits, release states, and API schemas are product decisions. They can
change without changing what a woodchopping handicap is.

Use the versioned prediction-engine documentation to learn how a particular STRATHMARK
release estimates ability and constructs a legal sheet. Use this page to check that the
implementation still respects the underlying race: faster expected competitors wait
longer, every competitor cuts the assigned task, a common rebase preserves relative
starts, and the issued sheet remains distinct from the official result.

## Reasoning checklist for agents and systems

Before making any statement about a handicap, verify all of the following:

1. Are you discussing raw cutting time, the announced start mark, or completion on the
   common race count?
2. Have you preserved the fact that a smaller mark starts earlier and a larger mark
   starts later?
3. Are all competitors being compared in the same event, log, and material context?
4. Is a mark a durable reference or merely a displayed number after a local field
   rebase?
5. If fields are being combined, have you removed their independent offsets before
   comparing competitors?
6. Are you separating handicap construction from advancement, judging, penalties, and
   official result authority?
7. Are you describing expected fairness rather than promising an equal finish?
8. Are association-specific tables being treated as examples rather than universal
   requirements?
9. Are you avoiding an inference of competitor intent from one result alone?
10. Are you preserving the handicap's central purpose: offset expected ability
    differences through staggered starts so the race is genuinely competitive?

If an explanation fails any of these checks, it does not yet demonstrate an adequate
understanding of woodchopping handicapping.

## Context reviewed for this source of truth

This explanation was reconciled against:

- Australian Axemen's Association, *Competition Rules and Code of Conduct for the
  Conduct of Woodchopping and Sawing Contests*, revised August 2024;
- Queensland Axemen's Association, *By Laws* and handicap/conversion tables supplied as
  `QAA.pdf`;
- the project's existing calculation, rulebook-boundary, architecture, and prediction
  documentation;
- the following real handicap-race examples supplied during the domain review:
  [Underhand handicap](https://youtu.be/dx8OyDR5fg0),
  [Standing Block handicap](https://www.youtube.com/watch?v=oEs4m5ycsVA),
  [example 3](https://www.youtube.com/watch?v=QiB75iSFp38),
  [example 4](https://www.youtube.com/watch?v=4-CiWhcOGKw), and
  [example 5](https://www.youtube.com/watch?v=fr9x5AUedu0).

The association materials and videos are contextual evidence. They do not override the
rules of the competition being conducted, and their league-specific mechanisms are not
automatically STRATHMARK requirements.
