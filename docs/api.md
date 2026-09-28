# API reference

Fleetline exposes JSON REST endpoints under `/api/v1/`. The implementation-generated OpenAPI document is `/api/schema/`; Swagger UI is `/api/docs/`. This page documents cross-cutting contracts and the endpoint inventory that clients must follow.

## Authentication and tenant scope

Browser clients use Django session cookies and CSRF protection:

1. `GET /api/v1/auth/csrf/` establishes the CSRF cookie and returns `csrf_token`.
2. `POST /api/v1/auth/login/` with `{"username":"…","password":"…"}` establishes the session.
3. Include `X-CSRFToken: <token>` on unsafe session-authenticated requests.
4. `POST /api/v1/auth/logout/` closes the session.

Failed password and invalid one-time-code attempts are counted by both a hashed
account key and client address. Responses remain generic for known and unknown
accounts. Repeated client failures can return `429 login_throttled` with
`error.details.retry_after_seconds` and a matching `Retry-After` header; retry only
after that interval. A successful login resets the account failure tally. The
server trusts the rightmost forwarded client address only when
`TRUST_PROXY_HEADERS=1`, which is safe only behind the controlled reverse proxy.

System and integration administrators also send `otp` at login and cannot log in until MFA is configured. Non-browser integrations use an administratively issued scoped token:

```http
Authorization: Bearer <token>
Content-Type: application/json
```

Tokens are organization-bound, require an explicit future expiry, can be revoked,
and are stored by the server only as hashes. A token can use only the intersection
of its explicit scopes and its user's role permissions; wildcard scopes are not
accepted. Successful bearer authentication updates `last_used_at`. Every business
query is restricted to the authenticated organization; resource IDs from another
organization return no usable data.

AutoPi devices use `X-Device-Token`, not a human session or API bearer token, at the public ingestion endpoint.

## Errors, IDs, dates, and amounts

UUIDs are JSON strings. Timestamps are ISO 8601 with a timezone; date-only values use `YYYY-MM-DD`. Quantities and currency values are decimal strings in responses so clients do not lose precision.

Errors have one shape:

```json
{
  "error": {
    "code": "reason_required",
    "message": "A reason is required",
    "details": null
  }
}
```

Expected statuses include `200` success, `201` created, `202` accepted for ingest, `400` validation, `401` unauthenticated, `403` unauthorized, `404` absent or out of tenant scope, and `409` state/idempotency/concurrency conflict.

List endpoints currently return bounded named arrays rather than cursor pagination. Query filters are described below. Clients must not infer authorization from a hidden UI control; the server enforces every mutation.

## Idempotent mutations

Send a fresh client-generated UUID for every logical transactional action and retain it across network retries:

```http
Idempotency-Key: 3f61a7c7-16b7-4eb0-98e2-f2f7cb0acd61
```

The server scopes the key by organization, user, and route and stores the request fingerprint and response. An identical replay returns the stored result. Reusing the same key with different input returns `409 idempotency_conflict`.

The key is required for API-token issue/revoke; asset creation/update/status and
meter writes; attachment uploads; PM-to-work, inspection/defect/request/WO
transactional actions; task/labor updates; reservations/issues/returns/adjustments/counts;
PO transitions; receipts; and receipt reversal. Service-package and maintenance-plan creation
accept a key but currently allow an online request without one. Send the header on every
mutation for a consistent client contract.

## Core endpoints

