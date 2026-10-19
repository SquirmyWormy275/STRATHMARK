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
python -m pip install "strathmark==3.0.0rc14"
python -c "import strathmark; print(strathmark.__version__)"
```

The maintained 3.0.0rc14 package retains the V2 `2.0.0` calculation contract.
STRATHEX 7.4.6 pins the exact reviewed security source for new installations.
The immutable 2.0.0/2.0.1 artifacts remain available for retained installations;
they do not contain the current transport and storage fixes. Keep source-bound
competitions on their original environment and install upgrades separately.

Next, run the [Python example](Quick-Start).

## V2 API

Install the API dependencies in the same environment:

```bash
python -m pip install "strathmark[api]==3.0.0rc14"
```

Then follow [REST API](REST-API) to choose an explicit database path and start the
server on loopback.
The public calculation endpoint does not require Ollama or a residual ML model.

## Current source build

The repository version is 3.0.0rc14. Use Python 3.13 and install from the repository
root into its own environment:

```bash
python -m pip install -e .
strathmark demo
```

This synthetic demo uses V2 calculations and opens no operator database. To work
on V3, follow [contributor setup](Testing) and install the exact release lock.
Installing the source build does not select V3 for an existing competition.

## Security maintenance

Use the maintained 3.0.0rc14 candidate for the October 2026 input, transport and
storage hardening. It publishes automatically after successful main CI:

```bash
python -m pip install "strathmark[api]==3.0.0rc14"
python -c "import strathmark; print(strathmark.__version__)"
```

Confirm the installed version is the reviewed 3.0.0rc14 release. The stable 2.0.x artifacts
remain immutable. Install an upgrade in a separate environment and run a synthetic
calculation before serving new work. Saved Linux V3 competitions retain their exact
source, model and key identities; continue them with their original environment.
Custom V2 model paths now require independent digest pins, and working-directory
models are ignored. See [Security maintenance](Security-Maintenance).
