# ADR 0009: GatorHub personnel projection and Fleetline work teams

- Status: accepted
- Date: 2026-09-04

## Context

GatorHub has a durable `Employee` identity separate from its optional login `User`.
Fleetline needs those people in maintenance assignment pickers without sharing databases,
accepting GatorHub browser JWTs, or turning a directory import into an authorization
grant. Work orders also need more than the legacy single-technician field while retaining
an append-only history of who was assigned.

## Decision

GatorHub `Employee.id` is the external person key. Fleetline stores a minimal,
organization-scoped `ExternalEmployeeProjection` under
`(organization, source_system="gatorhub", external_employee_id=Employee.id)`. The
optional GatorHub `User.id` is metadata only. A projection never creates a Fleetline
login, role, permission, or account link.

GatorHub will push explicit upserts to Fleetline's versioned REST endpoint using a
Fleetline-issued opaque, expiring service token with `personnel.sync`. The token is held
only by the GatorHub server. It is not a GatorHub human JWT, is never exposed to either
browser, and does not grant maintenance management or PM permissions. Requests use a
stable UUID idempotency key plus source version and timezone-aware source timestamp.
Older updates and equal-timestamp conflicts are rejected. Inactivation is explicit;
absence from an incomplete reconciliation never changes a person.

Fleetline managers change a work-order team with their Fleetline session and
`maintenance.manage`. The service token cannot assign work. The team supports at most
one lead plus additional technicians. Assignment and unassignment are immutable events;
the current team is their projection. Assignment events preserve assignment-time source
identity and display evidence so later directory edits do not rewrite history. An
external person can be scheduled and shown in history but cannot execute work without a
separately provisioned and assigned local Fleetline account. Existing pre-snapshot
events are backfilled with the best identity available at migration time, not presented
as proof of their original display name.

## Consequences

The applications remain independently deployable and usable through an integration
outage. Fleetline can rotate or revoke the integration token without changing GatorHub
authentication. Cross-organization identity collisions are prevented, and directory
updates cannot grant application access.

GatorHub still requires an outbound adapter, reconciliation status and retry, explicit
inactivation delivery, a complete source watermark, production secret provisioning, and
an organization-approved maintenance-eligibility rule before live synchronization is
enabled. The current active-only human employee picker is not that integration boundary.
Exact wire schemas and readiness state are maintained in
[`contracts/gatorhub-fleetline-v1.json`](../../contracts/gatorhub-fleetline-v1.json), with
operational details in
[`docs/gatorhub-personnel-integration.md`](../gatorhub-personnel-integration.md).
