# Security maintenance

The 3.0.0rc12 candidate applies the October 2026 Security Cloud fixes. The V2
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
- Manual PyPI recovery requires successful main CI for the exact tagged source.
  Release evidence records controlled tool, environment and executable identities.

Read the [full security and upgrade notes](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SECURITY_HARDENING.md)
for exact limits and custom-model variable names.

Upgrade through [Installation](Installation) in a separate environment and verify
synthetic calculations first. Do not replace the exact runtime of a saved Linux
V3 competition: source, model and signing identity are frozen for that competition.
Preserve its original environment and verified recovery copies. Package publication
does not grant Windows V3 production authority.
