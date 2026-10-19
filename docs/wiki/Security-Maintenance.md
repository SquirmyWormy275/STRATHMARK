# Security maintenance

The 3.0.0rc14 candidate applies the October 2026 Security Cloud fixes. The V2
numeric model and handicap policy remain the reviewed engine; the changes protect
request handling, local input, model loading, storage and release provenance.

- HTTP bodies and history collections have explicit limits. Slow bodies time out,
  and timed-out V3 workers retain their admission slot until actual completion.
- ZIP, TAR and Excel inputs are checked for member, byte, expansion and dimension
  budgets before normal parsing. Oversized imports and archive packages fail early.
- Working-directory files cannot replace the packaged V2 model. Explicit custom
  models require absolute owner-controlled paths and separately configured SHA-256
  pins for the core file and residual manifest.
- Pre-field receipts use service time. A backdated caller request cannot authorize
  new work after the deadline; an exact retry can recover a timely saved receipt.
- POSIX authority files and new private roots use explicit owner-only permissions.
  Blob reads retain verified handles and restore verifies the copied database before
  activation. Windows ACL verification remains an installation requirement.
- Operational V3 event and command-result authority and signed registry heads
  require an independently provisioned monotonic witness. Missing or mismatched
  current trust stops operation and preserves existing evidence. See
  [authority trust and migration](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/AUTHORITY_TRUST.md)
  for provisioning and integrity-incident recovery; the package does not supply a
  production witness backend.
- Manual PyPI recovery requires successful main CI for the exact tagged source.
  Release evidence records controlled tool, environment and executable identities.

Read the [full security and upgrade notes](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SECURITY_HARDENING.md)
for exact limits and custom-model variable names.

Upgrade through [Installation](Installation) in a separate environment and verify
synthetic calculations first. Do not replace the exact runtime of a saved Linux
V3 competition: source, model and signing identity are frozen for that competition.
Preserve its original environment and verified recovery copies. Package publication
does not grant Windows V3 production authority.

## Verification follow-up in rc13

The follow-up scan identified nine additional or residual boundaries. Public V2
requests cannot consume the reserved authenticated pool or bodyless health route;
V3 authentication overflow has no application waiter queue. Administrative Excel
imports use the bounded parser, and local provider paths must have trusted ancestry.
Diagnostic evaluator results cannot publish or reopen an operational bundle.

Encrypted recovery inspection reports integrity only. Origin verification needs an
independently retained plaintext digest or the exact trusted snapshot inventory;
an adjacent unsigned receipt is not that authority. GPG input, expansion and
execution have finite limits. Existing keys and encrypted archives are preserved.

Release code runs from the successful CI commit, and the build uses reviewed
wheel hashes with installed-byte checks. New qualification evidence similarly
requires approved Python and Formula toolchain identities. Old evidence remains
historical and does not grant fresh production eligibility.

See the [complete follow-up scope](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SECURITY_VERIFICATION_2026-10-19.md),
[admission and promotion rules](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/VERIFICATION_ADMISSION_AND_PROMOTION.md)
and [qualification toolchain](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/QUALIFICATION_TOOLCHAIN.md).

## Final scan follow-up in rc14

The [rc14 security record](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SECURITY_FINAL_SCAN_2026-10-19.md)
covers health/shadow/password admission, parser metadata limits, private plaintext
archives, same-read archive proof, and credential/snapshot concurrency. Full public
request identities are reserved before lifecycle preparation and authenticated by
the independent witness. Preserve pre-upgrade databases and original runtime profiles;
legacy public keys without a complete request binding cannot be silently adopted.
These changes do not grant Windows V3 production qualification.