| Methods and path | Purpose and important input |
|---|---|
| `GET /api/v1/auth/csrf/` | Establish CSRF cookie; public |
| `POST /api/v1/auth/login/` | `username`, `password`, privileged `otp`; public |
| `POST /api/v1/auth/logout/` | End current authenticated session |
| `GET /api/v1/auth/me/` | Current user, roles, permissions, and role navigation |
| `GET, POST /api/v1/users/` | List same-organization active users, optionally by `role`; an `admin.users` principal creates a user from `username`, `name`, `password`, `role_slugs`, and optional `default_location` |
| `POST /api/v1/users/{id}/mfa/` | Provision or rotate MFA for a same-organization privileged administrator; rotation requires `reason`; the secret and `otpauth_uri` are returned once, and replay returns only redacted metadata |
| `GET, POST /api/v1/api-tokens/` | An `admin.users` principal lists same-organization token metadata or issues a token for a same-organization active `user_id` using `name`, nonempty explicit `scopes`, and future timezone-aware `expires_at`; issue requires a UUID `Idempotency-Key` |
| `POST /api/v1/api-tokens/{id}/revoke/` | An `admin.users` principal revokes a same-organization token with optional `reason`; requires a UUID `Idempotency-Key` |
| `GET /api/v1/roles/` | List configured standard roles; `admin.users` only |
| `GET, POST /api/v1/locations/` | List active same-organization locations for administration, or create one with `name` and `code`; creation requires `admin.config` |
| `GET /api/v1/bootstrap/` | Offline bootstrap: permitted assets, assigned work, parts/stock subset, active inspection templates, conflicts, expiry, and a signed account-bound `offline_grant` for human sessions |
| `GET /api/v1/search/?q=…` | Permission-filtered assets, work orders, parts/alternates, and vendors; query requires two characters |
| `GET /api/v1/reports/operations/` | Role-filtered operational summary and source work-order records |
| `GET, POST /api/v1/attachments/` | List version lineages by `resource_type` + `resource_id`; or upload multipart `file`, `resource_type`, `resource_id`, `sensitivity`, optional `category`/`title`, and optional prior `supersedes_id` |
| `GET /api/v1/attachments/{id}/download/` | Authorized file download with no-sniff response |
| `GET, POST /api/v1/documents/` | List authorized technical-document revisions, optionally by `asset_id`/`category`; upload a reviewed-then-indexed PDF manual |
| `POST /api/v1/documents/{id}/approve/` | Record required security-review attestation and enqueue extraction for the newest quarantined revision |
| `GET /api/v1/documents/search/?q=…` | Full-text manual retrieval with authorized page excerpts and citations; optional `asset_id`, bounded `limit` |
| `GET /api/v1/documents/{id}/download/` | Authorized original-manual PDF download with no-sniff response |
| `GET, POST /api/v1/comments/` | List by `resource_type` + `resource_id`; or create with those fields and `body` |
| `GET, POST /api/v1/notifications/` | List current user's notifications; mark one read with `id` |
| `GET, POST /api/v1/webhooks/` | Manage subscriptions; create with `name`, HTTPS `url`, and `event_types`; secret is returned once |
| `POST /api/v1/webhooks/{id}/rotate-secret/` | Rotate a same-organization webhook signing secret with required `reason`; the replacement is returned once and an idempotent replay is redacted |
| `GET /api/v1/webhooks/deliveries/` | List up to 200 deliveries, optionally filtered by `pending`, `retry`, `delivered`, or `dead`, with the total dead-letter count |
| `POST /api/v1/webhooks/deliveries/{id}/retry/` | Requeue a same-organization `dead` or `retry` delivery; an idempotent mutation requiring webhook-management permission |
| `GET /api/v1/audit-events/` | Filter by `resource_type` and/or `resource_id`; audit-authorized roles only |
| `POST /api/v1/offline/sync/` | Synchronize at most 100 durable field operations |
| `POST /api/v1/import/assets/` | Multipart UTF-8 CSV in `file`, maximum 5,000 rows |
| `GET /api/v1/export/` | Download organization-scoped machine-readable JSON export |
| `POST /api/v1/users/{id}/disable/` | Disable same-organization user and revoke future offline activity; cannot disable self |

