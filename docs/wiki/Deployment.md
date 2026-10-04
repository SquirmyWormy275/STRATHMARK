# Deployment and recovery

## Linux V3 competitions

Use STRATHMARK 3.0.0rc7 with STRATHEX 7.4.2 in separate Python 3.13 environments.
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

For profile selection and rollback, use the
[portable installer guide](https://github.com/SquirmyWormy275/STRATHEX/blob/main/docs/PORTABLE_INSTALLATION.md).
Rollback selects an earlier installation; it does not rewrite saved competitions.

## Encrypted archives

The optional GPG policy encrypts backups and verifies full decryption before success.
Keep a protected recovery-key copy separately from the workstation and archive disk.
A public fingerprint alone cannot decrypt a backup. Preserve plaintext archives
until encryption readback and recovery-key storage are verified.

The runbooks cover policy fields, hard-link requirements, interrupted publication
and restore checks.

## Windows V7 service

Windows production qualification is incomplete. It requires the designated installed
composition, non-exportable CNG signing identities, exact production evidence and a
separately authorized eligibility handoff. A development-key rehearsal cannot supply
that authority. See the [Windows deployment specification](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/DEPLOYMENT.md).
