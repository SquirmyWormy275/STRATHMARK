# HTTP admission and evaluator publication verification

The October 19 verification follow-up keeps untrusted request bodies and
diagnostic metrics outside operational authority.

V2 accepts at most sixteen public requests, with at most four from any one
transport source address. Forwarded headers cannot manufacture a different
source. Four additional slots are reserved for callers whose authorization
header passes the existing `STRATHMARK_API_TOKEN` validator before any body is
read. Shadow endpoints retain their separate authenticated work pools. Bodyless
`GET /health` does not wait for incoming bytes or consume those slots; a declared
health body is rejected. The existing 1 MiB, two-second idle and ten-second total
body budgets remain. The documented development bind is loopback. Remote use
requires a TLS reverse proxy with connection and slow-client limits; application
admission does not control connections waiting inside the web server or proxy.

V3 blocking work reserves capacity without waiting. Excess authentication
requests receive 503 immediately and do not join a semaphore queue. Timed-out or
canceled callers continue to occupy their worker slot until the real work exits.
Public bodyless health remains separate. These checks bound admitted application
work; they do not claim to prevent all network denial of service.

The isolated evaluator request format accepts supplied metrics for diagnostics.
Its digest verifies consistency, and its signature records the diagnostic
outcome. It does not execute an independently provisioned audit harness and
cannot produce promotion evidence. A passing diagnostic report does not
authorize a model change.

`BundleRepository` now requires signed promotion authorization and the matching
promotion-evidence digest by default. A typed report cannot override its signed
fields. New signed bundle manifests use schema v2 and retain that promotion
status. Operational verification refuses diagnostic bundles and old manifests
without promotion proof, including reopening a bundle written by another
repository instance. Historical verification remains available without changing
the stored bundle. Existing retained installation profiles keep their original
immutable runtime; this change does not migrate them.

Synthetic publication tests may opt into
`allow_diagnostic_publication=True` only in a nonproduction repository. That
option is incompatible with `production=True`. The factory service's existing
promotion-calibration and signer checks remain mandatory. This repair does not
provision an audit executor, Windows CNG identity, independent witness, or other
production qualification.