An issued API-token secret appears only in the first successful `201` response.
The server persists a redacted idempotent response; replaying the same request and
key returns the same token metadata with `secret_recoverable: false` and never
returns the secret. Listing also omits the token hash and secret. If the first
response is lost, revoke that token and issue another. Issue and revoke append
`api_token.issued` and `api_token.revoked` audit events; token metadata exposes
creation, expiry, last use, and revocation timestamps.

Accepted attachment content types are JPEG, PNG, WebP, PDF, plain text, and CSV, bounded by `ATTACHMENT_MAX_BYTES`. The server verifies common binary signatures, randomizes stored names, records SHA-256, and authorizes downloads. Every initial upload receives a `document_key` and version `1`. To replace a document, upload new bytes with the current attachment's `supersedes_id`; the server retains the same logical key, increments the version, and rejects cross-organization, cross-resource, branching lineage, or a sensitivity change. Omitted `category` and `title` inherit from the prior version. List results are grouped by `document_key`, ordered by version, and expose `supersedes_id`, visible `superseded_by_id`, `is_current`, and `sensitivity`. Stored attachment rows and bytes cannot be edited or deleted; offline staging permits only its audited one-time association with the synchronized record. Per-user recent staged uploads are bounded by `STAGED_ATTACHMENT_MAX_COUNT` and `STAGED_ATTACHMENT_MAX_BYTES`; overflow returns `429 staged_attachment_quota_exceeded`, and the deployment cleanup job removes expired unlinked staging after `STAGED_ATTACHMENT_TTL_HOURS`. The original version remains authorized and downloadable.

### Attachment sensitivity

`sensitivity` is immutable: `operational` is visible to callers otherwise authorized
for the linked resource; `financial` requires `financial.view` for both listing and
direct download. New generic Part, Purchase Order, and Receipt uploads require an
explicit `operational` or `financial` value (`400 attachment_sensitivity_required` or
`400 invalid_attachment_sensitivity` otherwise). Uploading a financial attachment
requires `financial.manage` (`403 financial_permission_denied`). Commercial legacy
attachments are migrated fail-closed as financial; do not silently reclassify one—after
review, retain it and upload an operational copy as a new lineage when appropriate.
Generic Vendor attachments are not yet an HTTP-supported target and remain denied.
Technical-document attachments are forced operational by a database guard and remain
available only through the separate document-library authorization contract below.

### Technical-document library contract

Technical manuals use a separate route and permission boundary from ordinary
attachments. `documents.view` permits published applicable manuals, page search,
and download only with source-asset access; `documents.manage` permits upload,
review, revision history, and source-asset access for document management. The
server always applies organization and source-asset access checks. A generic
attachment listing hides a technical document, and generic attachment
supersession returns `409 document_replacement_required` so that a manual must
retain its metadata and review history.

`POST /api/v1/documents/` is multipart, requires a UUID `Idempotency-Key`, and
accepts a PDF `file`, `asset_id`, `title`, and `category`; it optionally accepts
`manufacturer`, `model`, `engine_type`, `revision`, `source`, `license`, a JSON
`applicability` array, and `supersedes_document_id`. Each applicability item
allows only `asset_id`, `asset_type_id`, `make`, `model`, and `engine_type`; at
least one target must be present. A replacement retains its source asset and
attachment lineage. The response is `201` with `{ "document": ... }` and starts
in `quarantined` state.

`POST /api/v1/documents/{id}/approve/` requires a UUID `Idempotency-Key`, a
nonempty `review_note`, and a nonempty `security_review_reference`. It returns
`202` with the queued document and appends the worker request through the
transactional outbox. The terminal processing states are `indexed`,
`ocr_unavailable`, `needs_review`, and `failed`; `processing_detail` is a
bounded operator-facing explanation. `indexed` and `ocr_unavailable` are
published states. `ocr_unavailable` explicitly means that image-only pages
could not be OCRed because the configured local OCR runtime was unavailable; it
does not claim those pages are searchable.

