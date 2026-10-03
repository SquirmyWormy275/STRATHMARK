# Cryptography security maintenance — October 2, 2026

The baseline patch introduced in STRATHMARK 3.0.0rc3 requires `cryptography>=50.0.2,<51` for API, security,
development, and candidate extras; its exact Python 3.13 lock uses 50.0.2.
STRATHEX 7.2.1 uses the same maintained floor. STRATHMARK 3.0.0rc4 and STRATHEX 7.3.0 retain this patch. The security-only change updates dependency security;
it does not change the frozen V7 contract, numeric algorithms, V2 receipts, or
production eligibility.

GitHub's refreshed dependency graph reported six advisories against 46.0.5:

- [GHSA-m959-cc7f-wv43](https://github.com/advisories/GHSA-m959-cc7f-wv43): DNS name constraints.
- [GHSA-p423-j2cm-9vmq](https://github.com/advisories/GHSA-p423-j2cm-9vmq): non-contiguous buffer overflow.
- [GHSA-537c-gmf6-5ccf](https://github.com/advisories/GHSA-537c-gmf6-5ccf): bundled OpenSSL.
- [GHSA-jwv3-5hgf-82ww](https://github.com/advisories/GHSA-jwv3-5hgf-82ww): certificate path-building denial of service.
- [GHSA-m2h6-j472-rp4c](https://github.com/advisories/GHSA-m2h6-j472-rp4c): wildcard DNS constraints.
- [GHSA-g6cj-pr64-35w5](https://github.com/advisories/GHSA-g6cj-pr64-35w5): PKCS#7 decryption oracle.

The [upstream changelog](https://cryptography.io/en/latest/changelog/) documents
50.0.0's decryption fix and 50.0.2's OpenSSL 4.0.3 wheels. Python 3.10–3.13
remain supported. Upstream no longer ships 32-bit Windows or Intel macOS wheels;
the repository's installed checks target Linux and 64-bit Windows.

Existing signed Windows receipts remain bound to their original source, wheel,
lock, and machine. They are preserved as historical development evidence;
changing this lock cannot qualify a new installation. Regenerate designated
machine evidence with independent production trust before claiming eligibility.

Installed tests exercise signatures, certificates, authenticated transport,
model integrity, and the exact lock on Linux/Windows. Private candidate artifacts
must be rebuilt against the updated source identity before selecting a new root.
Preserve old code and models for previously saved numeric previews.
