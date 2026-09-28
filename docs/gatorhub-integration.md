# GatorHub integration and PM cutover

This is the implementation and rollout contract for connecting the GatorHub checkout at
`/home/gatorhub/gator/gatorhub` to Fleetline. It records the repository and local-data
evidence observed on 2026-09-03. It does not treat a development SQLite snapshot as
production truth.

## Boundary and current state

Fleetline already provides organization-scoped external asset and meter endpoints, an
`assets.sync` token scope, append-only meter semantics, audit events, and an HMAC-signed
webhook outbox. GatorHub does not yet publish vehicle changes, receive Fleetline
webhooks, show Fleetline PM/availability in dispatch or mobile snapshots, or deep-link a
truck to PM scheduling. Those GatorHub and GatorMobile changes are future work and are
cutover blockers, not hidden Fleetline behavior.

| Concern | Authoritative system | Projection or consumer |
|---|---|---|
| Vehicle UUID, truck number, VIN, year, make, model | GatorHub | Fleetline asset mirror |
| Fleetline asset UUID and GatorHub external-key binding | Fleetline | GatorHub stores/looks up the returned link |
| Meter readings, quality, corrections, and current projection | Fleetline | GatorHub may submit evidence and display the accepted value |
| Service packages, PM plans, due state, and completion reset | Fleetline | GatorHub dispatch/mobile show a summary |
| Findings, defects, requests, work orders, and repair history | Fleetline after cutover | GatorHub links to the source record |
| Available, restricted, out-of-service, and retired state | Fleetline after cutover | GatorHub dispatch keeps a fail-safe projection |
| Service requests and dispatch assignment | GatorHub | Fleetline does not dispatch trucks |
| Person record and GatorHub job/access profile | GatorHub | Fleetline maps a stable subject to a locally authorized user |
| Fleetline roles and effective permissions | Fleetline | GatorHub roles are provisioning hints only |

Do not share databases, create a general ERP boundary, or make GatorHub/AutoPi a
prerequisite for Fleetline maintenance.

## Identifiers and field mapping

The asset key is the tuple `(fleetline_organization_id, "gatorhub", Vehicle.id)`.
GatorHub `Vehicle.id` is a UUID and is immutable. `truck_no` is only a label: preserve it
as a string when reading GatorHub and never integer-coerce it. Fleetline trims and
uppercases `unit_number`, so `Winch` is displayed as `WINCH`, while leading zeros in
`000`, `001`, and `01` remain significant. The external UUID, not the normalized label,
prevents collisions and survives renumbering.

| GatorHub field | Fleetline v1 target | Rule |
|---|---|---|
| `Vehicle.id` | `Asset.source_system`, `Asset.external_id` | Store as `gatorhub` plus the UUID |
| `truck_no` | `unit_number` | Required; string-preserving input, trimmed/uppercased display |
| `vin`, `year`, `make`, `model` | same-named fields | GatorHub-owned on an externally linked asset |
| `vehicle_type` | configured Fleetline asset type and `specs.integrations.gatorhub.vehicle_type` | Prefer a reviewed type-ID map; retain the source value in `source_details` |
| `license_plate`, `capacity_bbl` | `specs.integrations.gatorhub.*` | Send in the bounded `source_details` object; the source merge preserves unrelated Fleetline specs |
| `status`, `active` | no master-data upsert field | Use the initial-state review and later Fleetline-to-GatorHub projection below |

Ordinary Fleetline edits must not overwrite GatorHub-owned fields on a linked asset.
Fleetline-owned location, driver assignment, specs, maintenance status, and histories are
preserved by a source upsert.

For a DVIR meter observation use
`gatorhub:vehicle_inspection:<inspection_uuid>:odometer_begin` or
`gatorhub:vehicle_inspection:<inspection_uuid>:odometer_end` as the reading external ID.
The source must supply an explicit `mi` or `km` unit and timezone-aware observation time.
The external ID is permanent even if the source later corrects the value; a correction is
a new superseding Fleetline record, never a rewrite.

## Roster reconciliation before import

The owner-provided canonical file contains 27 trucks. The local GatorHub SQLite snapshot
contains 31 because scenario fixtures created `107`, `112`, `115`, and `120`; those four
have related scenario inspections, service requests, and JHAs and are not evidence of
four additional production trucks.