`GET /api/v1/documents/search/?q=...` requires at least two query characters and
returns only pages from the caller's newest published, applicable document
revision. A result is shaped as:

```json
{
  "document_id": "uuid",
  "page_number": 17,
  "excerpt": "…torque procedure…",
  "citation": {
    "document_title": "M2 Service Manual",
    "document_key": "uuid",
    "version": 2,
    "page_number": 17,
    "attachment_id": "uuid",
    "download_url": "/api/v1/documents/uuid/download/"
  }
}
```

This endpoint is the supported grounded-retrieval boundary for a future
technician assistant: use the caller-filtered result and preserve its document
and page citation. Fleetline does not expose an LLM provider or an automatic
maintenance mutation through this API. See
[truck-document-library.md](truck-document-library.md) for extraction,
provenance, and OCR deployment details.

Asset import columns are `unit_number` (required), `asset_type` (default `Truck`), and `vin`; `location_code` remains optional for a future multi-yard rollout. The response reports created and rejected rows independently. Asset master data is the MVP's supported CSV import type; other authoritative records use their versioned API workflows until a real source-data contract and reconciliation rules are approved.

### Operational report contract

`GET /api/v1/reports/operations/` returns only the reporting domain authorized by the caller's effective session or bearer-token permissions. Multiple report permissions produce the union of their domains. Monetary metrics and their drilldowns additionally require `financial.view`; quantity and operational state remain available to the applicable operational role.

| Permission | Metrics and source records |
|---|---|
| `reports.shop` or `reports.all` | Asset count, out-of-service assets/events, overdue PM plans, and open work orders; current correction-leaf labor cost and net work-order part cost only with `financial.view` |
| `reports.executive` | Asset count, out-of-service assets, and out-of-service events; no work-order, labor, inventory, purchasing, or integration detail |
| `reports.inventory` | Part count plus on-hand and reserved quantities; net work-order part cost only with `financial.view`; no labor detail |
| `reports.purchasing` | Open purchase-order and posted-receipt counts; remaining open-order value only with `financial.view` |
| `reports.integration` | Active-device and accepted/quarantined message counts |

Every key in `summary` has a matching `drilldown` member containing `source_total`, `source_count`, and the complete contributing `sources` list. Each source carries a stable `type`, `id`, `label`, contextual `link`, and signed `contribution`. Cost and quantity totals are decimal strings. Net work-order part cost includes issues and compensating returns/reversals; corrected labor includes only the current leaf while preserving the full correction chain elsewhere. `source_records` remains a compatibility alias for open work-order sources and is populated only for shop/all reports.

### Organization export contract

`GET /api/v1/export/` requires both `export.all` and `financial.export` and returns schema version `1.0`, export time, an explicit organization scope whose `permissions` lists both grants, and deterministic ID-ordered records for:

- safe organization, location, user, and role data, including user-role relationships and each role's effective permissions;
- asset types, assets, status events, meters, every meter reading/correction, components, and component installation periods;
- service-package versions, plans/triggers, inspection templates/submissions/responses/findings, defects, alerts, requests, work orders/tasks/close snapshots, and labor history;
- parts/cross-references, warehouses/bins/balances, the stock ledger, reservations, counts, and count lines;
- vendors/vendor parts, purchase requests, purchase orders/lines, receipts/lines, and reversals;
- attachment metadata, technical-document metadata/applicability/page provenance and text, comments, notifications, audit events, and offline sync conflicts; and
- devices/associations plus raw messages and normalized telematics events.

Adding a record family to the export is additive and does not bump the export schema version; `schema_version` changes only when an existing family's shape changes.

The JSON export does not contain passwords, MFA secrets, API-token or device-token hashes, webhook signing secrets, sessions, idempotency records, delivery outbox state, or worker heartbeats. Attachment metadata includes its storage-relative file name and integrity digest, but not file bytes; use the documented backup process when a restorable database-and-media archive is required.

### Financial access

