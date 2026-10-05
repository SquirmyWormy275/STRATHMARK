# Frequently asked questions

## Can I use V3 to run a Linux competition?

Yes, with the configured Linux competition runtime: STRATHMARK 3.0.0rc7 and
STRATHEX 7.4.2. It supports review, separate issue confirmation, official outcomes,
corrections, recovery and later rounds. Start with
[STRATHEX setup](https://github.com/SquirmyWormy275/STRATHEX/wiki/Quick-Start).
Windows V7 production qualification remains incomplete.

## Is the accuracy improvement active?

Only in the separate [Accuracy Preview](Accuracy-Preview). The competition model
is unchanged. The 2.81% gain on examined history misses the 5% target and has no
independent future validation.

## Where do I choose V2 or V3?

In STRATHEX at event or tournament creation. The choice locks at the first numeric
operation. A tournament's events and rounds inherit it. See [engine selection](Competition-Engine-Selection).

## Why do faster competitors get larger marks?

They wait longer before starting. A smaller mark starts earlier.
[How handicaps work](Handicap-Mark-Math) gives a worked example.

## Can I copy a competitor's heat mark into the final?

No. Marks depend on the complete field and its base count. Recalculate the advancing
field from ability evidence, then rebase it.

## Does V3 change the winner after a race?

No. Issued marks stay fixed. Judges authorize official outcomes. New performances
can affect a later round; they do not rewrite the issued race. Official result
corrections use signed revisions and preserve earlier records.

## Does V3 detect cheating?

It can flag surprising performances and adjust future capability estimates.
It cannot infer intent or decide that a competitor was dishonest.

## Do I need Ollama or a cloud connection?

V2 numeric calculations do not need either. Linux V3 uses Formula and trained ML;
its council is unavailable, so fields require explicit degraded or individual review.
The [LLM page](LLM-Integration) explains the separate council work.

## What happens when the selected engine fails?

The affected work stops. Restore the same engine and artifacts; the competition
does not switch to the other engine. For an ambiguous V3 command, retry its exact
saved identity rather than submitting a changed request.

## Does passing a rehearsal make Windows V3 production-ready?

No. A rehearsal verifies a particular build and environment using development keys.
Windows production requires its own installation, non-exportable CNG signing identity
and qualified evidence. See [Deployment](Deployment).
