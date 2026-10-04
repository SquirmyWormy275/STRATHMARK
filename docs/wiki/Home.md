# STRATHMARK

STRATHMARK predicts cutting times and calculates handicap start marks. Judges
operate it through [STRATHEX](https://github.com/SquirmyWormy275/STRATHEX/wiki).
Developers can also use its Python library and APIs.

## Find what you need

| I want to… | Start here |
| --- | --- |
| Install the library | [Installation](Installation) |
| Calculate a sample field | [Quick start](Quick-Start) |
| Run a competition | [STRATHEX](STRATHEX-Consumer) |
| Choose V2 or V3 | [Engine selection](Competition-Engine-Selection) |
| Understand handicap marks | [How handicaps work](Handicap-Mark-Math) |
| Try the accuracy changes | [Accuracy Preview](Accuracy-Preview) |
| Back up or restore V3 | [Deployment and recovery](Deployment) |
| Find an answer quickly | [FAQ](FAQ) |

## What works today?

**V2** is the established engine for Linux and Windows. **Linux V3**, using
STRATHMARK 3.0.0rc7 with STRATHEX 7.4.2, supports a full local competition workflow.
It requires a configured trained model, persistent signing key and independent
backups. It uses Formula and ML; its LLM council is unavailable.

The Windows V7 service is for development and rehearsal; production qualification
is incomplete. Older numeric-preview competitions keep their original limitations.
See [V3](Prediction-Engine-V3) for the differences.

The accuracy changes are runnable in a separate preview program. They have not
replaced the competition model.

## For developers

[Architecture](Architecture-Overview), [REST API](REST-API) and [testing](Testing)
cover integration. The detailed versioned specifications remain in the
[repository](https://github.com/SquirmyWormy275/STRATHMARK/tree/main/docs).
