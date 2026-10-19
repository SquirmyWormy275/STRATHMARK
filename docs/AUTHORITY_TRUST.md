# Authenticated event authority and retained registry heads

The 2026-10-19 security repair changes the operational V3 composition boundary.
`compose_v3_application_gateway` requires an `AuthenticatedEventAuthority`.
It binds every event-store instance for that database, including instances inside
lifecycle, field, job and credential services. Startup, command writes, exact
retries and reaction recovery reject missing, unavailable or mismatched current
authority. No environment variable enables an exception.

An ordinary event hash proves internal consistency. It cannot identify an
authorized writer: someone who can rewrite SQLite can recompute those hashes.
The operational authority therefore signs the current event sequence and digest,
the entire command-result ledger digest, the installation's witness scope, and
the prior witnessed generation. The event digest binds the actor, command,
payload, prior global/aggregate heads and sequence. The result-ledger digest also
binds exact result bytes, principals, idempotency keys and event-set links.

Each accepted command advances an independently retained signed state with an
atomic compare-and-swap while SQLite's writer lock is held. An exact retry must
match the current authenticated state and never creates a new generation. The
result-ledger digest is streamed rather than materialized as one document; this
cost grows with stored history and is a correctness tradeoff pending separately
qualified incremental authenticated indexing. No numeric prediction contract,
production qualification flag, source pin, or approved competition identity is
changed by this boundary.

## Required witness provisioning

`terminal_witness.MonotonicWitness` is an installation interface. **This package
does not supply or provision a production witness backend.** A deployment must
provide a reviewed implementation with authenticated reads and writes, durable
linearizable compare-and-swap, and monotonic generations. Reads must return the
latest committed generation. Eventual-consistency replicas and cached prefixes
do not satisfy the contract. A successful CAS must remain retained after process,
machine and ordinary database/registry restore operations.

The witness must be protected from the actor who can rewrite SQLite or registry
files. Suitable designs require an independently administered replicated service
or a deletion-resistant monotonic storage authority. A counter, signed file,
sidecar, backup directory, or checkpoint beside the database is not sufficient.
Runtime credentials may advance the scoped state but must not create missing
scopes, reset generations, replace trust roots, or delete retained states. The
signer identity and verification trust must be provisioned independently.

Provision each new scope explicitly at `WitnessState(0, b"")`; a missing scope is
an error, not an invitation for runtime bootstrap. Use separate installation
scopes for event authority, external rolling heads, and the checkpoint registry.
For a checkpoint binding named `installation/checkpoint`, provision
`installation/checkpoint/rotations` and `installation/checkpoint/checkpoints`.
For a rolling-head binding, provision the supplied scope itself. Never derive a
new scope from mutable database contents after a failed reopen.

For a new, empty database, an offline provisioning operation may call
`AuthenticatedEventAuthority.provision_empty_database(database, created_at=...)`.
Then pass that same authority to `compose_v3_application_gateway`. The provisioner
rejects an existing event history. Startup does not call this operation for you.
After provisioning, verify restart with the witness available, witness outage,
stale witness replies, concurrent CAS, coherent SQLite rollback, and registry
tail deletion before enabling an installation. These backend qualification
checks remain required; synthetic unit tests do not qualify a production backend.

The standalone Windows qualification entrypoints
`scripts/benchmark_v3_result_to_ready.py` and
`scripts/benchmark_v3_field_assembly.py` currently construct synthetic event-store
fixtures without an authenticated witness. Outside an explicitly selected test
scope they now fail closed at the event-write boundary. Consequently,
`scripts/run_v3_release_evidence.py`, which invokes both benchmarks, cannot produce
new operational qualification from those fixtures. Before using these commands
for operational qualification, supply the real independently provisioned witness
and measure its authentication, signing, CAS, timeout and recovery costs through
the operational path. Do not add automatic rehearsal selection to make release
evidence pass, and do not reuse older unwitnessed latency results as proof of the
new operational path. Ordinary package candidate publication and portable
rehearsal tests do not establish that qualification.