`financial.view` exposes monetary values and commercial terms only where the caller also has the relevant operational read permission. Without it, APIs retain part availability, quantities, bin addresses, work status, labor minutes, receipt quantities, and audit/action traceability but omit part cost, stock valuation, count-dollar values, labor rate/cost, PO/receipt line price, vendor payment terms, financial report metrics, and money-bearing audit context. Generic outbound webhook payloads also omit those fields.

`financial.manage` is required to set a part default cost, vendor payment terms, PO price, labor rate or correction, inventory valuation adjustment, and count approval. An approved PO's price is derived server-side when a permitted parts user posts a receipt; normal receipt, issue, and return operations therefore remain available to their operational roles without returning or accepting a price. `financial.export` is required in addition to `export.all` for the complete export.

Inspection-template questions and responses, and work-order-task measurements, are
operational JSON rather than a second cost ledger. Writes with recognized monetary
keys are rejected as `financial_payload_not_allowed`; legacy structured values are
recursively redacted without `financial.view`.

## Assets and meters

Base path: `/api/v1/assets/`.

| Methods and path | Purpose and important input |
|---|---|
| `GET, POST /` | List/filter by `status`, `q`, `include_archived=true`; create with `unit_number`, asset type ID/name, location/driver IDs, VIN/serial/year/make/model/ownership/specs/status |
| `GET, PUT /external/{source_system}/{external_id}/` | Look up or idempotently upsert an organization-bound external asset; writes require a scoped API token with `assets.sync`, accept bounded source-owned extension data in `source_details`, and return browser-safe asset and PM-scheduling links |
| `POST /external/{source_system}/{external_id}/meters/` | Append an external meter observation with required organization/source-wide permanent reading `external_id`, explicit kind/unit/value/time, and source provenance; writes require `assets.sync`; future or implausibly fast observations are retained as suspect and do not advance the current meter |
| `GET, PATCH /{asset_id}/` | Retrieve or update master fields; availability changes use the separate endpoint |
| `GET /{asset_id}/history/` | Status events, meters/readings, and audit details when permitted |
| `GET, POST /{asset_id}/meters/` | List; create a meter using `kind`, `name`, `unit`, optionally append `value`, timezone-aware `observed_at`, `external_id`, `reason` |
| `POST /meter-readings/{reading_id}/correct/` | Append superseding reading with `value` and required business `reason` |
| `POST /{asset_id}/availability/` | Append status event using `status`, `reason`, `classification`, optional `override_reason`; retirement also requires free-text `disposition` and `final_meter_reading_ids` containing the current accepted reading for every active odometer/hour meter |
| `GET, POST /{asset_id}/components/` | List every installation period on this asset, newest first; install using `kind` + `serial_number` (or an existing `component_id`), optional `manufacturer`, `model`, `installed_at`, `work_order_id`. A technician (`maintenance.execute`) must supply an open `work_order_id` on this asset where they are an active assignee; `assets.manage` or `maintenance.manage` may install without one |
| `GET /components/{component_id}/` | The component, all its installation periods, work-order tasks tagged with it, and the work orders that installed or removed it |
| `POST /components/{component_id}/remove/` | Close the open installation period using a required `reason`, optional `removed_at` and `work_order_id`. A component may be removed exactly once per period |

Component kinds are `engine`, `transmission`, `reefer_unit`, `apu`, `axle`, `aftertreatment`, and `other`. A serial is unique per organization and kind, and may be installed on only one asset at a time — the database enforces this, so a concurrent second install returns `409 component_installed_elsewhere` naming the asset it is on. Installation rows are append-only apart from the single removal fill; meter readings are frozen by value at install and removal and later corrections never rewrite them. A work-order task may carry an optional `component_id` (`409 component_not_on_asset` when that component is open on a different asset). See [ADR 0011](adr/0011-serviceable-component-tracking.md).

