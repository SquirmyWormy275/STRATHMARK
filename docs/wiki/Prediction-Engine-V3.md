# Prediction Engine V3

V3 adds independent forecasts and learning between rounds. Judges choose it for a
competition in STRATHEX; it does not automatically replace V2.

## Runtime choices

| Runtime | Current use |
| --- | --- |
| Linux competition, rc7 | Full local workflow with Formula and trained CatBoost ML. |
| Older Linux numeric preview | Seeding times and proposed marks, without issue or settlement. |
| Windows V7 service | Development and rehearsal; production qualification is incomplete. |

The LLM council is unavailable in the Linux competition runtime. Every field therefore
requires deliberate degraded or individual review. Five or more marks of disagreement
between the Formula/ML counterfactual sheets requires individual review.

## How a competition progresses

1. Freeze the evidence available for the round.
2. Predict raw cutting times for seeding before fields exist.
3. Create the actual heats and stands.
4. Calculate and review marks for each complete field.
5. Approve the field, then confirm issue separately.
6. Record all issued competitors' outcomes and official placings.
7. Settle every field in the event's round before advancing.

All heats in the same round use the same frozen evidence. Valid completed results
can affect a later round through capability updates and earned Formula/ML weights.
Penalty and nonfinish rows do not become raw-time training evidence. Championship
fields use fixed Mark 3.

## What is preserved?

Original component forecasts, the selected source/model/key, issued marks and result
revisions are retained. Later fields are reconstructed and rebased as complete fields.
Unexpected faster and slower performances can change future capability, but V3 does
not assign motives or alter an issued race's winner.

## Set it up

Use the [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md)
for the trained model, Python 3.13 environment, persistent authority and backups.

Developers working on the authenticated V7 service should use the
[detailed V3 specification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V3.md).
That specification covers the wider Formula/ML/three-member-council design, frozen
18-route contract and Windows qualification requirements.