The first import must therefore use `sop/app/backend/gatorhub/seed_data/fleet.csv` as an
allowlist and produce a review report rather than copying every live `vehicles` row.
Assert exactly 27 distinct GatorHub UUIDs and 27 Fleetline external bindings, and include
explicit checks for `Winch`, `000`, `001`, and `01`. Do not delete the four fixture rows
from a referenced database; reset an isolated demo/test database or archive/migrate its
references deliberately. Before production rollout, compare the actual production
database with the 27-row file and have an owner approve every addition, merge, rename, or
retirement.

## Meter baseline warning

Do not seed PM from the current local DVIR odometers. The inspected snapshot has 18
vehicle inspections whose meter values are repeated/demo-like and concentrated on the
four scenario trucks. GatorHub stores begin/end integer values without a unit,
plausibility policy, provenance, or durable external reading ID. The current GatorMobile
field runtime also defaults to `truck-7`, hardcodes a single passing brake check, and does
not submit an odometer.

For each real truck, collect and approve:

- physical odometer and/or engine-hour reading, unit, observation time, and recorder;
- last completed service date and completion meter for every package being scheduled;
- manufacturer/company interval and grace policy; and
- AutoPi/device association and units, if telematics will be used.

Until those facts exist, create a manual accepted baseline in Fleetline and use a date
plan where appropriate. Keep GatorHub/AutoPi optional.

## API, token, link, and webhook flow

### Vehicle reconciliation

1. Bind one GatorHub deployment to a configured Fleetline organization and default
   location. Never accept an organization ID from the synchronized vehicle payload.
2. Create a dedicated Fleetline integration user and an expiring API token containing
   only `assets.view` and `assets.sync`. Store it only in the GatorHub server secret
   store; rotate and revoke it independently of users and browser sessions.
3. Read the canonical GatorHub roster and call
   `PUT /api/v1/assets/external/gatorhub/{Vehicle.id}/` with the owned fields. Send a stable
   UUID `Idempotency-Key` and reject/report duplicate UUID, VIN, or unit conflicts.
4. `GET` the same URL to resolve the Fleetline asset and returned `deep_link`. Never make
   the browser call this API with the service token. Persist the returned Fleetline asset
   UUID beside `Vehicle.id`; inbound webhooks use that mapping.
5. Repeat a full comparison on a schedule because GatorHub currently has neither a
   vehicle `updated_at` field nor a vehicle outbox. Unchanged source payloads must be
   no-ops.

A deterministic key can be `UUIDv5(Vehicle.id, "asset-upsert:" +
SHA256(canonical-owned-fields))`. The unique external tuple is the second deduplication
barrier. For a meter operation, use `UUIDv5(inspection.id, "meter:" + field_name)` plus
the permanent external reading ID above. An identical replay returns the logical prior
result; reuse with a different request body must fail rather than overwrite.
Fleetline scopes that reading ID across the organization and source, using an
organization-wide database lock during ingest; replace it with a dedicated lock table
only if measured fleet ingest concurrency makes the coarse lock material.

### PM deep link

Deploy Fleetline at a separate origin such as `maintenance.example.com`, while GatorHub
uses `hub.example.com`. Configure Fleetline's `FLEETLINE_SITE_ADDRESS`, `ALLOWED_HOSTS`,
TLS, and trusted origins for that hostname. Separate origins avoid collisions between the
two SPAs, both `/api/v1` namespaces, service-worker scopes, local storage, and sessions.
Do not iframe Fleetline.

The external-asset response returns browser-safe `deep_link` and `schedule_link` values.
The latter opens `/schedule?asset_id={fleetline_asset_id}`, preserves the selection through
login, and preselects the truck for a user authorized with `pm.manage`. No credential or
organization identifier belongs in either URL.

Initial deployment uses a normal Fleetline login. Do not reuse GatorHub's local-storage
JWT or signing key. A later SSO release should use an audited authorization-code flow
with short-lived, single-use codes, state/PKCE, an exact audience and redirect URI, and a
stable subject (`User.id`, with `Employee.id` retained for person linkage). Fleetline
must issue its own session and perform its own authorization.

### Return projection

Subscribe GatorHub to `asset.availability_changed`, `maintenance.due`,
`maintenance.plan_projection_changed`, and `work_order.completed`. Fleetline sends a
JSON envelope with an immutable event `id`, `schema_version`, `type`, `organization_id`,
occurrence time, resource reference, and data. It signs the exact bytes in
`X-Fleetline-Signature: sha256=<hex>`.

