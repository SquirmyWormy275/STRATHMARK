# STRATHEX Consumer Contract

## Current authority status

V3 is an implemented release candidate under exact-source verification. The older
installed-adapter rehearsal is stale after current source changes. V2 remains the trusted
production authority until an explicit cutover. No production authority has changed, no
STRATHEX endpoint has switched, and V2 is not audit-only.

Until cutover, STRATHEX continues to use the frozen V2 shadow contract. The V3 adapter is
a separate dependency pinned to one exact commit, wheel, OpenAPI digest, release
evidence digest, and service identity. STRATHEX owns human authentication/RBAC, roster,
schedule, issue permissions, official results, publication, and payouts. STRATHMARK owns
numeric evidence, field receipts, and settlement evidence.

The V3 workflow prepares rolling cards early, assembles each complete field against one
same-round epoch, surfaces green sheets for batch approval and amber/red sheets for
individual deliberate action, acknowledges issue atomically, settles the complete issued
roster atomically, closes all seven derivation reactions without inventing an approval
decision, and advances evidence only at the next round boundary. Displayed marks are
never copied between fields.

A production-CNG-signed pre-switch handoff still declares V2 current and requires a
separate release authorization. See the canonical
[consumer migration](../STRATHEX_CONSUMER_MIGRATION.md).
