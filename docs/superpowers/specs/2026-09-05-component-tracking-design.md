# Component tracking — design

**Status:** synthesized 2026-09-05 from a 12-agent design workflow (6 codebase
readers → 3 independent designs → 3 adversarial judges; unanimous winner
"field user first", with the judges' grafts applied). Third of five
sub-projects (replay → fault alerts → **components** → warranty → camera barcode).

## Problem

A truck's engine or transmission serial number is today a string inside
`Asset.specs.equipment.<section>.serial_number` (ADR 0007). Nothing can say
"this exact transmission was on TRK-012 from March to June at these mileages,
was moved to TRK-007 on WO-2026-0042, and these are the jobs done on it".
Requirement AST-05 asks for exactly that, and the next sub-project (warranty)
needs a stable record to attach coverage and claims to — a JSON key is not one.

ADR 0007 deferred component tables "until the fleet needs serviceable-component
lifecycle tracking". That trigger is now met; this design amends it with ADR 0011.

## Decisions (plain English)

1. **A component is its own record** — kind (engine, transmission, reefer unit,
   APU, axle, aftertreatment, other), serial number, make, model. Identity only.
   No cost, vendor, part or warranty fields yet.
2. **Each time it goes on or comes off a truck is one "installation period" row**
   with the truck, when, who, which work order, and a frozen copy of the truck's
   odometer / engine-hour readings at that moment. The install half is written
   once; the removal half (when, who, work order, meters, required reason) is
   filled exactly once; the database rejects every other update and all deletes.
   A mistake is corrected by removing with a reason and reinstalling — history
   is never edited (ADR 0002).
3. **The database enforces one open installation per component**, so a serial
   cannot be "on two trucks at once" even under concurrent requests.
4. **Meter evidence is captured automatically, best-effort**: the latest
   accepted, uncorrected reading observed at or before the event time, per active
   odometer / engine-hours meter, copied by value with the reading id (the
   retirement-snapshot shape). Missing readings are omitted, never invented, and
   never block a technician. Later meter corrections do not rewrite snapshots.
5. **Technicians record swaps from the work-order screen.** A technician
   (`maintenance.execute`) may install/remove only through an open work order on
   that asset where they are an active assignee. Managers/supervisors
   (`assets.manage` or `maintenance.manage`) may do it with or without a work
   order, from the asset page too.
6. **Work-order tasks may point at a component**, so per-component service
   history is a query, not an inference. The component page shows tagged tasks
   plus the install/remove work orders themselves.
7. **Retiring a truck closes its open installations** with the retirement
   meter snapshot and reason "Asset retired".
8. **Existing serials are backfilled** from `specs.equipment.*.serial_number`
   into components with source `legacy_specs_backfill` (best available identity,
   not proof). The JSON keys are left in place but the UI stops writing and
   displaying them; `specs.equipment.engine.type` and other descriptive values
   remain authoritative (the document library reads `engine.type`).
9. **Unknowns stay unbuilt**: no shelf location for removed components, no
   lifetime-hours math, no position/slot labels, no cost/vendor/receipt, no
   warranty fields, no component search page, no offline install/remove, no
   void/replace verb, no per-component PM plans, no component `retired_at`.

## Data model (`backend/assets/models.py`, after `MeterReading`)

### `Component(OrganizationOwnedModel)`

| field | type | notes |
|---|---|---|
| `kind` | `CharField(30, choices=Kind)` | `Kind` TextChoices: `engine`, `transmission`, `reefer_unit`, `apu`, `axle`, `aftertreatment`, `other` |
| `serial_number` | `CharField(100)` | required; `save()` normalizes `strip().upper()` like `Asset.vin` |
| `manufacturer` | `CharField(100, blank=True)` | |
| `model` | `CharField(100, blank=True)` | |

Constraints: `UniqueConstraint(organization, kind, serial_number)` named
`uniq_component_kind_serial_org`; `CheckConstraint(~Q(serial_number=""))` named
`component_serial_not_empty`; `Index(organization, kind)`; `ordering = ["kind",
"serial_number"]`.

> **Why `kind` stays in the key.** Dropping to `(organization, serial_number)`
> was considered and rejected. It reads better — a mis-picked `kind` would then
> be impossible, instead of silently splitting one physical part's history
> across two rows — but it breaks the backfill: `backfill_specs_serials` walks
> every equipment section of every asset, and legacy free-text serial fields
> contain placeholder junk. Two sections holding `"N/A"` would collapse into one
> Component representing two different physical parts, silently, at migration
> time, with no human in the loop.
> **Ceiling:** a genuine cross-kind serial collision (a real axle serial equal to
> a real APU serial) is allowed by this key and produces two rows, which is
> correct. The reverse mistake — the same part entered under two kinds — is not
> caught and must be fixed by a row merge. Narrowing the key later is therefore
> *not* free; it needs a merge migration. Revisit only with roster evidence that
> mis-keyed kinds actually happen.

`delete()` raises `ValidationError("Components are retired, not deleted")`
mirroring `Asset.delete`. `to_dict()` →
`{id, kind, kind_label, serial_number, manufacturer, model, installed_on:
{installation_id, asset_id, unit_number, installed_at} | None, created_at,
updated_at}`.

### `ComponentInstallation(OrganizationOwnedModel)` — period row, write-once

| field | type | notes |
|---|---|---|
| `component` | FK `Component` PROTECT, `related_name="installations"` | |
| `asset` | FK `Asset` PROTECT, `related_name="component_installations"` | |
| `installed_at` | `DateTimeField` | tz-aware, not in the future |
| `installed_by` | FK `core.User` PROTECT null/blank, `related_name="component_installs"` | null only for backfill |
| `installed_work_order` | FK `"maintenance.WorkOrder"` PROTECT null/blank, `related_name="component_installs"` | string ref; assets → maintenance dependency stated in ADR |
| `installed_meters` | `JSONField(default=list)` | list of `{meter_id, meter_name, kind, unit, reading_id, value, observed_at, source, quality}` |
| `source` | `CharField(40, default="web")` | `web` / `asset_retirement` / `legacy_specs_backfill` |
| `removed_at` | `DateTimeField(null, blank)` | |
| `removed_by` | FK `core.User` PROTECT null/blank, `related_name="component_removals"` | |
| `removed_work_order` | FK `"maintenance.WorkOrder"` PROTECT null/blank, `related_name="component_removals"` | |
| `removed_meters` | `JSONField(default=list)` | |
| `removal_reason` | `TextField(max_length=1000, blank=True)` | required iff removed |

Constraints:
- `CheckConstraint(Q(removed_at__isnull=True) | Q(removed_at__gt=F("installed_at")))` → `component_installation_positive_period`
- `UniqueConstraint(fields=["component"], condition=Q(removed_at__isnull=True))` → `uniq_open_component_installation`
- `CheckConstraint((removed_at IS NULL) == (removal_reason == ""))` → `component_installation_removal_reason`
- `Index(organization, asset, installed_at)`, `Index(organization, component, installed_at)`; `ordering = ["-installed_at", "-created_at", "id"]`

  The tie-break is load-bearing, not cosmetic: `backfill_specs_serials` sets
  `installed_at = asset.created_at` for *every* equipment section of an asset, so
  a truck carrying both an engine and a transmission serial gets two
  installations with an identical `installed_at` on day one. Bare
  `["-installed_at"]` leaves the components table, the component detail view and
  the history timeline nondeterministic, and the "orders newest first" assertions
  would be flaky.

`clean()`: component, asset, both work orders same organization; each work
order's `asset_id == asset_id`; **period overlap check** copied from
`DeviceAssetAssociation.clean` (a backdated `installed_at` may not fall inside
another period of the same component); `installed_at`/`removed_at` not in the
future.

**PostgreSQL write-once guard** (RunSQL in the migration, after the backfill),
modelled on the inspection Submitted→Voided guard in `maintenance/migrations/0004`:
trigger `component_installation_write_once` BEFORE UPDATE OR DELETE on
`assets_componentinstallation`. DELETE always raises
(`'assets_componentinstallation is append-only'`, ERRCODE 55000). UPDATE is
allowed only when `OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL AND
ROW(OLD.id, OLD.organization_id, OLD.component_id, OLD.asset_id, OLD.installed_at,
OLD.installed_by_id, OLD.installed_work_order_id, OLD.installed_meters,
OLD.source, OLD.created_at) IS NOT DISTINCT FROM ROW(NEW.…same…)`. Any future
column on this table requires re-creating the trigger — stated in the ADR.

**The primary key leads the ROW() list**, matching the guard this copies
(`maintenance/migrations/0004_inspection_immutability_guards.py:21` puts
`NEW.id`/`OLD.id` first). A column absent from the comparison is silently
freely updatable, and `id` is the column whose mutation would be least visible.

Re-creating the trigger later is an ordinary `RunSQL` DROP + CREATE inside one
migration; PostgreSQL DDL is transactional, so there is no window in which the
guard is absent. That is why this table takes no speculative columns now — see
Notes, "Columns deliberately not added".

`to_dict()` → `{id, component_id, component: {kind, kind_label, serial_number,
manufacturer, model}, asset_id, asset: {unit_number}, installed_at,
installed_by_id, installed_by, installed_work_order_id,
installed_work_order_number, installed_meters, source, removed_at,
removed_by_id, removed_by, removed_work_order_id, removed_work_order_number,
removed_meters, removal_reason}`.

### Existing model changes

- `maintenance.WorkOrderTask.component` — FK `"assets.Component"` PROTECT
  null/blank, `related_name="work_order_tasks"`; migration
  `maintenance/0009_workordertask_component.py` (AddField, depends on
  `assets/0004`). `to_dict` adds `component_id` and `component: {kind,
  kind_label, serial_number} | None`; flows into `WorkOrderCloseSnapshot`
  automatically (additive under snapshot `schema_version` 1).
- `Asset` — **no field change**; `Asset.to_dict` is deliberately not extended
  (500-row list N+1; GatorHub `ExternalAsset` contract untouched, stays 1.3.0).
- `AssetStatusEvent` — no schema change; the RETIRED event's `context` gains
  additive `removed_component_ids: [str]`.
- `core.reporting._EXPORTED_MODELS` — add `("components", Component,
  frozenset())` and `("component_installations", ComponentInstallation,
  frozenset())` after `meter_readings`. Export `schema_version` stays `"1.0"`;
  `docs/api.md` gains one sentence: additive record families do not bump the
  export schema version.
- `assets.admin` — `ComponentAdmin` (list/search on kind, serial, make, model);
  `ComponentInstallationAdmin` read-only, no add/delete (fact-table convention).

## Services (`backend/assets/services.py`)

- `_meter_snapshot(meter, reading) -> dict` — extracted from the retirement
  snapshot literal (services.py ~L660-672); retirement calls it, behaviour
  unchanged.
- `_meter_snapshots_as_of(asset, at) -> list[dict]` — per active
  odometer/engine_hours meter, latest ACCEPTED uncorrected reading with
  `observed_at <= at`; missing → omitted.
- `install_component(*, asset, actor, component=None, kind="", serial_number="",
  manufacturer="", model="", installed_at=None, work_order=None, source="web")
  -> ComponentInstallation`
  - lock order: `Asset` → `Component` → installations (`select_for_update`),
    matching `associate_device_to_asset`, to avoid deadlock with retirement
  - `installed_at` defaults now; tz-aware and not future else `invalid_installed_at` 400
  - asset must not be RETIRED/archived → `asset_retired` 409
  - `component is None` → `kind`+`serial_number` required
    (`component_identity_required` 400), kind valid (`invalid_component_kind`
    400), `Component.get_or_create(organization, kind, serial_number=normalized,
    defaults={manufacturer, model})`; created → audit `component.created`
  - `component` given → same org (`invalid_component` 400)
  - open installation exists → `component_installed_elsewhere` 409 with
    `details {asset_id, unit_number, installation_id}`; the same 409 shape is
    produced from `IntegrityError` on `uniq_open_component_installation` by
    re-reading the open row (race path == pre-check path)
  - `work_order` optional: same org and `work_order.asset_id == asset.pk`
    (`invalid_work_order` 400); status not Completed/Closed/Cancelled
    (`work_order_not_open` 409)
  - **authorization**: actor with `assets.manage` or `maintenance.manage` may
    install with or without a work order; otherwise (`maintenance.execute`) a
    work order is required (`work_order_required` 403) and the actor must be an
    active assignee (`permission_denied` 403), mirroring `_can_execute`
  - `installed_meters = _meter_snapshots_as_of(asset, installed_at)`
  - audit `component.installed` (resource=installation, `new_state=unit_number`,
    context `{component_id, asset_id, work_order_id, installed_meters}`); emit
    `component.installed` `{component_id, asset_id, installation_id}` (internal
    outbox event; not in the webhook enum)
- `remove_component(*, component, actor, reason, removed_at=None,
  work_order=None) -> ComponentInstallation`
  - `reason.strip()` required → `reason_required` 400
  - open installation locked else `component_not_installed` 409
  - `removed_at` defaults now; tz-aware, not future, `> installed_at` else
    `invalid_removed_at` 400
  - work-order and authorization rules identical to install (work order must be
    on the installation's asset)
  - `removed_meters = _meter_snapshots_as_of(asset, removed_at)`; `save(update_fields=[removed_at, removed_by, removed_work_order, removed_meters, removal_reason, updated_at])` — the only UPDATE the guard permits
  - audit `component.removed` (context `{component_id, asset_id, work_order_id,
    reason, removed_meters}`); emit `component.removed`
- `change_asset_status` RETIRED branch — after retirement snapshots are built,
  close every open installation of the asset (`removed_at=now`,
  `removed_by=actor`, `removed_meters=<retirement snapshots>`,
  `removal_reason="Asset retired"`, `source` unchanged); one `component.removed`
  audit per row; `removed_component_ids` in the event/audit context.
- `maintenance.services.create_work_order_task(..., component=None)` and
  `update_work_order_task(..., component=UNSET)`; helper
  `_component_on_asset(component, asset)`: same org (`invalid_reference` 400);
  component has an open installation on this asset **or** no open installation
  at all (the transmission about to go in) else `component_not_on_asset` 409.

## API (`backend/assets/urls.py`, `views.py`; `maintenance/views.py`)

| Method & path | Permission | Notes |
|---|---|---|
| `GET /api/v1/assets/{asset_id}/components/` | asset via `_asset_queryset` (assets.view or assigned driver); cross-org 404 | `{"installations": [...]}` all periods, `-installed_at` |
| `POST /api/v1/assets/{asset_id}/components/` | `assets.manage` \| `maintenance.manage` \| `maintenance.execute` (execute requires `work_order_id`, see services) | body allow-list `{component_id, kind, serial_number, manufacturer, model, installed_at, work_order_id}` else `unsupported_fields` 400; 201 `{"installation", "component"}`; wrapped in `idempotent()` |
| `GET /api/v1/assets/components/{component_id}/` | `assets.view`; 404 cross-org | `{"component", "installations": [...], "tasks": [{task_id, work_order_id, work_order_number, title, status, completed_at, asset_id, unit_number}], "work_orders": [install/remove WOs]}` (service history = tasks ∪ install/remove WOs) |
| `POST /api/v1/assets/components/{component_id}/remove/` | same trio as install | body `{reason, removed_at, work_order_id}`; 200 `{"installation"}`; `idempotent()` |
| `GET /api/v1/assets/{asset_id}/history/` (existing) | unchanged | adds `component_installations` and timeline entries `component_installed` / `component_removed` with `links.component_id`, `links.work_order_id?`, `context.installed_meters|removed_meters` |
| `POST /work-orders/{id}/tasks/`, `PATCH …/tasks/{task_id}/` (existing) | unchanged | optional `component_id` (null clears on PATCH); 409 `component_not_on_asset` |

URL names: `asset-components`, `component-detail`, `remove-component`. Add the
new paths to `core/test_schema.py` expected paths so the auto-added
`Idempotency-Key` parameter and 400/401/403/404/409 responses are asserted.

## UI (`frontend/src/App.tsx`)

- **Asset detail**: new `Panel "Components"` as the second panel in the split.
  Collapsed `<details>` with a count in the summary (`"2 installed components"`);
  never renders an empty table. Open table columns: Component (kind label +
  make/model), Component serial (Link `/components/{id}`), Installed, Meter at
  install (first snapshot `value unit`, `—` if none), Remove button gated
  `!isRetired && (can("assets.manage") || can("maintenance.manage"))` (technicians
  act from the work-order screen). Remove = `window.prompt("Reason for
  removal")` → POST remove — same pattern as `availability()`. Panel action:
  `details.action-details` "Install component" popover: SelectField Kind, Field
  "Component serial number" (required), Make, Model, then nested
  `<details><summary>Advanced</summary>` with "Installed at" (datetime-local) and
  "Work order number". Below: `<details><summary>Past components (n)</summary>`
  table (Component, Serial, Installed, Removed, Meter at removal, Reason).
- **"Engine information" dl** keeps Manufacturer/Model/Type from
  `specs.equipment.engine`; the Serial row renders the installed engine
  component's serial as a Link, or `—`. **"Edit equipment details"** drops the
  `*_serial_number` inputs and `saveEquipment` strips `serial_number` from every
  spread section so re-saving never re-sends legacy keys.
- **Asset create form**: the "Engine serial number" field is **removed**; the
  fleet manager installs the engine from the Components panel (one popover).
  No two-POST path.
- **Work-order detail**: new `Panel "Components on {unit}"` after the
  Tasks/Work details split. Install/Remove controls gated
  `(can("maintenance.execute") || can("maintenance.manage")) &&
  !/completed|closed|cancelled/i.test(status)`; every POST includes
  `work_order_id`. Transmission swap = Remove (tap) + reason + OK, then Install
  (tap) + Kind + serial + Install — no ids, no meter picking. Task rows append
  `Component: Transmission · ALLISON-3000-778` when set. "Add work-order task"
  form gains optional SelectField "Component" listing installed components.
- **New route `/components/:id` → `ComponentDetail`** (before the `*` route).
  Title `${kind_label} ${serial_number}`, subtitle make/model, Status chip
  Installed / Not installed. Panels: "Installation history" (Asset link,
  Installed, Meter at install, Removed, Meter at removal, Reason, Work order
  link) and "Service history" (tagged tasks ∪ install/remove work orders). No nav
  entry — reached from asset/WO panels.
- Timeline: `links.component_id → /components/{id}`; Status tone regex gains
  `installed → success`.

## Data migration (`assets/migrations/0004_components.py`)

Dependencies: `assets/0003_asset_external_identity`, latest `core`,
`maintenance/0008_…` (for the WorkOrder FKs). Operations: CreateModel ×2,
constraints, indexes, `RunPython(backfill_specs_serials, noop)`, then `RunSQL`
(create trigger / drop). Backfill runs **before** the trigger so closing retired
rows is legal.

`backfill_specs_serials`: for every Asset (all orgs, archived included) read
`specs.get("equipment", {})`; section → kind: `engine→engine`,
`transmission→transmission`, `axle→axle`, `emissions→aftertreatment`,
`auxiliary→other`. For each string `serial_number`: normalize `strip().upper()`, then
**skip it unless it looks like a serial** — reject the empty string and the
placeholder set `{"N/A", "NA", "NONE", "N\\A", "-", "--", "0", "00", "000",
"UNKNOWN", "TBD", "SEE PLATE", "ON PLATE", "?"}`, and reject anything shorter
than 4 characters. Print one line per skip naming the unit number, section and
raw value, so the operator can hand-enter the real serials afterwards. This
guard is what makes `(organization, kind, serial_number)` safe — without it the
legacy junk in these free-text fields becomes Component rows.

For each surviving serial: `Component.get_or_create(organization, kind,
serial_number, defaults={manufacturer[:100], model[:100]})`; if that component
already has an open installation (same serial typed on two trucks) **skip with
a printed warning naming both unit numbers**; else create
`ComponentInstallation(id=uuid5(asset.pk, f"legacy-component:{section}"),
installed_at=asset.created_at, installed_by=None, installed_meters=[],
source="legacy_specs_backfill")`. For retired assets (`archived_at` set) also set
`removed_at=archived_at`, `removal_reason="Asset retired"`, `removed_meters`
copied from the RETIRED `AssetStatusEvent.context.final_meter_readings` when
present, else `[]` with reason `"Asset retired (legacy backfill)"`. JSON keys are
not rewritten. No audit rows are fabricated for the backfill — `source` on the
row is the marker.

`assets/tests.py` (~L86-143) and `tests/e2e/preventive-maintenance.spec.ts`
currently assert `serial_number` inside specs; both change in the same
delivery.

## ADR 0011 — `docs/adr/0011-serviceable-component-tracking.md`

Header: `- Status: accepted`, `- Date: 2026-09-05`. Quote ADR 0007's deferral
sentence verbatim and name AST-05 plus the warranty dependency as the trigger.
Decision text: the plain-English decisions above, plus explicitly: (a) this is a
third shape between `DeviceAssetAssociation` (fully mutable close) and
`AssetStatusEvent` (pure facts) — a period row with a DB write-once guard —
chosen so the database can enforce one open installation and warranty gets a
stable period id; (b) assets may reference `maintenance.WorkOrder` by string FK
(inventory already crosses into maintenance); (c) transfer = remove then install
in one transaction — no fourth verb; (d) serial uniqueness is `(org, kind,
serial)`; widening the key (adding manufacturer) is a lossless follow-up, but
**narrowing it to `(org, serial)` is not** — that needs a row merge, and the
backfill's placeholder guard is what makes the current key safe. The ceiling and
its upgrade path are stated in the design; (e) `Component` is the source of truth for serials; legacy
JSON keys are historical.

## Docs

`docs/prior-art.md` records the ERPNext and Snipe-IT reading behind the
decisions here — in particular why Snipe-IT's check-in, which hard-deletes the
period row, is the clearest external case for `ComponentInstallation` being
write-once.

- `docs/api.md`: new endpoint rows in the assets table; task `component_id`;
  export bullet adds components and installations; one sentence on additive
  export families.
- `docs/validation-assumptions.md`: row "Component identity" — kind list and
  `(org, kind, serial)` uniqueness are provisional; evidence: roster component
  serials and manufacturer collisions; best-effort meter evidence at
  install/remove vs strict at retirement.
- `docs/e2e-coverage.md`: row for `component-tracking.spec.ts`.
- `scripts/test-e2e.sh`: add the spec to `required_specs`.

## Tests

Backend (`assets/test_components.py`, `assets/test_components_migration.py`,
`maintenance/test_component_tasks.py`, plus edits to `core/test_reporting_export.py`,
`core/test_schema.py`, `assets/tests.py`):

1. install creates component (serial normalized) and snapshots current meters;
   audit `component.created` + `component.installed`
2. same-key replay returns the same 201; different key → 409
   `component_installed_elsewhere` with `details.asset_id`
3. component open on TRK-A installed onto TRK-B → 409 with
   `details.unit_number == "TRK-A"`; no second row
4. remove without reason → 400; with reason → 200 with `removed_meters`;
   second remove → 409 `component_not_installed`
5. DB write-once guard (`TransactionTestCase`, skipped unless postgresql):
   `update(installed_at=…)` on open row raises; `update(removed_at=…)` on a
   closed row raises; raw DELETE raises
6. reinstall after removal reuses the same component: one `Component`, two
   installations, detail endpoint orders newest first
7. backdated install snapshots the reading as of `installed_at`, not the latest
8. backdated install inside a closed period of the same component → 400
   (overlap); future `installed_at` → 400
9. meter correction after install leaves `installed_meters` unchanged and
   `reading_id` pointing at the original
10. tenant scoping: other org gets 404 on detail, remove, and list
11. technician: with assigned open WO → 201 and `installed_work_order` set;
    without WO → 403 `work_order_required`; not assigned → 403; closed WO → 409;
    WO on another asset → 400
12. driver: GET 200 read-only; POST → 403
13. manager installs without a work order → 201
14. retirement closes open installations with the final snapshot and
    `removed_component_ids`; `test_retirement.py` still passes
15. asset history includes `component_installations` and both timeline types
16. export contains `components` and `component_installations`;
    `schema_version` still `"1.0"`
17. schema test: new paths present with `Idempotency-Key` and error responses
18. task with `component_id` on an installed component → 201; appears in
    component detail `tasks`; PATCH null clears and bumps version
19. task component on another asset → 409; a removed (uninstalled) component → 201
20. task `component_id` survives into `WorkOrderCloseSnapshot`
21. migration test (`migrate_from assets/0003 → 0004`): two engine serials →
    two components with deterministic uuid5 installation ids and source
    `legacy_specs_backfill`; retired asset's installation closed; duplicate
    serial on a second truck skipped; guard active afterwards

E2E `tests/e2e/component-tracking.spec.ts`: supervisor creates a WO on seeded
TRK-012 via API and assigns the technician; technician (second browser context)
opens the WO, installs a transmission from the "Components on TRK-012" panel
(asserts 201 with `installed_meters.length >= 1` and `installed_work_order_id`),
removes it with reason "Bench test only", replays the captured install with the
same `Idempotency-Key` and gets the same installation id, reinstalls the same
serial (one component, two installations); supervisor reloads the asset page,
sees the row and "Past components (1)", opens the component page, sees two
installation rows with the WO number and reason and the tagged task under
service history; audit events for the installation carry actor technician and
`correlation_id == Idempotency-Key`; `diagnostics.assertClean()`.
`preventive-maintenance.spec.ts` updated for the removed engine-serial field.

## Also in this delivery

Two changes outside `assets/` that this design makes cheap now and expensive
later. Both were verified against the code, not assumed.

**1. Component serials reach the search box.** `global_search`
(`core/views.py:903-966`) covers asset, part, work order and vendor; it has no
component branch, and `/components/:id` gets no nav entry, so a legible serial
plate on a bench part reaches nothing. Add a `component` branch gated on
`assets.view`, matching `serial_number__icontains`, emitting
`{"type": "component", "id", "label": serial_number, "detail": kind_label}`.
Frontend: one clause in the `pathFor` ternary chain (`App.tsx:1565`) and the
hardcoded field label on the next line — currently "Search assets, work orders,
parts, and vendors" — must change too. This also lays the groundwork for the
camera-barcode sub-project, which needs serial lookup.

**2. `WorkOrderCloseSnapshot` freezes the asset's meters.**
`_close_snapshot_payload` (`maintenance/services.py:1684-1772`) freezes the work
order, tasks, labor, stock, reservations, attachments and comments — but the only
usage figure anywhere in it is the nullable `completion_meter_id`, set only when
the caller passes `completion_meter`. So a non-PM repair closed without one
leaves a permanent record with no mileage at all, while
`ComponentInstallation.installed_meters` freezes meters by value on the very same
swap. Add one key, `"asset_meters": _meter_snapshots_as_of(asset, closed_at)`,
reusing this spec's own helper. Additive under the existing
`"schema_version": 1`, and consistent with ADR 0002 (append a fact, never rewrite
one). Doing it after warranty ships means a backfill against `MeterReading` rows
that can no longer be tied to a close.

## Approaches considered

- **Minimal seam** (33/32/30): two models in the `DeviceAssetAssociation` shape
  only. Rejected: technicians locked out, no work-order link (warranty would
  need a second migration), open rows fully mutable, server-side rejection of
  legacy serial keys breaks the existing equipment form.
- **Ledger / immutability first** (33/28/30): append-only event rows with a
  void verb, derived usage, archive endpoint. Rejected: over-built for a
  Freq-2 event class, "one asset at a time" not enforced by the database,
  retirement-grade evidence blocks field work.
- **Field user first** (41/40/40) — chosen, with grafts above.

## Columns deliberately not added

Reviewed against ERPNext (`Serial No`, `Installation Note`, `Asset Movement`,
`Warranty Claim`) and Snipe-IT (components vs accessories vs consumables). Each
was proposed, each is refused for now, and the reason is recorded so the warranty
sub-project does not re-argue it:

| Proposed | Refused because |
|---|---|
| `ComponentInstallation.removal_kind` (failure / planned / asset_retired) | Its only consumer is warranty, which is undesigned. `removal_reason` is required-iff-removed free text and at ~10-30 removals a year one person classifies the backlog in ten minutes, so no information is lost by waiting. The "one-way door" argument for landing it before the trigger does not hold: DROP + CREATE TRIGGER inside one migration is atomic on PostgreSQL. |
| `Component.warranty_expires_at` / `warranty_meter_limit` | Coverage at this fleet is "whichever comes first" — a date *and* a meter limit, and an APU or reefer is hours-only. Neither the axis nor the base is settled (see warranty notes below), so three nullable columns with no writer and no reader would bake in a guess. `Component` has no write-once trigger; adding them later is a plain `AddField`. |
| `Component.part` FK to `inventory.Part` | `Part` is a catalogue row and `VendorPart` (`purchasing/models.py:57`) lists many vendors per part, so the link delivers neither who sold *this* serial nor for how much. It would reverse decisions 1 and 9 for nothing. |
| `Component.retired_at` | All three design judges called it a dead field. Add it with a scrap workflow. |

## Warranty sub-project — constraints established here

The next sub-project is warranty recovery. These four points are settled now, by
this design, and must not be re-derived:

1. **A claim line references a `ComponentInstallation`, not a `Component`** —
   unique `(claim, installation)`. The write-once row already carries a stable
   id plus frozen `installed_at` and `installed_meters`, which is exactly the
   evidence a claim needs.
2. **Coverage starts at acquisition, not at installation.** A manufacturer term
   runs from when the part was bought. Never derive a coverage window from an
   installation period, and a reinstall does not restart one.
3. **The coverage test runs against a supplied failure time, not the filing
   time.** A claim filed in March for a February failure must be judged as of
   February.
4. **Vendor and price for a serial are not derivable from the catalogue.**
   Capture them on the claim itself and attach the invoice through the existing
   document library. Do not serialize inventory to reach them.

Two hazards to resolve *in* that design, not before it:

- **Meter axis under transfer.** A mileage limit measured against one truck's
  odometer is meaningless once the component moves to another truck. Whether a
  limit runs against the host odometer since install (OEM factory-fitted) or
  against usage summed across installation periods (reman/replacement) is
  genuinely open, and is why coverage is not three columns today.
- **No warranty rule may auto-create a work order.** ADR 0004's "evidence, never
  an automatic diagnosis" holds only because a human converts. Raise coverage
  warnings through `record_alert_occurrence(..., source_type=...)`
  (`maintenance/services.py:2082`), which already does the upsert, the
  occurrence bump, the audit and the emit — no new table, no new notification
  path, and no cron until someone asks to be warned early.

## Notes

- Project is not under git; spec saved, not committed.
- ruff (E/F/I/B/S/DJ, 100 cols) and mypy (strict on non-`tests.py`) must pass;
  E2E suite runs under `make verify`.
