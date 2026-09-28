# ADR 0006: GatorHub integration boundary

- Status: accepted
- Date: 2026-09-03

GatorHub remains the source of truth for a truck's business identity and descriptive
master data. Fleetline owns the organization-bound maintenance asset mirror, append-only
meter history, preventive-maintenance plans and due state, defects and requests, work
orders, parts activity, and operational availability. GatorHub may display Fleetline's
availability and PM summary for dispatch, but it must not independently change those
facts after cutover.

The stable cross-system key is GatorHub `Vehicle.id`, stored in Fleetline as
`source_system=gatorhub` plus `external_id=<Vehicle.id>`. `truck_no` is mapped to
`Asset.unit_number` for display and search, never used as identity: it is editable,
free-text dispatch currently creates it, and legitimate values include `000`, `001`,
`01`, and `Winch`. Each GatorHub deployment must be explicitly bound to exactly one
Fleetline organization and default location; no organization is inferred from request
data.

Server-to-server writes use the versioned REST boundary, a dedicated expiring Fleetline
API token, the minimum `assets.view` and `assets.sync` scopes, and a stable UUID
`Idempotency-Key`. Fleetline returns availability and PM projections through its durable,
HMAC-signed webhook outbox. GatorHub must verify the signature over the raw request body,
persist and deduplicate the event ID before acknowledging it, and run periodic
reconciliation as the recovery path. Neither application reads or writes the other's
database.

Deploy the applications on separate origins under the same managed parent domain, for
example `hub.example.com` and `maintenance.example.com`. GatorHub deep-links to the
Fleetline asset or PM screen. It must never put its browser JWT, a Fleetline API token, or
a session identifier in a URL, and the applications do not share cookies or service
workers. Initial rollout uses Fleetline login; a later single-sign-on rollout must use a
reviewed authorization-code flow and a stable GatorHub user/employee subject rather than
reusing GatorHub's browser token.

GatorHub's legacy maintenance work orders become read-only history at the transactional
cutover. New inspection findings flow to Fleetline as findings/defects for explicit
triage; they must not simultaneously create a GatorHub work order. Fleetline is the only
writer allowed to place a truck out of service or return it to service after that point.
PM due status is visible to dispatch, but it is not automatically an out-of-service rule
without an approved company policy.

The wire vocabulary and ownership map are versioned in
[`contracts/gatorhub-fleetline-v1.json`](../../contracts/gatorhub-fleetline-v1.json).
Roster adjudication, migration gates, failure handling, role mapping, and cross-system E2E
acceptance criteria are in
[`docs/gatorhub-integration.md`](../gatorhub-integration.md).
