# Testing and Release Proof

V3 is an implemented release candidate under exact-source verification. Its older
rehearsal is stale after current source changes. V2 remains the trusted production
authority until explicit cutover. No production authority has changed. Tests and
verifiers never switch authority.

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

The preserved V2 suite uses another isolated database and remains authoritative evidence
for V2 behavior. Optional-provider live smokes are opt-in and cannot replace deterministic
contract fakes, temporal replay, or installed-wheel verification.

See [Deployment](Deployment.md) for the full gate.
