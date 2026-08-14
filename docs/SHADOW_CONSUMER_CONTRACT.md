# Trusted Shadow Consumer Contract

The frozen local consumer contract is packaged at
`strathmark/contracts/shadow_consumer_v1.openapi.json`. It is an OpenAPI 3.1
document covering the complete versioned STRATHMARK boundary consumed by
Missoula: service health, calculate/recover, receipt lookup, current status,
numeric settlement/void, bounded mirror replay, and advisory drift.

The sibling `.sha256` file pins the canonical JSON representation. Python
consumers should use `load_shadow_consumer_contract()` and
`shadow_consumer_contract_digest()` instead of reading package paths directly.
Both functions fail closed if the installed document is malformed, has the wrong
contract version, exposes a different route set, or no longer matches the reviewed
digest. The current reviewed SHA-256 is
`c4af9a4ce286c66c07b43cee6951cfe7556fe35c266ad250b5b7d85e58d7e358`.

`scripts/freeze_shadow_consumer_contract.py` deterministically regenerates the
canonical artifact and checksum. The response contract is closed at every fixed
object boundary, including receipt cores, predictions, evidence, ledger state,
numeric revisions, drift, and health. Only explicitly keyed diagnostic and drift
cohort maps accept dynamic keys, and their values remain schema constrained.

The request boundary matches the live service limits: wood diameter is 225 through
500 millimeters inclusive, numeric actual time is positive and no greater than 300
seconds, and every baseline drift residual is between -300 and 300 seconds
inclusive.

## Authority and privacy

- STRATHMARK owns numeric predictions, immutable receipt cores, live numeric
  settlement/void revisions, and the payload-free monitoring projection.
- Missoula owns preparation, human authorization, lifecycle approval, complete
  operational outcomes, prospective observations, official results, standings,
  points, publication, and payouts.
- The examples use pseudonymous namespaced identifiers. Display names, contact
  data, medical information, and free-text observations are outside this contract.
- Cloud mirroring is optional and off the trust path. `not-configured` and pending
  delivery do not make a locally recorded receipt untrusted.

## Consumer sequence

1. Refresh and attest a verified local evidence snapshot for the explicit UTC
   cutoff.
2. Prepare one ordered whole-field request with stable namespaced identities.
3. Look up the receipt before calculating; calculate only when lookup returns 404.
4. Persist the returned `core_json` byte-for-byte. Treat `status` as a live
   projection, not part of the immutable core.
5. Review or issue only when trust is `recorded`, freshness is `current`, and
   readiness is true. Official championship authority remains unchanged.
6. Submit only eligible positive raw elapsed times, or explicit void revisions,
   using optimistic numeric revision numbers and stable Missoula outcome IDs.
7. Read status after ambiguous timeouts. Never blindly recalculate or repeat a
   correction with a new identity.

Exact retries survive process restart and artifact upgrades because receipt lookup
precedes provider loading. A new request/run revision whose only material change is
the prospective observation fingerprint produces a distinct receipt while retaining
the same active numeric input fingerprint, calculation input, and numeric output.
Reusing the original caller/request/run identity with a changed observation remains
an idempotency conflict and returns HTTP 409.

## Offline rehearsal

`tests/test_shadow_consumer_contract.py` is the executable reference rehearsal. It
removes ambient database/cloud variables, uses a temporary SQLite file and an
in-process verified evidence adapter, then exercises calculate, context invariance,
restart recovery under an intentionally unusable upgraded provider, lookup, live
status, settle, void, health, mirror replay, and advisory drift. The same test
validates every packaged request/response example against its JSON Schema and the
live Pydantic request models. It also validates actual responses from all seven
routes, injects unreviewed fields at deep response boundaries to prove they are
rejected, and exercises adversarial values immediately inside and outside each
numeric boundary.

The CI-installed distribution path, `scripts/smoke_installed_distribution.py`,
loads the contract from the installed package, verifies its checksum, and asserts
the exact route set from outside the checkout for both wheel and source
distribution jobs. Its `--offline` option supports the same check without package
index access when dependencies are already available locally.

This proves the local contract only. It does not prove production durability,
hosted Supabase behavior, deployment, secrets, or official-handicap authority.
