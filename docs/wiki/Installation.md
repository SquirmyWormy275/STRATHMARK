# Installation

Choose the setup that matches your job:

- **Running an event:** install [STRATHEX](https://github.com/SquirmyWormy275/STRATHEX/wiki/Quick-Start).
- **Using the prediction library:** install V2 below.
- **Running Linux V3:** follow the [competition setup guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md).
- **Trying the calibration changes:** see [Accuracy Preview](Accuracy-Preview).

## V2 library

Use Python 3.10–3.13. Python 3.13 is recommended. Create the environment on Linux:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

Or in Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then install:

```bash
python -m pip install "strathmark==2.0.1"
python -c "import strathmark; print(strathmark.__version__)"
```

Both 2.0.0 and 2.0.1 are published on PyPI. The 2.0.1 patch changes packaging
metadata and documentation; the V2 calculations are unchanged. STRATHEX pins its
reviewed 2.0.0 source separately.

Next, run the [Python example](Quick-Start).

## V2 API

Install the API dependencies in the same environment:

```bash
python -m pip install "strathmark[api]==2.0.1"
```

Then follow [REST API](REST-API) to choose an explicit database path and start the
server on loopback.
The public calculation endpoint does not require Ollama or a residual ML model.

## Current source build

The repository version is 3.0.0rc7. Use Python 3.13 and install from the repository
root into its own environment:

```bash
python -m pip install -e .
strathmark demo
```

This synthetic demo uses V2 calculations and opens no operator database. To work
on V3, follow [contributor setup](Testing) and install the exact release lock.
Installing the source build does not select V3 for an existing competition.
