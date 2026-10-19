# Deployment and recovery

## Linux V3 competitions

For new profiles, use STRATHMARK 3.0.0rc12 with STRATHEX 7.4.4 in separate Python
3.13 environments. Retained rc7/7.4.2 competitions keep their exact original
runtime, model, signing authority and data; never upgrade them in place.
The V3 environment needs the exact release lock, a verified trained model and a
persistent local signing authority. The operator launcher requires an existing
backup directory on a different filesystem from the live workbook.

Follow the [Linux setup and recovery runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md)
for the exact initialization and launch commands.

## What must be backed up?

Keep the workbook, STRATHEX saves and command/selection ledgers, V3 database and
separate head, signing key and identity, frozen histories, trained model and exact
runtime/wheels together. A saved competition is bound to its original source,
model and key; preserve older installations after an upgrade.

Restore into a new private directory and verify the recovered files before opening
it. On Linux, the authority directory needs mode 700 and key, identity, head and
database files need mode 600. Do not initialize a replacement key over an existing
competition or edit the database to repair a command.

Authority paths also need trusted ancestors. On POSIX, parent directories must
belong to the current user or root, with no group/other write access unless they
have the sticky bit. Safe 755 parents and trusted sticky temporary roots remain
supported; shared writable locations are refused without changing their modes.
Store database files inside a dedicated subdirectory of a sticky temporary root:
their immediate parent must prevent peers from precreating journal/WAL siblings.
This applies to local databases, workspace secrets and plaintext backups.
Authenticated encrypted archives may still use a separate removable filesystem
with permissive emulated modes. See
[Security hardening](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/SECURITY_HARDENING.md).

For profile selection and rollback, use the
[portable installer guide](https://github.com/SquirmyWormy275/STRATHEX/blob/main/docs/PORTABLE_INSTALLATION.md).
Rollback selects an earlier installation; it does not rewrite saved competitions.

## Encrypted archives

The optional GPG policy encrypts backups and verifies full decryption before success.
Keep a protected recovery-key copy separately from the workstation and archive disk.
A public fingerprint alone cannot decrypt a backup. Preserve plaintext archives
until encryption readback and recovery-key storage are verified.

From rc13, later `verify` calls require an independently retained plaintext SHA256;
never obtain it from the adjacent unsigned receipt. A public recipient-key holder
can create ciphertext and a matching receipt, so successful decryption alone is not
proof of origin. `inspect` is explicitly integrity-only for legacy archives. Native
competition backup retries still compare all members to the trusted local snapshot.
The GPG stream has finite input/output allowances and a watchdog; larger archives
need a source/inventory-owned allowance. Receipt sizes cannot raise limits. Existing
certification-only keys and archives remain readable without changing live policies.

The runbook's [recovery trust and limits](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md#rc13-recovery-trust-and-processing-limits)
also covers hard-link publication, the independent pin, restore checks and the
remaining limitation when an operating-system file read stalls.

## Windows V7 service

Windows production qualification is incomplete. It requires the designated installed
composition, non-exportable CNG signing identities, exact production evidence and a
separately authorized eligibility handoff. A development-key rehearsal cannot supply
that authority. See the [Windows deployment specification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md).

Fresh rehearsal evidence also requires the exact evidence-tool lock, isolated Python
(`python -I`), a clean committed checkout, and an independently approved NVIDIA tool
path and digest. Its v2 receipts record the effective child environment, Python
binary digest and tool versions. Existing receipts must be regenerated; a successful
Linux regression run does not substitute for designated Windows execution.