Asset status values are `Available`, `Restricted`, `OutOfService`, and `Retired`. Meter kinds are `odometer`, `engine_hours`, and `other`; reading quality is `accepted`, `suspect`, or `rejected`. Historical readings and status events are not edited in place.

For the GatorHub ownership, mapping, idempotency, roster, and webhook rules, see
[GatorHub integration and PM cutover](gatorhub-integration.md) and the versioned
[`gatorhub-fleetline-v1.json`](../contracts/gatorhub-fleetline-v1.json) contract.

## Maintenance, PM, inspections, and work

Base path: `/api/v1/maintenance/`.

| Methods and path | Purpose and important input |
|---|---|
| `GET, POST /service-packages/` | List/version package; create with `name`, `description`, task list, expected-parts list, expected labor minutes |
| `GET, POST /plans/` | Filter by `asset_id`; create with `asset_id`, `service_package_id`, `name`, typed trigger list |
| `POST /plans/recalculate/` | Recalculate all active plans or one `plan_id` |
| `POST /plans/{id}/create-work-order/` | Create planned WO; optional assignment, summary, priority, QC, target date |
| `GET, POST /inspection-templates/` | List active/all; create next named version with unique question IDs and optional retention metadata |
| `GET, POST /inspections/` | Filter by `asset_id`; submit/draft with asset/template IDs, responses, acknowledgment, `submit`, optional voided `replaces_id` |
| `GET /inspections/{id}/` | Retrieve permitted exact submitted/template snapshot |
| `POST /inspections/{id}/void/` | Void immutable submission with `reason`; replacement is a new inspection |
| `GET, POST /defects/` | Filter by asset; create with asset, category, description, severity, safety flag |
| `POST /defects/{id}/transition/` | Supervisors move to an allowed `status` with a required reason where applicable; the technician assigned to the linked active WO may move only to `Corrected`, supplying `repair_details` (or reason) and optional same-defect `evidence_attachment_ids`; the response includes `repair_evidence` |
| `POST /defects/{id}/request/` | Acknowledge as needed and create one active linked maintenance request |
| `GET, POST /requests/` | Filter by status; create with asset, summary, description, priority |
| `POST /requests/{id}/transition/` | Triage/approve/defer/reject/close/reopen according to state rules; reasons enforced |
| `POST /requests/{id}/work-order/` | Convert approved request to linked work; optional package, assignment, priority, QC, target date |
| `GET, POST /work-orders/` | Filter by status/asset; create direct authorized work order |
| `GET, PATCH /work-orders/{id}/` | Retrieve tasks/labor and ordered immutable `close_snapshots`, or edit permitted active fields using role rules; PATCH requires `Idempotency-Key` and the last observed positive `base_version`, and stale versions return `409 sync_conflict` |
| `POST /work-orders/{id}/transition/` | State transition with `status`, reason/summary and optional `completion_meter_id` |
| `GET, POST /work-orders/{id}/tasks/` | List; supervisors add title/instructions/required/sequence with required `Idempotency-Key`; omitted sequence is assigned under the work-order lock |
| `GET, PATCH, POST /work-orders/{id}/tasks/{task_id}/` | Read/update status, notes, measurement, and optimistic `base_version` |
| `GET, POST /work-orders/{id}/labor/` | List immutable history plus current leaf totals; append minutes/rate/note/timestamps, or a supervisor correction linked by `corrects_id` with a reason |
| `GET /alerts/` | List human-reviewable alerts, optionally filtered by status |
| `POST /alerts/{id}/transition/` | Review/suppress/dismiss/resolve/convert; suppress/dismiss requires reason; conversion creates a request |

Inspection states are `Draft`, `InProgress`, `Submitted`, `Voided`. Defect states are `Open`, `Acknowledged`, `Deferred`, `InRepair`, `Corrected`, `Verified`, `Closed`. Request states are `Submitted`, `Triaged`, `Approved`, `Deferred`, `Rejected`, `Converted`, `Closed`. Work-order states are `Draft`, `Ready`, `InProgress`, `Blocked`, `QC`, `Completed`, `Closed`, `Cancelled`, `Reopened`; invalid transitions return `409`. Priorities are `low`, `normal`, `high`, and `safety`.

