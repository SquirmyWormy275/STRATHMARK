# Use STRATHMARK through STRATHEX

[STRATHEX](https://github.com/SquirmyWormy275/STRATHEX) is the judge-facing terminal
application. It handles rosters, events, stands, review, result entry, advancement,
payouts, saves and Excel exports. STRATHMARK supplies predictions and marks.

## Start an event

Follow the [STRATHEX quick start](https://github.com/SquirmyWormy275/STRATHEX/wiki/Quick-Start).
Choose V2 or a configured V3 runtime when creating the event or tournament. All
children of a tournament inherit its engine. The first numeric operation locks it.

V2 normally runs in the STRATHEX Python process. Linux V3 runs in a separately
installed interpreter with its trained model and persistent local authority.
The authenticated V7 service is a different integration profile.

## Linux V3 workflow

Seeding times come first. Generate the exact heats and stands, calculate the whole
field, review it, approve it and confirm issue separately. Then record all outcomes,
authorize official placings and settle the round before advancing.

The LLM council is unavailable, so fields need deliberate degraded or individual
review. A preview-only profile cannot issue official sheets or record competition
results. See [engine choice](Competition-Engine-Selection).

## Resuming and retrying

Keep the original source, model, key and saved state. A timeout can leave a command's
outcome uncertain; retry the exact saved command instead of inventing a new one.
The selected engine never falls back to the other engine.

Developers implementing the authenticated service adapter should read the
[consumer migration guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/STRATHEX_CONSUMER_MIGRATION.md)
for contract pins, signed receipt verification, durable outboxes and human authorization.
