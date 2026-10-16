# Managed event operations

`strathmark.managed-event.v1` is a persistent, presentation-independent event
boundary. The first supported profile operates **synthetic individual SB/UH
handicap events with the real V2 predictor**. Official use is disabled. Existing
calculation-only, V2 shadow, V3 Linux and V7 contracts remain separate and intact.
See [the ecosystem agreement](MODULAR_ECOSYSTEM.md) for ownership and numerical policy.

## Install and operate

Install `strathmark==3.0.0rc11` in its own Python 3.10–3.13 environment. The
calculation and event CLI do not require Flask, STRATHEX, Missoula or MNEMEX.
Use explicit absolute storage paths. The local database and its sibling
`.head.json` file are private authority material; protect and back up both.
The local CLI trusts the OS account that can write these files. `--actor` is
attribution within that local trust boundary, not remote human authentication.

```bash
strathmark capabilities
strathmark example --output /private/new-event.json
strathmark event create --database /private/events.sqlite3 \
  --show synthetic:missoula --request-id create-001 --input /private/new-event.json
strathmark event status --database /private/events.sqlite3 \
  --show synthetic:missoula --event-id synthetic:missoula:underhand
```

Edit the generated synthetic configuration or supply your own supported JSON.
The example's 275 mm wood does not change Missoula's actual wood configuration.
The root records the required `selection_reason`, actor, time and numeric runtime
identity and mark policy. Children inherit it and reject a different numeric environment.
Names are display data; every consequential operation binds entry, host and
engine identities. History requires source references and dates strictly before
`prediction_as_of`. `event_date` identifies the source occurrence. Unknown
quality is `null`; known quality 0–10 is recorded and numerically ignored in V2.
`quality_sensitive` is unsupported.

For calculation-only use, `strathmark calculate --input request.json --json`
accepts `strathmark.calculation.v1`, with `engine`, `mark_policy`, `discipline`,
`wood`, `quality_policy`, `prediction_as_of`, and `competitors` (the same entry
records as the configuration roster). It opens no operational database and
returns `issued_mark=false`.

Mutations use `event ACTION --database PATH --show SHOW --event-id ID
--request-id STABLE_ID --expected-revision N --editor cli --input PAYLOAD.json`.
Use `--json` for complete machine-readable responses. Retain the exact request
after a timeout: an accepted retry returns the original response, even after
a later correction. Changed payloads and stale new edits conflict.

| Action | Payload / effect |
| --- | --- |
| `forecast` | `{}`; mark-free seeding evidence |
| `fields` | `fields`: exact heat IDs, ordered `entries`, physical `stands`; optional host `schedule_ref` and `unavailable_stands` |
| `calculate` | `heat_id`; binds wood, actual field, immutable history and numeric identity |
| `review` | `heat_id`, `sheet_digest`; whole-sheet deliberate review |
| `issue` | same binding plus `confirm: true`; separate immutable issue identity |
| `outcomes` | `heat_id`, `issue_id`, complete `outcomes`; each entry supplies explicit status and raw timer readings |
| `accept` | `heat_id`, `result_digest`; explicit result acceptance, independent of export/publication |
| `advance` | `{}` normally; boundary ties under judge policy require `tie_selections` by heat and a `reason` |
| `finalize` | `{}` after the complete accepted final |
| `correct` | complete replacement outcomes, current `supersedes` result digest, issue identity and `reason` |
| `retain_downstream` | director `reason`, explicitly retaining already-issued downstream races after a correction |
| `configure` | complete `config`; unissued inputs only, invalidating prior preparation while preserving root selection |
| `handover` | next `editor`, e.g. `gui`; revisions change but the physical field does not |
| `export` | read-only per-attempt history, independent summary and durable delivery states |
| `backup` | new `--output`; checksummed checkpoint, not independent restore authorization |

Finished outcomes use `timer1_seconds` / `timer2_seconds`, explicit
`raw_duration_seconds`, `start_mark_seconds`, `common_count_seconds`, status and
official position in the result. Raw cutting duration plus start count is
common-count completion. Dual timers are averaged with decimal arithmetic.
One-timer results require explicit partial capture and subsequent judge review.
Nonfinishes (`dnf`, `dq`, `dns`, `scratched`, `void`) have no invented duration.
Equal completions share positions. Eliminated entries share the latest-round
placing tier; times from separately rebased heats are never compared to invent
a cross-heat order. The final is one field.

Source attempt IDs use a versioned hash of the structured show/tournament/event/heat/entry
identity; delimiter-bearing IDs cannot collide.