## Parts and inventory

Base path: `/api/v1/inventory/`.

| Methods and path | Purpose and important input |
|---|---|
| `GET, POST /parts/` | Search `q` across number/name/manufacturer/barcode/alternates, or use `identifier` for case-insensitive exact part/manufacturer/barcode/alternate/vendor lookup; create number/name and optional cross-references/cost metadata |
| `GET /parts/{id}/history/` | Part aliases, per-bin balances, reservations, and immutable transactions |
| `GET, POST /warehouses/` | List; create from `location_id`, `code`, `name` |
| `GET, POST /bins/` | Filter by warehouse; create from `warehouse_id`, `code`, `name` |
| `GET /bins/{id}/history/` | Immutable transaction history for a same-organization bin, subject to work-order access |
| `GET /stock/` | Filter by part/bin; returns current projection and ledger reconciliation errors |
| `GET, POST /reservations/` | Filter by WO; reserve part/bin/WO/quantity or release with `action=release`, reservation ID and reason |
| `GET, POST /issues/` | List issues; append issue from a reservation or part/bin/WO/quantity/unit cost/reason |
| `GET, POST /returns/` | List returns; append against `original_transaction_id`, quantity, reason |
| `GET, POST /adjustments/` | List corrections; append part/bin quantity adjustment or reverse `original_transaction_id`, always with reason |
| `GET, POST /counts/` | List; an `inventory.count` or `inventory.adjust` user captures warehouse lines of part/bin/counted quantity and reason; returns `PendingApproval` without changing stock when the configured gross value threshold applies |
| `POST /counts/{id}/approve/` | An `inventory.adjust` user other than the submitter approves and atomically posts a pending count; requires an idempotency key and returns `409 count_balance_stale` if stock changed after capture |

Count states are `Draft`, `PendingApproval`, and `Posted`; `Draft` is transactional and normally not externally visible. No-variance counts and counts submitted by an `inventory.adjust` user post immediately. Other nonzero discrepancies use the `INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD` snapshot and remain ledger-neutral until approval. Reservation states are `Pending`, `Active`, `PartiallyIssued`, `Fulfilled`, `Released`, `Expired`. Stock transaction types are `RECEIPT`, `ISSUE`, `RETURN`, `ADJUSTMENT`, `COUNT_ADJUSTMENT`, and `REVERSAL`. `StockBalance` is read-only through the API and is verified against the ledger; final-unit operations use PostgreSQL locking.

## Purchasing and receiving

Base path: `/api/v1/purchasing/`.

| Methods and path | Purpose and important input |
|---|---|
| `GET, POST /vendors/` | List; create code/name and optional contact/address/payment terms |
| `GET, POST /purchase-requests/` | List; create from part, positive quantity, reason, optional needed date |
| `POST /purchase-requests/{id}/transition/` | Approve/reject using `target` and a different authorized approver, or cancel by requester/manager; rejection and cancellation require `reason` |
| `GET, POST /purchase-orders/` | List with lines/receipts; create vendor, optional number/expected date/emergency/notes, and part/quantity/unit-cost lines |
| `POST /purchase-orders/{id}/transition/` | Move using `target` (or `status`) and reason; approval requires approval permission and policy threshold |
| `GET, POST /receipts/` | List; post PO lines with actual quantity and destination bin plus optional packing slip |
| `POST /receipts/{id}/reverse/` | Append reversal and compensating stock movements with `reason` |

PO states are `Draft`, `Submitted`, `Approved`, `Sent`, `PartiallyReceived`, `Received`, `Closed`, `Cancelled`. Partial receipts update only quantities actually received and leave line balances open. Receipts are `Posted` or `Reversed` after posting; they are never deleted.

