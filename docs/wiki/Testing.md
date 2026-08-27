# Testing and Release Proof

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. Repository implementation and audit are complete for this candidate. Its
checked-in development-key rehearsal is source-bound and must pass the release verifier. V2 remains the trusted production
authority until explicit cutover. No production authority has changed. Tests and
verifiers never switch authority.

Run V3 tests and release proof only in the designated Python 3.13 environment with the
exact V3 release lock installed. The package and V2 compatibility matrix remains Python
3.10-3.13; those older interpreters intentionally exclude `tests/v3`. Enabling SQLite
`trusted_schema` to make an older bundled SQLite accept V3's expression indexes is not a
valid compatibility fix.

Every run must use isolated V2 and V3 databases and a unique pytest base directory:

    $env:STRATHMARK_TEST_DB = '1'
    $env:STRATHMARK_DB_PATH = "$PWD\.tmp\wiki-v2.sqlite3"
    $env:STRATHMARK_V3_DB_PATH = "$PWD\.tmp\wiki-v3.sqlite3"
    python -m pytest tests/v3 -q --basetemp .tmp/wiki-v3 -p no:cacheprovider

The V3 suite covers contracts, canonicalization, event authority, migrations,
formula/ML/LLM assessors, capability, credibility, pooling, disagreement, optimizer,
rolling preparation, approval, issue, settlement, factory, API/security, recovery,
cutover, documented examples, and installed artifacts.

Whole-domain proof is:

    python scripts/replay_v3.py
    python scripts/run_v3_release_evidence.py --local-model qwen3.5:9b --local-model ministral-3:8b
    $source = (git rev-parse HEAD).Trim()
    python scripts/verify_v3_release.py --evidence benchmarks/v3/v3_executable_evidence.json --emit-rehearsal $source --output-attestation benchmarks/v3/v3_release_attestation.json
    python scripts/verify_v3_release.py
    python scripts/verify_v3_release.py --require-production

The evidence runner requires an unchanged committed tree and pinned installed models;
it builds and installs the exact wheel. The ordinary verifier rejects missing, stale,
failed, substituted, or tampered proof. After a fresh rehearsal is emitted, the ordinary
verifier passes and the last command must fail with production_attestation_required. A
production pass requires a separate CNG-backed artifact and still does not perform the
consumer switch.

Factory tests prove the local composition/scheduler and bounded evaluator exchange. They
do not prove production family executors, authoritative local settlement metrics, OS
identity/ACL separation, provisioned CNG keys, or CI execution; final evidence must use
those real installed components.

The separate post-format result-to-ready manifest records five completed Windows trials
and a 3.414-second maximum against the 120-second limit. It retains exact source digests
and per-component timing as one component of the exact-wheel release evidence.

The preserved V2 suite uses another isolated database and remains authoritative evidence
for V2 behavior. Optional-provider live smokes are opt-in and cannot replace deterministic
contract fakes, temporal replay, or installed-wheel verification.

See [Deployment](Deployment.md) for the full gate.
