# How woodchopping handicaps work

A handicap race uses staggered starts to give competitors of different expected
cutting speeds a meaningful chance of winning. The faster expected competitor
waits longer before starting. In a championship or scratch race, everyone starts
together and raw cutting speed decides the result.

**Mandatory domain reading.** This is the shared domain reference for STRATHMARK
contributors. Read it before
changing predictions, marks, start sheets, simulations or results. Association
rulebooks govern their competitions; the explanations here define handicap meaning
within this project.

## Start marks

The starter calls increasing numbers, usually seconds. A competitor begins when
the count reaches their **mark**:

- Smaller mark: earlier start.
- Larger mark: later start.
- **Frontmarker:** usually the slowest expected competitor, with the smallest mark.
- **Backmarker:** usually the fastest expected competitor, with a larger mark.

A smaller mark starts earlier; a larger mark starts later. The handicap changes
the start time. Everyone still cuts the assigned wood.

## Two clocks

**Raw cutting time** runs from the first legal strike or saw movement to completion.
**Completion on the starter's count** includes the wait before starting:

```text
start mark + raw cutting time = completion on the common count
18         + 27 seconds       = 45
```

A mark of 18 is not an 18-second prediction or a bonus deducted after the race.

## A worked example

For a simple gap handicap:

```text
mark = frontmark + slowest predicted time - competitor's predicted time
```

With a frontmark of 3:

| Competitor | Predicted cutting time | Start mark | Expected completion |
| --- | ---: | ---: | ---: |
| Frontmarker | 60 s | 3 | 63 |
| Middlemarker | 45 s | 18 | 63 |
| Backmarker | 30 s | 33 | 63 |

If everyone cuts exactly as predicted, they finish together on count 63. The
backmarker starts 30 seconds after the frontmarker because they are expected to
finish the cut 30 seconds faster.

This example explains the geometry. The versioned engines use their own prediction
and optimization methods; it is not a rule requiring every system to use simple gaps.

## Rebasing a field

Adding or subtracting the same constant from every mark preserves the race.
If the example starts at 15 instead of 3, add 12 to every mark:

| Competitor | Original mark | Rebased mark |
| --- | ---: | ---: |
| Frontmarker | 3 | 15 |
| Middlemarker | 18 | 30 |
| Backmarker | 33 | 45 |

Everyone's expected completion moves from 63 to 75. No competitor gains an advantage.
Rebasing changes the origin of the count, not the estimated ability.

A displayed mark is therefore **relative to its field**. Mark 12 in one heat and
Mark 12 in another heat do not prove equal ability when the fields used different
bases. Form a final from the underlying ability estimates, then calculate and rebase
the complete new field. Do not copy the displayed heat marks into it.

## Book marks and conversions

A traditional book or reference mark records ability over time. It may be converted
for an event, timber class or log diameter before a field's displayed marks are set:

```text
recorded ability -> event/log conversion -> common field offset -> displayed mark
```

Book marks, conversion scales, panel marks, limits and adjustment rules vary between
associations. A book mark is not necessarily the number announced in every heat.

Standing Block, Underhand, sawing and other disciplines are different tasks. Diameter
and timber properties matter too. Put performances into a common event and material
context before comparing ability. A 250 mm Standing Block time and a 325 mm Underhand
time are not interchangeable just because both are measured in seconds.

## Heats and finals

Advancement decides who reaches the next round. Handicapping decides when those
competitors start. These are separate decisions.

Later fields may combine competitors from independently rebased heats. Reconstruct
their handicap on one common basis. How an engine admits new results between rounds
is a versioned software decision; see [V2](Prediction-Engine-V2) and [V3](Prediction-Engine-V3).

## Fairness and real performances

A fair handicap compensates for the best available estimate of ability; it does not
claim everyone has equal skill. A deterministic estimate aims to align expected
completions. A probabilistic estimate also considers comparable chances of winning.

Actual cuts vary. Technique, break behavior, wood variation and integer-second marks
mean that a close finish or dead heat cannot be guaranteed. A close finish created
using evidence that was unavailable before the race is not a valid pre-race handicap.
See [fairness assessment](Fairness-Assessment).

## Honest effort

Deliberately concealing ability through a slow performance—often called foxing or
sandbagging—undermines the evidence used for future handicaps. A slow result alone
cannot establish intent: an ordinary poor cut can look similar.

Faster and slower valid performances both provide ability evidence. A fast cut matters
even if the competitor is eliminated or does not win. Placing alone does not describe
how far someone exceeded their expected performance.

Traditional books record results, winnings, recent form and adjustments. Associations
may use success penalties, minimum-effort rules or handicapper discretion. Their
specific prize thresholds, X systems and penalties are association rules, not universal
requirements of a woodchopping handicap.

## Officials and software

The handicapper constructs or reviews the start sheet. The starter controls releases.
Judges decide legal completion and placings. The governing body controls eligibility,
protests, penalties and event rules.

A predicted time, proposed sheet, issued marks and official result are different
records. Calculation software assists officials; a number alone does not authorize
a competition, prove dishonesty or change an issued race's winner.

## Background

This explanation was checked against the Australian Axemen's Association's
*Competition Rules and Code of Conduct* (August 2024), the supplied Queensland
Axemen's Association bylaws and conversion tables, and these race examples:

- [Underhand handicap](https://youtu.be/dx8OyDR5fg0)
- [Standing Block handicap](https://www.youtube.com/watch?v=oEs4m5ycsVA)
- [Example 3](https://www.youtube.com/watch?v=QiB75iSFp38)
- [Example 4](https://www.youtube.com/watch?v=4-CiWhcOGKw)
- [Example 5](https://www.youtube.com/watch?v=fr9x5AUedu0)

Consult the applicable rulebook for the competition's actual procedures.
