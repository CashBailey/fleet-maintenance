# ADR 0008: Financial data is a separately authorized view

- Status: accepted
- Date: 2026-09-04

Monetary values and sensitive commercial terms are not implied by operational access.
Fleetline therefore has three explicit permissions: `financial.view`,
`financial.manage`, and `financial.export`. Purchasing managers and fleet managers
receive view/manage access; management receives view access; only the fleet-manager
role receives the deliberately broad export permission. A scoped API token must carry
the same permission explicitly.

Public serializers default to operational data only. Part cost, stock valuation,
count variance value, labor rate/cost, work-order cost projections and close snapshots,
vendor payment terms, PO/receipt prices, financial report metrics, and money-bearing
audit context are returned only to `financial.view`. Generic outbound webhooks always
redact money fields. The immutable facts remain complete in PostgreSQL; redaction is a
presentation and integration boundary, not a rewrite of history.

Structured operational JSON is not an alternate cost ledger. Inspection-template
questions and responses, and work-order-task measurements, reject recognized monetary
keys on write; legacy structured values are recursively redacted when the caller lacks
`financial.view`.

`financial.manage` is required to set a part cost, vendor payment terms, PO line price,
labor rate or correction, inventory valuation adjustment, or count approval. Routine
quantity operations remain role-based: a technician can issue/return a part and a parts
clerk can receive an approved PO because neither client supplies or changes the derived
price. This preserves the required shop workflows without making cost visible.

Opaque files use immutable `Attachment.sensitivity` (`operational` or `financial`).
New generic Part, Purchase Order, and Receipt uploads must explicitly classify the file;
financial files require `financial.manage`, and only `financial.view` can list or
download them. Operational specifications and manuals remain available through their
ordinary resource authorization. Legacy commercial attachments are migrated to
`financial` rather than guessed from a filename; after review, an operational copy is
uploaded as a new lineage so the original classification remains auditable. Technical
documents are always operational attachments and retain their separate
`documents.view`/source-asset boundary.

The full organization export requires both `export.all` and `financial.export`. A
future non-financial operational export must be designed as a separate, explicitly
redacted contract rather than weakening this boundary.
