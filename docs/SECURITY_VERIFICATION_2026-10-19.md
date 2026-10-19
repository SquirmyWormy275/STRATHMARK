# Security verification follow-up: 3.0.0rc13

The follow-up Security Cloud scan of rc12 found nine additional or residual
boundaries. This release addresses those findings without changing handicap
mathematics, trained model bytes or existing competition identities. Scan reports
are static-analysis leads; the table describes the verified behavior and tests.

| Boundary | Repair and regression evidence |
| --- | --- |
| Moving release tooling | Automatic jobs use the successful CI commit throughout. Manual tooling needs its own exact successful CI. Tests simulate main advancing and reject unreviewed or dirty tooling. |
| Supplied evaluator metrics | These remain diagnostic. The existing factory gate already refused promotion; the residual direct bundle publication path now requires signed promotion evidence. Tests reject diagnostic and forged typed reports and refuse operational reopening of legacy manifests. |
| Administrative workbooks | Import and Linux history paths consume bounded, regular-file snapshots before pandas/openpyxl. Tests reject expansion bombs, symbolic links and FIFOs before parsing. |
| V2 slow request bodies | Public per-source admission is bounded. Verified result-token callers have a separate pool; bodyless health is independent. Concurrent slow-body tests exercise all three boundaries. |
| V3 authentication overload | Excess requests fail immediately; timed-out work keeps its slot until actual completion. Tests exercise simultaneous overflow without semaphore waiters. |
| Local provider paths | Configuration, runtime root, executable and log paths must retain trusted ownership and ancestry. Tests reject writable parents and links without changing the linked file. |
| Encrypted recovery | Verification requires an independent plaintext identity; integrity-only inspection is explicit. Ciphertext and expanded plaintext are bounded, GPG has a watchdog, and hashing/decryption use the same opened file. Real-GPG tests reject public-key-only forged receipts and exercise genuine recovery. |
| Release build dependencies | Build, backend, Twine and transitive wheels have reviewed hash pins. Builds disable dependency resolution and verify installed bytes and source again before publishing artifacts. Tests reject source/toolchain changes; actual wheel and sdist rehearsals exercise the build path. |
| Qualification tool identity | Fresh evidence verifies locked wheel and installed-file bytes before and after proofs, plus an independently approved Python digest and Formula manifest. Tests reject same-version substitutions and added executable files. |

## Upgrade and recovery

Install rc13 separately for new profiles, paired with STRATHEX 7.4.5. Retained
competitions keep their original exact source, model, Formula inputs, authority
keys and data. No installer migration changes their numeric or signing identity.

An unsigned recovery receipt can describe an archive but cannot establish who
created it. Use `inspect_file` only to inspect legacy integrity. Restoration must
also match an independently trusted plaintext SHA-256 or an exact trusted
snapshot inventory. Existing encryption keys, policies and archive formats remain
available. Never copy the claimed digest out of the adjacent receipt as an
independent approval. See [Linux recovery](V3_LINUX_COMPETITION.md).

Old signed bundle manifests remain eligible for historical verification. They
cannot gain current operational authority without the required promotion proof.
Qualification evidence must be regenerated using the
[approved toolchain](QUALIFICATION_TOOLCHAIN.md); historical evidence is preserved.
These repairs do not provision Windows CNG, a production audit executor or an
independent monotonic witness.

Application admission limits do not control every connection inside a web server
or proxy. Remote services still need the documented TLS reverse proxy and
connection limits. GPG is killed on its deadline; a stalled kernel/filesystem read
is not guaranteed to be interruptible. Caller-approved large recovery sources can
use an explicit bounded allowance; no adjacent receipt supplies that authority.

The cloud dashboard's repository-scan entries are historical scan records. A
repair, a passing test and a new scan are distinct evidence; this document does
not assert that the original cloud records were administratively closed.
