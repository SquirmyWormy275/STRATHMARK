# STRATHEX Consumer Contract

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements; implementation is under final audit. The older installed-adapter rehearsal
is stale until regenerated from the final documentation commit. V2 remains the trusted
production authority until an explicit cutover. No production authority has changed, no
STRATHEX endpoint has switched, and V2 is not audit-only.

Until cutover, STRATHEX continues to use the frozen V2 shadow contract. The V3 adapter is
a separate dependency pinned to one exact commit, wheel, OpenAPI digest, release
evidence digest, and service identity. STRATHEX owns human authentication/RBAC, roster,
schedule, issue permissions, official results, publication, and payouts. STRATHMARK owns
numeric evidence, field receipts, and settlement evidence.

The V3 workflow prepares rolling cards early, assembles each complete field against one
same-round epoch, surfaces normal green/amber sheets for ordinary batch approval and red
or degraded sheets for the appropriate deliberate lane, records exact selected and
excluded receipt approvals, acknowledges issue separately and atomically, settles the
complete issued roster atomically, closes all seven derivation reactions without
inventing an approval decision, and advances evidence only at the next round boundary.
Displayed marks are never copied between fields.

STRATHMARK's typed `POST /v3/approvals/decide` endpoint now exists. STRATHEX's durable
outbox forwarder and immutable local acknowledgment persistence are not implemented;
they remain a cutover blocker.

A production-CNG-signed pre-switch handoff still declares V2 current and requires a
separate release authorization. See the canonical
[consumer migration](../STRATHEX_CONSUMER_MIGRATION.md).