Corrections append source revisions. Unissued dependent fields are invalidated;
issued fields and their history remain frozen with a visible progression-review
gate. A director retention decision preserves its reason, actor, time and exact
result/issue bindings in state and export. Every latest result used by a summary
must be accepted before finalization, including retained earlier-round corrections. Host points/payout corrections remain separate, explicit actions.

## Optional shared workspace

```bash
python -m pip install 'strathmark[workspace]==3.0.0rc11'
strathmark workspace-account --auth-file /private/operators.json \
  --actor director --show synthetic:missoula
strathmark workspace-key --session-key-file /private/workspace.key
strathmark workspace --database /private/events.sqlite3 \
  --auth-file /private/operators.json --session-key-file /private/workspace.key
```

The account command prompts for a password; it never accepts secrets in URLs.
Configuration files require mode 600 on POSIX. Open `http://127.0.0.1:8789`,
sign in and open the existing event. Deep links support event/round/heat anchors.
Hand over from CLI to `gui` deliberately before editing. The workspace provides
field/stand assignment, mark review, separate issue confirmation, outcome entry,
advancement, correction, sync status and independent printable issued sheets.
For reproducible real-browser QA, install Playwright in the workspace test
environment and run `python -I scripts/verify_managed_browser.py /tmp/new-browser-proof`
with `/usr/bin/chromium` available. The script creates only new synthetic storage,
checks narrow-screen layout, and terminates its own server.

Both interfaces call the same operations and reject stale revisions. Browser
sessions are scoped by the current private account registry; account changes
revoke prior sessions. Normal CSRF remains enabled. No service credential is
sent to a browser. The loopback development launcher is not a hosted production
deployment procedure; network/identity/restore qualification remains separate.

## Host and records adapters

Python hosts call `EventOperations` with an authenticated, scoped `Principal`.
Hosts on a separate interpreter may use the bounded, one-shot
`python -I -m strathmark.events.rpc --database PATH` stdin protocol. This local
adapter trusts the private OS process, not caller-supplied public actor headers.
Missoula retains its old 2.x environment and explicitly selects this separate
installed runtime. It never falls back after failure.

Accepted attempts export `strathmark.source-result.v1`: source show/event/result,
source revision, predecessor source digest, correction reason, event date, wood,
round/heat/entry/issue/field evidence, explicit clocks, result status and position.
The host is the default outward MNEMEX publisher. A standalone configuration
declares its source show and publisher. The host wraps an accepted source in
`mnemex.show-result.v1` with a scoped participant reference; prediction settlement
is not career publication. MNEMEX downtime leaves a durable intent and never
undoes accepted results. All heat attempts survive separately from the summary.

## Recovery and verification

Before a heat, retain its issued sheet independently of the running service.
Already-issued races may record manual outcomes against that exact sheet and
reconcile later. Unissued fields pause; a traditional/manual authority change
requires a director decision and must not be labeled engine-issued. No automatic
cloud/local or V2/V3 failover exists.

`event transfer --destination-database NEW_PATH` additionally requires the normal
event identity, request ID, expected revision and current editor. It fences the
source before reconstructing the event and accepted receipts into pristine local
storage. The fenced source cannot target another destination. Exact completed
transfer retry reopens that same target. Publication atomically refuses an existing target; a late collision preserves
the other authority and leaves the source fenced. Interrupted transfers remain
blocked for explicit reconciliation. The head binds its absolute authority path; copying the DB/head pair to another
path is refused. Never restore both old files over the current original location:
that requires an independent current-head record outside this storage pair.
An old checkpoint cannot independently establish current
authority. Loss of the source machine needs independently verified current-head
and external fencing evidence; this local procedure does not claim that gate.

```bash
python -I -m strathmark.events.verification --output /tmp/new-installed-cli-proof
strathmark evaluate synthetic --output /tmp/new-synthetic-evaluation
```

The CLI verification runs actual installed commands through multiple process
restarts for Missoula and a different synthetic configuration. Tests separately
cover ties, scratches, corrections, stale editors, pre/post-commit process kills,
restore fencing, browser CSRF and scope. Reports distinguish synthetic numerical
execution from historical accuracy, prospective qualification, provider recovery
and independent human rehearsal. Existing historical replay, frozen prospective
and separate preview commands remain in [accuracy evaluation](ACCURACY_AND_COUNCIL.md).

## Earlier unpublished development checkpoints

The final managed candidate declares `source_identity_version: canonical-tuple.v1`
in capabilities, state and export. Earlier unpublished rc11 development checkpoints
with concatenated attempt identities remain available for inspection/export, but
new mutations are refused rather than mixing identity schemes or rewriting old
receipts. Resume those synthetic experiments only with their original preserved
artifact, or start a fresh occurrence with the final candidate. This is not a
migration of an official show. Established V2/V3 and STRATHEX saves are unaffected.
