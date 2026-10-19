# Qualification tool byte identity

New V3 executable evidence uses schema v3. The reviewed
`requirements/v3-verified.lock` contains the SHA-256 distribution allowlist for
the unchanged exact runtime and evidence-tool version locks. Retain the complete
platform-specific wheelhouse: the verifier compares each wheel with that lock,
then compares every installed package file with its authenticated wheel member.
Missing wheels, altered same-version packages, added package files and cached
bytecode fail verification. Build tools run as Python modules, so generated
console wrappers are not qualification inputs.

Use a new virtual environment on the designated trusted host and a fresh committed
checkout. Obtain the Python executable digest from the administrator's independently
approved provisioning record. The interpreter, its standard library, OS and this
verifier remain trusted host components; these checks cannot secure an already
compromised process. Never approve a proposed tool by hashing it and accepting
that observation as its own authority.

```powershell
$env:STRATHMARK_VERIFIED_WHEELHOUSE = 'C:\approved-proof-tools\wheels'
$env:STRATHMARK_EVIDENCE_PYTHON_SHA256 = $approvedPythonSha256
python -I -B -m pip download --require-hashes --only-binary=:all: -r requirements/v3-verified.lock -d "$env:STRATHMARK_VERIFIED_WHEELHOUSE"
python -I -B -m pip install --force-reinstall --no-compile --no-index --find-links "$env:STRATHMARK_VERIFIED_WHEELHOUSE" --require-hashes -r requirements/v3-verified.lock
python -I -B scripts/verify_toolchain.py --lock requirements/v3-verified.lock --wheelhouse "$env:STRATHMARK_VERIFIED_WHEELHOUSE" --installed --required requirements/v3-release.lock --required requirements/v3-evidence-tools.lock
```

Install only reviewed code in the environment. Use `-B` for subsequent Python
commands: `--no-compile` avoids installation-time bytecode, while `-B` prevents
new cache writes. Source execution still requires the approved source installation
and existing clean-checkout rules. General portable tests may use other compatible
dependencies; that does not create designated-machine qualification.

The evidence runner authenticates the installed tools before importing runtime
signing dependencies, checks the same identity at the start and end of proof
generation, and binds the wheel hashes, installed-file digest, hash-lock digest
and approved Python digest into the signed evidence payload. Verification requires
the same retained wheelhouse and approved Python identity. A version-only v1/v2
receipt remains a historical record and cannot qualify a changed release under
schema v3; do not rewrite old receipts to make them appear current.

[Formula workbook tooling](FORMULA_RUNTIME.md) has a separate independently
approved manifest for Node and the complete module inventory. The manifest is
required before any version probe or workbook replay. Canonical Formula paths are
frozen for execution and their input files/directories must be protected from
writes or replacement by unrelated accounts; these checks do not chmod or change
ACLs on an existing installation. Portable CI explicitly
skips that designated replay; a green hosted run does not attest to the Formula
installation or confer Windows CNG production eligibility.

No approved designated Formula manifest or complete Windows qualification run is
created by these changes. Existing numerical formula, workbook and historical
receipt identities are preserved. Changes to their authority require their own
reviewed qualification workflow.
