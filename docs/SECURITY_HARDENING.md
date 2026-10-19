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
unsafe ownership are rejected. Every authority access checks existing ancestors
from the filesystem root without changing their permissions. Ancestors must be
owned by the current effective user or root, and group/other write access is
rejected unless the directory has the sticky bit. This preserves safe 0755
parents and trusted-owner sticky temporary roots while rejecting locations
where another account could replace a checked database, private key or complete
authority directory before it is reopened. POSIX ACL write masks are reflected
in the group mode bits and are covered by the same conservative check. The
protection assumes the current account and root remain trusted. See
[rename permissions and sticky protection](https://man7.org/linux/man-pages/man2/rename.2.html)
and [ACL permission-bit correspondence](https://man7.org/linux/man-pages/man5/acl.5.html).
The immediate parent of an authority file must also prevent peer creation:
SQLite reuses predictable journal/WAL names, which the sticky bit alone does not
protect before creation. Use a dedicated private subdirectory beneath `/tmp`,
not a database directly in its shared root. Missing intermediate authority
directories are created with mode 0700 even under umask 000. The helper will not
tighten HOME, its ancestors or the configured shared temporary root.

Windows creates files and directories with a
protected DACL granting full access only to the current process user. Native
handle checks reject reparse points, hard links and unrelated owners; existing
ACLs are tightened and read back before access proceeds. Existing administrative
ownership is accepted, but receives no separate file DACL grant. The private
directory's inheritable rule protects SQLite sidecars from their first byte.
ACL failures and filesystems that do not retain these permissions stop the
operation. Choose a dedicated authority subdirectory: the helper refuses to
change the home directory, its ancestors or the shared temporary directory.
It never changes ancestor permissions merely to create a deeper directory.
Opening a file verifies an existing parent without changing that parent's ACL.
A broader parent is rejected; applications must explicitly provision their own
dedicated authority directory with `private_directory`. The default
`~/.strathmark` root is provisioned explicitly. A file path in Documents or a
shared project directory does not authorize changing that directory's access.
Every access also checks existing ancestor ownership and permissions without
changing them. An unrelated account's delete, replacement or security-control
rights, unknown ACL entry types, and junctions stop the operation. Current-user,
SYSTEM, built-in Administrators and Windows TrustedInstaller authority are
trusted; these checks do not claim protection against that user or machine
administrators. Shared writable ancestry must be replaced by a separately
provisioned private location; it is never repaired implicitly.
The Windows [Owner Rights identity](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/81d92bba-d22b-4a8c-908a-554ab29148ab)
applies only to that object's actual owner. Its ancestor ACL entries are accepted
only after the same handle's actual owner has passed the trust check; they do
not allow unrelated owners or unrelated write grants.

The shared checks also protect the separate Linux lifecycle store and managed
event SQLite/head files, workspace credentials and local GPG key home. Fresh
dedicated managed-event roots and transfer staging are explicitly provisioned;
an existing shared folder is never tightened merely because a database or key
path names it. Plaintext authority backups need safe ancestry. Authenticated
encrypted backup files and their receipts may still use an independent removable
filesystem with permissive emulated modes; they contain ciphertext, and their
private staging and decryption key remain on protected local storage.

Native Windows CI checks creation-time ACLs, existing-file tightening, sidecar
inheritance, managed-event storage and workspace keys, hard links and failure
behavior using independent .NET ACL reads.
These tests are distinct from designated Windows CNG production qualification.
The implementation follows Microsoft's [file security rules](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights),
[CreateFile security attributes](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [handle-based security updates](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo).

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
