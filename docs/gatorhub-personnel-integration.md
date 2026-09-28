# GatorHub personnel and assignment boundary

## Current status

Fleetline implements the receiving projection API and append-only work-order team
assignment records. GatorHub does **not** yet implement the publisher, reconciliation
job, secret configuration, or synchronization status surface. No live personnel sync is
claimed or enabled by this document.

The versioned wire contract is
[`contracts/gatorhub-fleetline-v1.json`](../contracts/gatorhub-fleetline-v1.json). The
architectural decision is recorded in
[`ADR 0009`](adr/0009-gatorhub-personnel-projection.md).

## Authority and identity

GatorHub is the future authority for the limited personnel attributes it publishes.
Fleetline remains authoritative for its login accounts, permissions, work-order team
changes, work execution, and audit records.

| Concept | Stable identity | Rule |
| --- | --- | --- |
| GatorHub person | `Employee.id` UUID | Stored as `(Fleetline organization, "gatorhub", external_employee_id)` |
| Optional GatorHub login | `User.id` UUID | Metadata only; never the person identity and never a Fleetline credential |
| Fleetline login | Fleetline `User.id` UUID | Created and authorized locally; never created by personnel projection |
| Assignment fact | Fleetline `WorkOrderAssignment.id` UUID | Append-only assignment or unassignment event |

Names, email addresses, usernames, badge numbers, and job titles are not cross-system
identifiers. A GatorHub `Employee` without a `User` remains a valid projected person.
Fleetline does not ingest passwords, JWTs, MFA secrets, email, phone, payroll data, or
GatorHub permission claims through this API.

## Service-to-service topology

The publisher must run on the GatorHub server. It uses a Fleetline-issued opaque,
expiring API token stored only in GatorHub server secret configuration. Fleetline stores
only the token hash. The token must never enter browser JavaScript, local storage, a URL,
client logs, or GatorHub's database exports.

For personnel-only synchronization, grant exactly `personnel.sync`. If one GatorHub
adapter also reconciles trucks, its combined token may contain only `assets.view`,
`assets.sync`, and `personnel.sync`. Do not grant `pm.manage`, `maintenance.manage`, or
`admin.users`. Do not reuse GatorHub's human HS256 JWT, share either application's
signing secret, share cookies, or give either application access to the other's database.

Fleetline and GatorHub remain separate applications on separate origins. Human PM and
work-order actions use the user's Fleetline session and Fleetline authorization, not the
service token.

## Personnel projection API

```text
PUT /api/v1/maintenance/personnel/external/gatorhub/{Employee.id}/
Authorization: Bearer <Fleetline service token>
Idempotency-Key: <UUID>
```

The JSON body is strict: unknown fields are rejected.

| Field | Required | Meaning |
| --- | --- | --- |
| `display_name` | yes | GatorHub employee display name, 1-200 characters |
| `active` | yes | Eligible for new maintenance assignment in this projection |
| `source_version` | yes | Opaque source version or canonical-payload hash, 1-160 characters |
| `source_updated_at` | yes | Timezone-aware source change timestamp |
| `external_user_id` | no | Linked GatorHub `User.id`; metadata only |
| `job_title` | no | Display metadata, at most 160 characters |
| `department` | no | Display metadata, at most 160 characters |

`active=true` means the employee is both active in GatorHub and included by an
organization-approved maintenance-assignment rule or allowlist. It is not a Fleetline
role grant. When either condition stops being true, GatorHub must send `active=false`.
An inactive projection is excluded from new assignments but remains referenced by
historical assignment facts. Existing open work orders require manager review; Fleetline
does not silently remove their team members. Missing rows in a partial or failed run
must never be interpreted as inactive.

Every logical update uses a stable UUID `Idempotency-Key`, preferably UUIDv5 over the
source employee UUID plus a canonical payload hash. An identical retry replays the first
response. Reusing a key with different input returns `409`. An older
`source_updated_at` returns `409`; an equal timestamp with different projected content
also returns `409`; identical current content returns `200` with `changed=false`.
GatorHub should persist failed operations and retry them with the original key.

## Work-order assignment behavior

Fleetline maintenance managers replace the active team through:

```text
POST /api/v1/maintenance/work-orders/{work_order_id}/assignments/
```

This endpoint requires a Fleetline human session, `maintenance.manage`, a UUID
`Idempotency-Key`, and the work order's current `base_version`. A team may contain at
most one `lead` and any number of `technician` entries. Each entry identifies either a
local Fleetline user or an active external projection, never both. Duplicate people,
inactive people, cross-organization references, stale versions, and changes to completed,
closed, or cancelled work orders are rejected.

Team replacement appends only the assignment and unassignment deltas. Previous event
rows remain immutable and auditable. The legacy `assigned_to` field remains a temporary
compatibility projection of a local lead.

An external projection is scheduling and display identity only. It cannot sign in or
execute work. A technician who uses Fleetline must have a separately provisioned local
Fleetline account and must be assigned as that local user. Projecting `User.id` does not
link or merge accounts automatically.

Each assignment event freezes the assignment-time display name and, for an external
person, the source system, `Employee.id`, and source version. A compensating
unassignment carries the original assignment snapshot. Later GatorHub renames do not
rewrite existing history; a later reassignment captures the new name and version.
`external_user_id`, job title, and department remain current projection metadata and are
deliberately not copied into assignment history. Migration `0008` freezes the
best-available identity for events created before snapshot fields existed; that backfill
must not be represented as proof of the person's original historical display name.

## Upstream blockers and activation gate

Live synchronization must remain disabled until GatorHub provides all of the following:

1. Server-only Fleetline base URL and secret-token configuration, an outbound client,
   timeouts, reconciliation command or job, observable status, retry, and token rotation.
2. An approved mapping or allowlist defining the maintenance-assignment population.
   Job-title substring matching is not authorization.
3. A monotonic change watermark that covers `Employee` changes and any optional linked
   `User` changes used by the export. `Employee.updated_at` alone does not cover changes
   to `User.is_active` or roles.
4. Explicit, retryable delivery of `active=false`. The current active-only employee
   picker is not a synchronization feed because it cannot convey inactivation or a
   completed-snapshot watermark.
5. A production Fleetline token provisioned with only the approved scopes and stored in
   GatorHub's server-side secret configuration.

Activation requires contract tests plus a two-application E2E run against GatorHub's
real API/database and Fleetline's real API/PostgreSQL database. The run must cover first
sync, identical replay, changed person, stale/conflicting input, explicit inactivation,
cross-organization isolation, wrong/revoked/expired token, assignment and unassignment,
and continued GatorHub operation while Fleetline is unavailable. Browser diagnostics
must also prove that the service token never appears in storage, requests made by the
browser, or navigation URLs.
