# Architecture

STRATHMARK calculates predictions and marks. STRATHEX manages the competition,
judge workflow, roster names, official outcomes and exports.

## The calculation paths

| Path | How it runs |
| --- | --- |
| V2 library | `HandicapCalculator.calculate()` in the calling Python process. |
| V2 API | A stateless field request to `/calculate`; trusted ledger/shadow routes are separate. |
| Linux V3 | A separate Python 3.13 process with a trained model and persistent local authority. |
| V3 V7 service | A separately composed authenticated service with a pinned contract and source identity. |

V2 and V3 have separate code and receipt formats. A competition keeps one selected
engine. New results do not rewrite old V2 receipts or turn them into V3 receipts.

## Inside Linux V3

Formula and ML forecast the same admitted evidence. Their distributions remain
available for comparison. V3 combines them, evaluates disagreement, calculates
complete-field marks and retains the receipts used for judge review.

Seeding forecasts contain cutting times, not marks. Proposed field marks need the
actual roster and stands. Approval and issue are separate actions. The local command
log preserves issue, outcomes, settlement and exact retry across restarts.

One round uses frozen evidence. Later rounds can use valid completed results and
updated capability and weighting. [V3](Prediction-Engine-V3) explains the workflow;
[storage](Persistence-and-Database) explains recovery.

## Wider service design

The V7 design also includes an independently evaluated LLM council, rolling forecast
preparation and model-factory roles. Those components and Windows CNG qualification
have their own installation requirements; they are not all active in Linux V3.

See the [module map and detailed architecture](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md)
when changing the implementation.
