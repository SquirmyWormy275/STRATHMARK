# TODOs and Operational Follow-up

V3 is implemented and under exact-source release verification. The older checked-in
rehearsal is stale after the current source changes. V2 remains the trusted production
authority until an explicit cutover. No ordinary V3 implementation item is hidden here
as if it were production evidence.

## Separately authorized production work

These are installation, integration, and authority decisions—not missing shortcuts that
the code silently takes:

- Provision installation-owned non-exportable Windows CNG identities for candidate,
  audit, release, backup, support, and handoff roles; record only public identities and
  key names outside the repository.
- Run the complete production-tier evidence set on the designated installed Windows
  host and create the exact CNG-signed release attestation. Pin the public release
  identity independently and pass it to the verifier with
  `--trusted-production-identity`; do not accept identity metadata from the attestation
  itself. Do not promote the checked-in development-key rehearsal.
- Implement and rehearse the pinned tournament-manager V3 adapter against the frozen
  OpenAPI digest while V2 remains authoritative.
- At zero open tournaments, prepare the signed final-V2/V3 handoff, obtain separate
  release authorization, and switch the consumer exactly once. Never enable concurrent
  V2/V3 trusted writers.
- After cutover, operate the signed backup/recovery and model-factory lifecycle; collect
  prospective settled evidence and reassess accuracy, calibration, equity, capacity, and
  provider choices through new versioned manifests.

Existing V2 PostgreSQL/Supabase migrations remain separate optional mirror work. Apply
them only through their own authorized runbook if the V2 shadow deployment still needs
them. They do not initialize or qualify V3.

## Preserved research boundary

New event/material properties, provider candidates, formula coefficients, ML features,
prompts, priors, thresholds, and optimizer objectives enter only through the versioned
factory, causal replay, signed promotion, and contract process. Do not edit a production
bundle in place or reopen a locked benchmark for tuning.

## Closed historical backlog

- The pre-2.0 first-valid cascade, name/date ledger, fixed 65/80/90/97 same-tournament
  blend, and hidden ML/LLM selection are retired.
- V2 replaced that architecture with one prior-only core and a strict residual gate.
- V3 is a separate blind formula/ML/LLM ensemble with immutable component forecasts,
  accuracy-earned weights, tournament epochs, dual-state capability, and event authority.
- V3 does not reactivate the old cascade or rewrite V2 receipts.

Historical rationale remains under `docs/solutions/`; current V3 behavior and gates are
in [`docs/PREDICTION_ENGINE_V3.md`](docs/PREDICTION_ENGINE_V3.md) and
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
