# Accuracy Preview

**The calibration changes are implemented in this separate program. They are not
active in competition predictions.**

The preview compares baseline and candidate raw cutting times using an isolated,
read-only workbook copy. It preserves the original forecasts alongside the candidate
and does not create start marks, approve fields, issue sheets or settle results.

## What did the candidate improve?

On 278 previously examined results from 20 competition groups:

| Average absolute error | Baseline | Candidate |
| --- | ---: | ---: |
| All results | 23.12 s | 22.47 s |
| First appearances | 85.05 s | 76.52 s |

The overall improvement is 2.81%. It misses the 5% qualification target, and these
historical results are not an independent future test. Large errors remain,
particularly for competitors with no prior same-event history.

## Open it

On a workstation where the preview has been installed, open **STRATH Accuracy
Preview** from the app menu or run its configured launcher:

```bash
strath-accuracy-preview
```

The menu offers a saved accuracy comparison, cutting-time previews and competitor
IDs. The packaged CLI also has snapshot, pack, status, forecast and audit commands;
use `strath-accuracy-preview --help` to see them. With a direct package installation,
supply the subcommand and your actual candidate and workbook paths:

```bash
strath-accuracy-preview menu --candidate /absolute/path/to/candidate --workbook /absolute/path/to/history-copy.xlsx
```

Add `--report /absolute/path/to/audit.json` to load a saved comparison report.
The bare command above works only through the configured workstation wrapper.

For installation and those paths, follow the
[preview runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/tools/accuracy-preview/README.md).
The public package contains code only. Private model weights, calibration and
workbook history must be supplied separately. Owner data needs explicit authorization.