## Preserving and reviewing existing evidence

1. Stop the listener, workers and backup publishers. Preserve independent copies
   of the complete SQLite database, WAL where present, blobs, registry records,
   public-key history, critical journals and existing exported evidence. Record
   independent hashes and retain the original files unchanged.
2. Review the retained history against independently trusted sources. Establish
   the exact current global sequence, event digest and command-result ledger
   digest. The result-ledger encoding is the SHA-256 stream of canonical rows in
   `(principal_id, idempotency_key)` order, with an eight-byte big-endian byte
   length before each row, as defined by `event_authority._facts`. Its columns
   include result bytes, command/event-set digests, sequence range and timestamp.
   Computing these values from the suspect database alone does not authenticate
   them. Existing checkpoint files must also match independently retained terminal
   sequence/digests, including the latest key rotation.
3. If the independent evidence does not establish that exact baseline, keep the
   database as forensic evidence and leave operational V3 unavailable. Do not
   delete or rewrite history, replace it with an empty database, or sign whatever
   prefix happens to remain. Existing signatures without a retained terminal
   state do not prove that no later signed record was removed.
4. After independent review and explicit offline provisioning, call
   `provision_reviewed_database` with the independently established
   `expected_global_sequence`, `expected_event_digest` and
   `expected_command_results_digest`. It replays the existing chain under a
   writer lock and requires exact agreement before signing generation one of an
   explicitly provisioned empty witness scope. It performs no schema migration,
   event rewrite, projection reset or witness reset. For existing registries,
   the independent witness administrator must provision the reviewed terminal
   states using the deployment's controlled import process; normal registry
   startup cannot adopt a surviving local prefix.
5. Reopen through the operational gateway and complete the backend qualification
   checks above. Keep the prior evidence copies and the reviewed baseline record.
   Existing production eligibility and competition pins remain unchanged.

## Failure and recovery behavior

The witness advances before SQLite commit and before acknowledgment. This avoids
acknowledging authority that has no independently retained current state. A
witness failure rolls back SQLite. If CAS committed but its reply was lost, or if
SQLite commit fails after CAS, the witness may be ahead of SQLite. Reopen then
fails closed. This is an integrity incident, **not automatically recoverable
transaction replay**: the witness retains signed digests, not the missing event
and result bytes. Reconstruction requires independent surviving evidence and
explicit review. If those bytes cannot be established, that generation cannot
be recovered from the witness alone. Never decrement/reset the witness or
automatically adopt the shorter database. Similarly, a rolled-back acknowledged
database requires restoration of its exact retained generation.

Checkpoint, key-rotation and rolling-head publication retain signed files before
advancing their terminal witnesses. A crash between those steps also fails
closed; reconcile the preserved signed records against independent evidence.
The runtime does not infer that a longer local chain is authorized. Cached
registry objects recheck retained files and the witness before using or advancing
a head, so deleting the last file cannot turn a signed prefix into current trust.

Reaction projection rebuilding replays and authenticates events under the same
`BEGIN IMMEDIATE` transaction that replaces the views. Verification occurs before
the first deletion. A competing writer cannot change the checked event head
between validation and replay, and validation failure preserves the prior views.

## Explicit rehearsal and forensic access

Synthetic callers can explicitly select `enable_unwitnessed_rehearsal` for an
isolated root using an ephemeral development signer. The separately named
`compose_v3_rehearsal_gateway` selects only its specified database and rejects an
installation signer. The operational gateway never selects this exception and
always requires an authenticated authority, even inside a rehearsal root.
Registry fixtures can explicitly use `allow_unwitnessed_rehearsal=True`; those
objects make no claim of rollback protection. Tests opt in for their isolated
temporary root, not through a runtime environment-variable bypass.

The low-level read-only authority verifier remains useful for historical and
forensic replay against an explicitly supplied anchor. An ancestor anchor alone
is not operational current authority. `from_checkpoint_registry` now requires
the exact current signed checkpoint; future writes still require the operational
event witness or an explicit rehearsal selection.
