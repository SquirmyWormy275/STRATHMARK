# HTTP admission and evaluator publication verification

The October 19 verification follow-up keeps untrusted request bodies and
diagnostic metrics outside operational authority.

V2 accepts at most sixteen public requests, with at most four from any one
transport source address. Forwarded headers cannot manufacture a different
source. Four additional slots are reserved for callers whose authorization
header passes the existing `STRATHMARK_API_TOKEN` validator before any body is
read. Shadow endpoints retain their separate authenticated work pools. Bodyless
`GET /health` does not wait for incoming bytes or consume those slots; a declared
health body is rejected. Health now admits at most two complete requests, at most
one per transport source, before dependency/model/storage work starts. Overflow
receives 503 immediately. Client cancellation retains its slot until the shielded
dependency and handler task actually exits. The response remains a current diagnostic result with
the requested cutoff; a cache cannot silently substitute another cutoff or old
readiness state. The existing 1 MiB, two-second idle and ten-second total
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


Authenticated shadow bodies have a separate limit of eight complete requests,
at most two per verified service bearer, acquired before any body allocation.
Receipt lookup and status use four separate slots, at most two per bearer, so
calculation body saturation cannot consume recovery admission. Overflow returns
429 without reading bytes. Per-request 256 KiB and receive deadlines remain.
Admission is retained through the handler and always released on cancellation or
failure; operation workers retain their own slots until the underlying work exits.

New shadow calculations reject more than 1,024 history rows for one competitor,
8,192 rows for the whole field, or 4 MiB of selected row JSON before claiming the
actor nonce. The full operator snapshot still supports 100,000 rows and 32 MiB;
no history is silently truncated or deleted. Existing receipt lookup remains
available. A rejected field needs an explicit reviewed evidence selection before
calculation. Numerical rules and historical receipts are unchanged.

The same cooperative deadline follows evidence loading, calculation phase
boundaries, receipt lookup and the atomic ledger write. Cancellation before commit
rolls the entire field back. A timeout racing an already committed field remains
an unknown outcome requiring receipt lookup. Native numerical calls are not forcibly
terminated mid-call; bounded input and retained worker slots prevent new work from
accumulating while those calls finish.

The optional synthetic managed workspace admits at most two password verifiers,
at most one per transport source. Fixed 60-second windows allow at most 40 total
attempts, eight per supplied account and sixteen per source, including unknown
accounts. The dictionaries cannot exceed the total admitted attempt budget.
Overflow returns the same generic 429 without password work. Normal CSRF, Origin,
loopback defaults and account-bound sessions remain mandatory. These in-process
limits apply to each service process; remote/multi-process deployments still need
proxy connection and aggregate rate limits.