## Devices and AutoPi ingestion

Base path: `/api/v1/integrations/`.

| Methods and path | Purpose and important input |
|---|---|
| `GET, POST /devices/` | Integration-admin list/register; create name, serial, optional external ID/vendor/model; `Idempotency-Key` required and ingest token returned only on the first response |
| `POST /devices/{id}/associate/` | Idempotent time-bounded asset association using asset ID and timezone-aware effective dates; `Idempotency-Key` required |
| `POST /devices/{id}/rotate-token/` | Immediately replace the ingest token; `Idempotency-Key` required and the new token is returned only on the first response |
| `POST /devices/{id}/status/` | Enable or disable ingestion with `status=active|disabled`; `Idempotency-Key` required |
| `POST /telematics/autopi/v1/messages/` | Public adapter authenticated by `X-Device-Token`; accepts canonical AutoPi envelope |
| `GET /data-quality/` | Integration report: device/message counts, suspect events, and exception details |

Canonical meter payload:

```json
{
  "schemaVersion": "1.0",
  "messageId": "autopi-message-0001",
  "organizationId": "4e8979ee-3afe-43a2-aed0-5be42ccd981f",
  "deviceId": "device-external-id",
  "observedAt": "2026-09-03T14:00:00Z",
  "sentAt": "2026-09-03T14:00:01Z",
  "sequence": 1,
  "source": "autopi",
  "type": "telemetry",
  "values": {
    "odometer": {"value": "128442.3", "unit": "mi"},
    "engineHours": {"value": "8931.4", "unit": "h"}
  }
}
```

The authenticated device must match both organization and `deviceId` in the body. Odometer accepts `mi` or `km`; engine hours accepts `h`, `hr`, or `hours`. Equivalent normalized readings are idempotent; reusing a non-null sequence for different content returns `409 sequence_conflict`. Rejected and quarantined raw evidence does not reserve a deduplication key, so the same source message can be accepted after its identity, timestamp, or association is corrected. Authentic late messages remain in history but cannot replace a newer current reading; decreasing or implausible readings become suspect/quarantined.

## Offline synchronization

Human-session clients must send the signed bootstrap grant as `X-Offline-Grant` on the sync request and on staged attachment uploads. Grants are user- and organization-bound, expire with the configured offline window, and cannot be used with API bearer tokens. After reconnecting, a newly issued grant may reauthorize saved work only for the same user and organization; account switching never transfers queued work.

The body contains at most 100 operations. Each has a stable UUID, type, and object payload:

```json
{
  "operations": [
    {
      "operation_id": "a22cd009-f122-4359-bd58-a017600295c7",
      "type": "defect.create",
      "payload": {
        "asset_id": "41b19346-2a80-41be-9714-866311143816",
        "category": "tires",
        "description": "Left steer tire sidewall damage",
        "severity": "safety",
        "safety_related": true
      }
    }
  ]
}
```

Supported types are `defect.create`, `inspection.submit`, `work_note.create`, `task.complete`, `stock.issue`, and `stock.return`. Each result is `synced`, `conflict`, or `rejected`; a conflict includes a human-readable message and current server detail. Replaying the same unchanged operation returns one logical result. Attachments synchronize through the normal multipart attachment endpoint while the client retains their bytes and operation linkage until acknowledged.

## Webhook delivery

The database-backed worker posts event envelopes containing `id`, `schema_version`, `type`, `organization_id`, `occurred_at`, `resource`, and `data`. It signs the exact request body with HMAC-SHA-256:

```http
X-Fleetline-Signature: sha256=<lowercase-hex-digest>
```

Receivers must compute the HMAC over the raw body, compare in constant time, reject replays by event ID, and return a 2xx status only after durable acceptance. Delivery backs off and becomes dead after eight failed attempts. HTTP and private-network targets remain disabled unless explicitly configured.