`work_order.completed` occurs before supervisor closure and therefore must not update the
next-PM projection. `maintenance.plan_projection_changed` is emitted when a plan is
created, whenever its due status changes, and after a successful close-time trigger reset
even if the status stays the same. It carries payload schema `1.0`, the Fleetline
asset/plan IDs, optional GatorHub source identity and work-order ID, final due status, and
each trigger's next date or meter value. Unknown initial baselines are sent as `Due` with
a human-readable reason. A failed close rolls back both the reset and event; an
idempotent close replay does not create another event.

The GatorHub receiver must:

1. read the raw body, verify HMAC-SHA256 in constant time, reject an unexpected
   organization, and enforce a size limit;
2. insert the event ID into a unique durable inbox before applying it;
3. update only projection fields and append a GatorHub audit event;
4. return 2xx only after the inbox commit, returning the same 2xx for an identical
   replay; and
5. enqueue resource fetch/retry work rather than doing long processing in the request.

Fleetline retries failures and exposes dead deliveries. A scheduled GatorHub job must
also pull/reconcile asset status and active PM summaries so a lost, delayed, or
out-of-order webhook cannot leave dispatch unsafe.

## Status and role mapping

Initial migration maps GatorHub `status=active|available` to Fleetline `Available` and
`status=out_of_service` to `OutOfService`, subject to review. A GatorHub `active=false`
record requires a human disposition before Fleetline `Retired`; retirement is not a
destructive sync operation. The external master-data endpoint deliberately does not
accept status, so apply a reviewed initial out-of-service state through Fleetline's
authorized availability transition after the mirror is created. After cutover, project
Fleetline status back as follows:

| Fleetline | GatorHub projection |
|---|---|
| `Available` | `status=available`; preserve the master active flag |
| `Restricted` | fail safe as `status=out_of_service`; preserve active flag |
| `OutOfService` | `status=out_of_service`; preserve active flag |
| `Retired` | `status=out_of_service`, `active=false` |

`DueSoon`, `Due`, and `Overdue` appear as dispatch warnings. They do not block dispatch
unless an approved and tested company rule says which PM condition changes availability.

Role mapping is a least-privilege provisioning hint, not authorization federation:

| GatorHub access profile | Fleetline default | Provisioning rule |
|---|---|---|
| `driver`, `lead_driver` | `driver` | No leadership privilege implied |
| `mechanic` | `technician` | Work execution only |
| `lead_mechanic` | `supervisor` | Approve explicitly |
| `owner` | `management` | Read-only operational view |
| `admin` | `system_admin` | Manual approval and Fleetline MFA required |
| dispatcher, office, billing, safety, service, SOP roles | none | Add a narrower Fleetline role if a real workflow requires it |

Fleetline-specific `fleet_manager`, `parts_clerk`, `purchasing_manager`, and
`integration_admin` are assigned in Fleetline and are never inferred from GatorHub.
Disabling either account must revoke that application's access; service tokens have their
own owner, expiry, rotation, and revocation procedure.

## Cutover gates in GatorHub and GatorMobile

These are priority-zero integration tasks outside the current Fleetline change:

1. Replace dispatch's free-text auto-create with selection of a reconciled vehicle or an
   explicit, reviewed “new truck” workflow. Enforce both `active` and Fleetline
   availability at assign, edit/reassign, start, and resume.
2. Stop a flagged GatorHub DVIR review from directly creating a local work order and
   changing vehicle state. Submit a Fleetline finding/defect, preserve source IDs and
   attachments, and let Fleetline triage decide request/work creation.
3. Freeze new GatorHub maintenance work orders and disable its local close/cancel logic
   from returning a truck to service. Otherwise GatorHub and Fleetline are conflicting
   status writers. Preserve existing records as linked, read-only history.
4. Add Fleetline availability, PM summary, and stable vehicle UUID to GatorHub's mobile
   service-request snapshot. Existing snapshots contain descriptive vehicle fields only.
5. Preserve `service_request.vehicle_id` when mobile creates a field ticket; the current
   submission path leaves the supported ticket vehicle link empty.
6. Replace GatorMobile's `truck-7`/single-check DVIR scaffold with the assigned GatorHub
   vehicle UUID, real template results, odometer plus unit/time, and offline-safe evidence.
7. Extend attachment sync parent types/kinds to inspection evidence. The current contract
   accepts service request, field ticket, JHA/JSA, and print-job parents, not vehicle
   inspections.

Do not begin transactional cutover until all seven have automated coverage or a written,
time-bounded operational control approved by the owner.

## Reconciliation and recovery

