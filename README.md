# STRATHMARK

STRATHMARK predicts raw woodchopping times and calculates handicap start marks.
[STRATHEX](https://github.com/SquirmyWormy275/STRATHEX) is the application judges use
to set up events, review marks and enter results. Use STRATHMARK directly when you
need the Python library or an API for another application.

## Which version should I use?

| Version or tool | What you can use it for |
| --- | --- |
| **V2** | The established prediction engine. Runs locally on Linux and Windows. |
| **V3 Linux competition runtime** | Formula and trained ML predictions, reviewed and issued fields, results, recovery and learning between rounds. Requires a configured local installation. |
| **V3 Windows V7 service** | Development and rehearsal. Windows production qualification is incomplete. |
| **Accuracy Preview** | Compare a frozen calibration candidate with the baseline. It does not change competition predictions. |

The current Linux pair is **STRATHMARK 3.0.0rc7 + STRATHEX 7.4.2**. Judges choose
V2 or V3 when creating each event or tournament. A tournament's events and rounds
keep that choice. See [engine selection](docs/wiki/Competition-Engine-Selection.md).

## Run a competition

Start with [STRATHEX's quick start](https://github.com/SquirmyWormy275/STRATHEX/wiki/Quick-Start).
For Linux V3, follow the [competition setup guide](docs/V3_LINUX_COMPETITION.md).
It covers the separate Python 3.13 environment, trained model, signing key, backup
location and judge workflow. Private models and competition data are supplied by
the operator; they are not included in the public package.

The Linux runtime uses Formula and ML. Its LLM council is unavailable, so judges
must explicitly review affected fields. The Windows V7 service has different
installation and qualification requirements.

## Install the V2 library

Use Python 3.10–3.13; Python 3.13 is recommended. In your virtual environment:

```bash
python -m pip install "strathmark==2.0.1"
python -c "import strathmark; print(strathmark.__version__)"
```

Version 2.0.1 preserves the 2.0.0 numeric engine. STRATHEX keeps its reviewed 2.0.0
source dependency. Follow the [Python example](docs/wiki/Quick-Start.md) to calculate
a small synthetic field, or [Installation](docs/wiki/Installation.md) for API setup.

## Try the source-build demo

From this repository, on Linux:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
strathmark demo
```

Or on Windows PowerShell, with Python 3.13 installed:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
strathmark demo
```

The demo calculates a synthetic V2 field and opens no competition database. It is
included in the current source build; it is not a command in the older V2 PyPI release.

## Understand the marks

A smaller mark starts earlier. A faster expected competitor gets a larger mark and
waits longer. Marks are calculated for the complete field, so a heat's displayed
marks cannot simply be copied into a final with different competitors.

[How handicaps work](docs/wiki/Handicap-Mark-Math.md) explains cutting time, start
counts, rebasing and the role of officials with worked examples.

## Accuracy Preview

The [separate preview program](tools/accuracy-preview/README.md) is implemented.
Its frozen candidate reduced average error from **23.12 to 22.47 seconds** on
278 previously examined historical results. That 2.81% improvement misses the 5%
qualification target and has no independent future validation. The changes are
available in the preview; they are **not enabled in competition predictions**.

## More help

- [Wiki](https://github.com/SquirmyWormy275/STRATHMARK/wiki): installation, examples and explanations.
- [V2](docs/PREDICTION_ENGINE_V2.md) and [V3](docs/PREDICTION_ENGINE_V3.md): detailed engine specifications.
- [Onboarding](ONBOARDING.md): contributor instructions and test isolation.
- [Deployment](docs/DEPLOYMENT.md): service qualification and recovery requirements.
- [Changelog](CHANGELOG.md): release history.

[Apache 2.0 license](LICENSE).
