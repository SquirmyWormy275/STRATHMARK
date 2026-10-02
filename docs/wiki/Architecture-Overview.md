# Architecture Overview

The current separate Linux competition profile (STRATHMARK 3.0.0rc4 / STRATHEX 7.3.0) supports explicit engine choice, real Formula/ML, local signed approval and issue, settlement, restart, and later-round learning. See the [Linux competition runbook](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_COMPETITION.md). Windows CNG production qualification remains separate; the LLM council is unavailable in this local profile.

The [Linux numeric candidate](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_CANDIDATE.md) separately runs Formula, trained ML, pooling, and optimization for unissued previews. Approval, issue, settlement, next-round learning, and production qualification remain unavailable in that profile.

## Current authority status

V3.0.0rc3 is a release candidate that tracks all 232 in-repository
requirements. Core implementation and contract tests exist; full installed lifecycle composition and qualification remain unfinished. Its
checked-in development-key rehearsal is source-bound and does not change authority. V2
remains the globally trusted production authority, V3 is not production-eligible, and
V2 is not audit-only.

V2 and V3 are separate engines. V2 keeps its released prior-only model, ledger, and
shadow contract. V3 uses a new namespace, closed contracts, an append-only SQLite event
authority, rebuildable projections, rolling preparation, a formula/ML/LLM ensemble,
accuracy-earned credibility, consequence review, atomic issue, and field-atomic
settlement. V3 never rewrites V2 receipts or masquerades through V2's five keys.

The race-day path is sealed evidence and round epoch; independent formula, ML, and LLM
council; validation, capability, credibility, and pooling; green/amber/red consequence
review; fairness-frontier marks rebased to 3; then immutable receipt, approval, issue,
and settlement.

The V7 contract has 18 paths. It locks one V3 selection to a competition root, exposes
lifecycle and versioned snapshot synchronization, and records typed selected/excluded
approval decisions before the separate issue acknowledgment. The tournament manager
still owns authorization and official issue.

Before fields exist, V3 may sign marginal raw-time forecasts for seeding. Those receipts
have no field/stand facts and explicitly state `purpose=pre_field_seeding_only` and
`issued_mark=false`. After exact fields exist, complete-field assembly performs joint
optimization and creates the only V3 receipt that carries displayed marks.

This is the documented pivot from an earlier global-switch assumption: different
competition roots may select different eligible engines, while every child of one root
inherits one immutable numeric authority.

The factory runtime composes local automation and settled-evidence monitoring and has a
bounded separate-process CNG evaluator entrypoint. It deliberately requires injected
concrete family executors and settlement metrics; OS identity/ACL separation and CNG
provisioning belong to the installation and remain unproven until exact-source CI.

STRATHMARK authenticates a service principal. The tournament manager owns human login,
RBAC, official issue, results, publication, and payouts. Ollama, cloud, and the optional
archive may fail without becoming race-day authority.

See the canonical [architecture](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/ARCHITECTURE.md) and
[V3 engine contract](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/PREDICTION_ENGINE_V3.md).