Every run produces a durable report containing source count, mapped count, new/changed/no-op
counts, unmatched external IDs, duplicate VIN/unit conflicts, status drift, plan coverage,
latest accepted meter age, webhook inbox/outbox lag, and dead deliveries. Alert on any
unmatched active dispatch vehicle or out-of-service disagreement.

Owned-field rules resolve conflicts; do not use cross-system last-write-wins. GatorHub
wins a VIN or truck-number edit, while Fleetline wins availability, meter quality/current
projection, PM state, and maintenance transactions. A conflicting replay, invalid unit,
implausible/decreasing reading, unknown organization binding, or ambiguous roster row is
quarantined for a named human review queue. No failure may silently drop a truck, meter,
finding, or status change.

## Rollout and acceptance tests

1. **Profile and approve data.** Back up both systems; bind organization/location;
   approve the 27-truck roster; classify every extra; collect real meter and service
   baselines.
2. **Dry-run the mirror.** Use a rotated test token against a clean Fleetline database,
   produce the reconciliation report, then import the 27 allowlisted UUIDs twice and
   prove the second run is a no-op.
3. **Configure PM.** Version service packages, create plans from approved intervals and
   baselines, and verify due/overdue and completion reset calculations.
4. **Deploy links and projections.** Put Fleetline on its subdomain, add GatorHub asset
   and Schedule PM links, implement the signed inbox and periodic reconciliation, and
   show warnings/status in dispatch.
5. **Cut over authority.** Freeze GatorHub work-order/status writes, route inspections to
   Fleetline triage, and monitor both audit trails during a controlled pilot.
6. **Complete mobile/identity work.** Ship the real DVIR/attachment contract and add SSO
   only after its security review.

The cross-system E2E job must launch real built GatorHub and Fleetline applications,
Fleetline PostgreSQL, the GatorHub database, both workers, and a real browser. It must
cover at least:

- canonical 27-row import, exact handling of `000`/`001`/`01`/`Winch`, exclusion of the
  four fixtures, source update/no-op replay, duplicate conflict, and tenant isolation;
- a GatorHub user choosing a truck, following Schedule PM, authenticating, seeing the
  correct preselected asset, and creating a plan through Fleetline UI;
- approved baseline meter through due/overdue, planned work, completion meter, next-due
  reset, and preserved service-package version;
- Fleetline out-of-service and return-to-service webhooks changing the GatorHub dispatch
  projection, while a PM warning alone does not block dispatch;
- direct and UI attempts to assign, start, or resume an unavailable truck being rejected
  server-side;
- duplicate API requests, repeated/out-of-order webhooks, receiver outage and retry,
  application restart, dead-letter repair, and full reconciliation without duplicates;
- disabled user, expired/revoked/wrong-scope token, invalid HMAC, wrong organization,
  direct-link RBAC, and no cross-organization data access; and
- mobile offline inspection with real assigned UUID, odometer/unit, defect and attachment,
  exactly-once synchronization into Fleetline triage, and conflict handling without data
  loss.

Assert visible UI, both databases, audit events, idempotency/inbox rows, meter history,
PM state, webhook delivery, and dispatch rejection. The cross-application suite is not
implemented yet; the integration is not production-ready until this suite passes.

## Source evidence

- GatorHub vehicle identity/master: `sop/app/backend/gatorhub/fleet/models.py:22-36`;
  status schema: `fleet/schemas.py:10-52`.
- Canonical roster and string-ID warning: `seed_data/fleet.csv:1-28` and
  `seed.py:305-329`; scenario trucks: `dispatch/seed_scenarios.py`.
- Free-text vehicle creation and incomplete dispatch guards:
  `dispatch/service.py:328-342,360-412,441-448,556-581`.
- GatorHub DVIR-to-local-work behavior: `hse/service.py:142-179`; local return-to-service
  writer: `maintenance/service.py:255-280`.
- Incomplete mobile projection/ticket lineage: `sync/snapshots.py:30-38` and
  `sync/service.py:62-78`; attachment parent allowlist: `sync/protocol.py:44-49,455-489`.
- GatorMobile DVIR scaffold:
  `gatormobile/apps/mobile/src/screens/FieldRuntimeScreens.tsx:882-927`.
- Fleetline external keys/API: `backend/assets/models.py`, `backend/assets/urls.py`,
  `backend/assets/views.py`; permission scope: `backend/core/permissions.py`.
- Fleetline status/due events and webhook envelope: `backend/assets/services.py`,
  `backend/maintenance/services.py`, and
  `backend/core/management/commands/runworker.py:238-303`.
