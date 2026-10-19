# Security hardening in 3.0.0rc12

The October 2026 Security Cloud review adds explicit limits and trust checks at
public and local input boundaries. These changes preserve the packaged V2 model,
mark calculations, existing receipt identities and deliberate competition-engine
selection. They do not qualify Windows V3 production authority.

## Input and work budgets

| Boundary | Enforced limit |
| --- | --- |
| V2 HTTP | 1 MiB before JSON parsing; 16 concurrent requests; 10-second body deadline and 2-second idle receive limit |
| V2 history | 1,000 results per competitor; 4,000 per field; 64 override entries; unknown calculation input fields rejected |
| V3 HTTP | Existing 1 MiB ceiling; 10-second body deadline and 2-second idle receive limit; public bodyless health bypasses authority admission |
| V3 synchronous calls | Shared bounded executor retains its slot until the actual worker exits, including after HTTP timeout |
| Canonical decimals | Fixed-point expansion must fit 128 characters before formatting |
| Support ZIP | Stored entries only; at most 1,024 members and 8 MiB expanded data |
| Workbook | 64 MiB input, 128 MiB expanded, 1,024 members, 200:1 maximum compression ratio; 32 worksheets, 100,000 rows, 256 columns and 1,000,000 populated cells |
| Recovery TAR | 1,024 members, 64 MiB per member, 128 MiB complete expanded stream and a 30-second processing deadline |
| V2 import | 64 MiB source, 128 tables, 100,000 total rows and a mandatory 60-second default deadline |
| Archive proof | 128 MiB package checked before allocation |
| Optional Ollama narrative | Streaming read, 1 MiB response ceiling, 20-second caller deadline (5 seconds for health), at most two admitted network calls |

Over-limit work fails explicitly. Split large offline datasets into separately
reviewed imports rather than raising production limits casually. A timeout never
proves that an already-started authority write did not commit; recover by the
original idempotency identity.

## Model trust

Working-directory `models/` files no longer override the packaged V2 core. A
custom core requires an absolute owner-controlled path in
`STRATHMARK_PREDICTION_CORE_ARTIFACT` and its independently configured raw-file
SHA-256 digest in `STRATHMARK_PREDICTION_CORE_SHA256`. A custom residual requires
an absolute `STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT` directory and an independently
configured `STRATHMARK_PREDICTION_RESIDUAL_MANIFEST_SHA256` digest. The manifest
continues to bind the native model, promotion evidence, core and causal cutoff.
Set pins from the separately reviewed artifact, never from an untrusted download
at load time. Invalid custom authority produces the existing explicit degraded
state; it cannot silently become the current numeric model.

## Time and local storage

Pre-field receipts use the injected service clock. New work after the snapshot
deadline is rejected even if the caller backdates its request. An exact retry can
recover an already persisted timely receipt after the deadline. These receipts
remain seeding-only and never issue marks.

On POSIX systems, created authority databases and blob/backup files use mode 0600
and new authority roots use 0700 independently of the process umask. Existing
owner files are tightened without changing their content; links, junctions and
unsafe ownership are rejected. Windows emits an explicit warning requiring
independent owner-only ACL verification on the designated installation; Linux
mode tests do not establish Windows ACL qualification.

Blob reads hash and return content through the same locked descriptors. Import
parsing uses a private snapshot matching the original source digest and rechecks
the source before recording authority. Restore verifies the copied temporary
database before publication and quarantines a failed final verification. These
controls supplement the existing signed checkpoint and backup reconciliation;
they do not grant an untrusted backup competition authority.

Manual publication recovery now proves successful main CI for the exact source
and protected-main ancestry. Release evidence uses the separately pinned tool
lock, isolated Python execution, a controlled environment and explicit executable
identities; see [Package release](PACKAGE_RELEASE.md).

The Ollama deadline includes response headers. A caller stops waiting at the total
deadline even if a peer trickles headers without triggering a socket idle timeout.
Requests cannot forcibly cancel that header read, so its bounded worker retains
its admission slot until the network call actually exits. At most two such calls
may remain; further calls decline immediately instead of accumulating threads or
queued requests. A returned response is closed after the byte/deadline checks.
This optional narrative path does not change numeric forecasts or marks.


Operational V3 gateways now require independently authenticated current event and
command-result authority. Signed checkpoint, key-rotation and rolling-head
registries require an independent monotonic terminal witness to reject deleted
tails. Reaction rebuilding verifies its authority under the writer lock before
replacing any views. See [Authority trust and migration](AUTHORITY_TRUST.md) for
backend provisioning, preserved-evidence migration, explicit rehearsal exceptions
and the fail-closed uncertain-commit incident procedure. No production witness
backend is included or provisioned by this package update.
