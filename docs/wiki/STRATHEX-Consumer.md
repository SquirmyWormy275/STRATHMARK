# STRATHEX Consumer Contract

## Current authority status

V3.0.0rc1 is a release candidate that tracks all 232 in-repository
requirements. STRATHMARK repository implementation and audit are complete for this
candidate. An installed-adapter rehearsal remains required; the STRATHMARK
development-key rehearsal is source-bound and cannot prove the external consumer. V2
remains the globally trusted production authority. V3 is not production-eligible, and
V2 is not audit-only.

For V2-selected competition roots, STRATHEX continues to use the frozen V2 shadow
contract. The V3 adapter is
a separate dependency pinned to one exact commit, wheel, OpenAPI digest, release
evidence digest, and service identity. STRATHEX owns human authentication/RBAC, roster,
schedule, issue permissions, official results, publication, and payouts. STRATHMARK owns
numeric evidence, field receipts, and settlement evidence.

The V3 workflow opens one deliberately selected competition root, freezes the round,
prepares rolling cards early, obtains signed field-independent seed times when fields do
not yet exist, then assembles each complete exact field against one
same-round epoch, surfaces normal green/amber sheets for ordinary batch approval and red
or degraded sheets for the appropriate deliberate lane, records exact selected and
excluded receipt approvals, acknowledges issue separately and atomically, settles the
complete issued roster atomically, closes all seven derivation reactions without
inventing an approval decision, and advances evidence only at the next round boundary.
Displayed marks are never copied between fields.

Pre-field receipts have `purpose=pre_field_seeding_only` and `issued_mark=false`. They
may order or group competitors, but they are not start sheets. Exact fields and stands
must first exist and be synchronized before `/v3/fields/assemble` may produce marks.

A standalone event selects V2 or V3 once. A tournament selects once during creation and
all child events and rounds inherit; child selectors are forbidden. Different roots may
choose different eligible engines, but one root never mixes or silently falls back.

STRATHMARK's frozen V6 contract now contains all 18 numeric lifecycle paths. The external
STRATHEX installation must still prove its exact dependency pin, durable outbox,
immutable local acknowledgments, and restart behavior before V3 production eligibility.

A production-CNG-signed pre-switch handoff still declares V2 current and requires a
separate release authorization before V3 can become an eligible choice. It is not a
global engine selection. See the canonical
[consumer migration](../STRATHEX_CONSUMER_MIGRATION.md).
