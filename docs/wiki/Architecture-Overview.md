# Architecture Overview

## Current authority status

V3 is an implemented release candidate under exact-source verification. Its older
rehearsal is stale after current source changes. V2 remains the trusted production
authority until an explicit cutover. No production authority has changed, no consumer
endpoint has switched, and V2 is not audit-only.

V2 and V3 are separate engines. V2 keeps its released prior-only model, ledger, and
shadow contract. V3 uses a new namespace, closed contracts, an append-only SQLite event
authority, rebuildable projections, rolling preparation, a formula/ML/LLM ensemble,
accuracy-earned credibility, consequence review, atomic issue, and field-atomic
settlement. V3 never rewrites V2 receipts or masquerades through V2's five keys.

The race-day path is sealed evidence and round epoch; independent formula, ML, and LLM
council; validation, capability, credibility, and pooling; green/amber/red consequence
review; fairness-frontier marks rebased to 3; then immutable receipt, approval, issue,
and settlement.

STRATHMARK authenticates a service principal. The tournament manager owns human login,
RBAC, official issue, results, publication, and payouts. Ollama, cloud, and the optional
archive may fail without becoming race-day authority.

See the canonical [architecture](../ARCHITECTURE.md) and
[V3 engine contract](../PREDICTION_ENGINE_V3.md).
