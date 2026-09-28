# Prior art

Six open-source projects solve pieces of what Fleetline solves. This records how
each one actually works, what we took from it, and — just as important — what we
refused and why, so the same arguments are not re-run every time someone
discovers one of them.

Read at 2026-09-05. Findings were produced by reading each project's source, then
adversarially re-checked against Fleetline's code and ADRs; several confident
claims about "gaps" turned out to be things Fleetline already does under a
different name, and those are recorded here too.

| Project | License | Stack | Overlaps |
|---|---|---|---|
| [InvenTree](https://github.com/inventree/InvenTree) | MIT | Django 5 / DRF | `inventory`, `purchasing` |
| [ERPNext](https://github.com/frappe/erpnext) | GPLv3 | Frappe / Python | components, warranty, assets |
| [Atlas CMMS](https://github.com/Grashjs/cmms) | AGPLv3 + commercial | Spring Boot / React | `maintenance` |
| [Traccar](https://github.com/traccar/traccar) | Apache 2.0 | Java | `integrations` |
| [Snipe-IT](https://github.com/snipe/snipe-it) | AGPLv3 | Laravel / PHP | `assets`, `core` audit |
| [LubeLogger](https://github.com/hargata/lubelog) · [Odoo Fleet](https://github.com/odoo/odoo/tree/master/addons/fleet) | MIT · LGPLv3 | .NET · Python | cost reporting, fleet UX |

There is no open-source equivalent to Fleetline as a whole. The closest single
project, Atlas CMMS, is a generic factory CMMS with no concept of a truck.

---

## InvenTree — parts and stock

**How it works.** Three-way split: `Part` is the catalogue abstraction (name,
IPN, revision, `minimum_stock`, `default_location`); `StockItem` is a physical
quantity of a Part at a `StockLocation`, carrying `serial`, `batch`,
`supplier_part`, `purchase_order`, `purchase_price`; `StockLocation` is an MPTT
tree. Manufacturer and supplier identity are separate models again
(`ManufacturerPart` MPN, `SupplierPart` SKU), so one part has many vendor
identities.

**Taken.**
- **Provenance belongs on the physical unit, not the catalogue row.** This is the
  argument that killed a proposed `Component.part` FK: `Part` is a catalogue row
  and `VendorPart` lists many vendors per part, so the link would say neither who
  sold *this* serial nor for how much. Recorded in the component spec.
- `minimum_stock` plus an `on_order` view netted against open PO lines — this is
  our own unbuilt INV-09. Backlogged, not invented here.

**Refused.**
- `Part.default_bin`. InvenTree's `default_location` is a receipt *destination*;
  our mandatory bin on an issue is a *source* that must actually hold stock, so a
  stale default would preselect an empty bin and fail. The click is better saved
  on the frontend, where the Inventory page already has every `StockBalance`.
- A stored `received` counter on PO lines. Reversals write negative `ReceiptLine`
  rows, so summing stays authoritative and cannot drift.

**We do it better.** `StockItem` is ~40 fields spanning identity, quantity,
location, purchasing, sales, builds, expiry, packaging, ownership and test
results, with a 130-line `clean()`. More importantly its **quantity is mutated in
place** and `StockItemTracking.deltas` is untyped JSON *describing* the change —
you cannot rebuild a balance from the entries. Our `Part` / `Bin` /
`StockBalance` / `StockTransaction` split with a real ledger is the ADR 0002
position and is strictly stronger.

---

## ERPNext — serialized units and warranty

**How it works.** The physical unit is the centre. `Serial No` is autonamed
`field:serial_no` — the serial string *is* the primary key — and everything hangs
off it: what it is, where it is, what it cost, and how long it is covered
(`warranty_period`, `warranty_expiry_date`, `amc_expiry_date`, plus a derived
`maintenance_status`). Movement is a submitted document (`Asset Movement`) and
the current position is a recomputed cache, not the source of truth.

**Taken.** Four constraints now written into the component spec's warranty
section: a claim line references an *installation*, not a component; coverage
starts at acquisition, not installation; the coverage test runs against a
supplied *failure* time, not the filing time; vendor and price for a serial are
captured on the claim because the catalogue cannot supply them.

**Refused.**
- **Their `Warranty Claim` points the wrong way.** It is a *customer* complaint
  record — `customer` is required, it carries territory and service address, and
  it has no vendor, no amount claimed and no amount recovered. It models "my
  customer claims against what I sold". We need "I claim against my supplier".
  Copy the linkage, none of the fields.
- **A stored derived status needs a cron.** `maintenance_status` is written, so
  it needs a scheduled job to stay true. We compute coverage on read.
- Date-only coverage. Correct for equipment sold by date; wrong for trucks, where
  coverage is "whichever comes first" and an APU or reefer is hours-only.

**We do it better.** Movement-as-document with a position cache means the cache
can drift from the documents. Our `ComponentInstallation` period row *is* the
truth, with a partial unique index enforcing one open installation per component
in the database rather than in a recompute.

---

## Atlas CMMS — work orders and PM

**How it works.** One `WorkOrderBase` mapped superclass carries the shared shape;
`WorkOrder`, `Request` and `PreventiveMaintenance` all extend it, so approving a
request or generating from a PM template is a field copy. Scheduling is delegated
wholesale to Quartz, which brings **misfire policies** with it.

**Taken.**
- **Missed-occurrence semantics.** Atlas thought explicitly about a schedule
  falling behind and answered "skip the missed slots". Our `_reset_plan` advances
  a `scheduled` baseline by exactly one interval per close, so a plan three
  intervals behind needs three open-and-close cycles to catch up and reads
  Overdue throughout. This is a real bug — queued.
- **Reading staleness as a modelled property.** `Meter.updateFrequency` is
  non-null and validated, so Atlas always knows how old a reading is. Our
  `calculate_plan_due` never looks at `observed_at`, so a frozen meter reports a
  plan as Current forever. Queued, as a one-constant staleness escalation rather
  than a per-meter column.
- Readable work-order numbers. Ours are eight hex digits of a UUID, which nobody
  can read aloud over a radio or match on an outside shop's invoice.

**Refused.** Quartz. A scheduler dependency to express intervals we already
compute.

**We do it better.** Atlas **cannot express "every 90 days or every 25,000 miles,
whichever comes first"** in one object — the time `Schedule` and the
`WorkOrderMeterTrigger` are separate entities on separate tables and nothing
reconciles them. That combination is the *normal* case for a truck, and our
`MaintenancePlan` + N `MaintenanceTrigger` rows folded by "most urgent wins" is
the right shape. Atlas also keeps two sources of truth for parts on a work order.

---

## Traccar — telematics ingest

**How it works.** Three layers. A `Position` is a **full state snapshot** — a
fixed column set plus everything else in one JSON `attributes` map, with a public
constant vocabulary pinning keys *and units* (`KEY_ODOMETER` in metres,
`KEY_HOURS` in **milliseconds**). An `Event` is a **transition**, not a snapshot,
and carries `positionId` back to the evidence. Transition state
(`motionState`, `overspeedState`) lives on the device row.

**Taken.**
- **The snapshot/transition distinction inside diagnostics.** A DM1 message means
  "here is the complete active fault set right now", but `_normalize_diagnostic`
  treats every code in every message as an independent occurrence and never
  observes a code *stopping*. So a coolant alert stays open after the fault
  self-clears, and the 24h cooldown re-raises it. Queued.
- **Device liveness as a first-class signal.** Queued as a row in the existing
  data-quality exceptions list, not as an alert — see below.

**Refused.** Routing silent-device detection through `MaintenanceAlert`. That
queue's Convert button creates maintenance requests *against the truck*, and a
dead dongle is not a truck defect. It also has no cooldown on that path, so a
dismissed alert would regenerate every worker tick.

**We do it better.** Traccar's maintenance model is genuinely worse and copying
it would be a regression: `Maintenance.type` is an unvalidated string naming a
Position attribute key, `getValue()` returns `0.0` for a missing key, and the
guard then **silently skips** — so a typo'd or unsupported meter produces a
reminder that never fires and never complains. Our `MaintenanceTrigger.meter` is
a real FK and `clean()` rejects a meter trigger without one.

### J1939 decoding — the licensing reality

Researched so it is not researched again. **No permissively-licensed SPN
dictionary of useful size exists.** `pretty_j1939` is Apache-2.0 and its bundled
`J1939db.json` carries a ~90-entry source-address table but only ~11 SPNs —
covering 5 of our 9 current rules (100, 110, 168, 175, 190) but not 111, 94 or
1761. isobus.net is ISO copyright and not redistributable. The only legal bulk
paths are the SAE J1939DA or a commercial DBC. **Buy neither.** Expand
`diagnostics.py` from codes that actually appear on this fleet. If the deployed
box turns out to forward DM1 lamp status, request it at the *frame* level — one
lamp state per message, not per code — as the cheap escalation path for unlisted
codes. Also recorded in `validation-assumptions.md`.

---

## Snipe-IT — asset custody and audit

**How it works.** Four tables split by *identity and return semantics*, not by
what the thing is. `Asset` is the only serialized row. Their **`Component` is
quantity-tracked, not serialized** — a serialized part installed in a truck is,
in their model, an Asset checked out to an Asset. Status is indirected through a
`Statuslabel` row holding three booleans that fold into four meta-statuses.

**Taken.**
- **Audit-row provenance.** Every Actionlog carries `remote_ip`, `user_agent` and
  a derived `action_source`. Queued — though the claim that our `AuditEvent.source`
  is always `"web"` turned out to be **false**: an AST scan found 15 call sites
  passing a real source (`autopi`, `worker`, `offline_sync`, caller-supplied). The
  one genuine gap is browser session vs API token, and the token is already on the
  actor object.
- Recording the **new** value alongside the old in `asset.updated` audit context.

**Refused.** Their component model, deliberately. Naming a quantity-tracked
consumable a "Component" is exactly the collision our spec avoids.

**We do it better.** **Snipe-IT's check-in deletes the period.**
`ComponentCheckinController::store()` decrements the pivot in place and hard
`DELETE`s the row at zero, with no soft delete. The only surviving trace is a log
line, so Snipe-IT **structurally cannot answer "what was installed on this asset
on 3 March"**. Our write-once `ComponentInstallation` with a filled-once removal
half exists precisely to answer that, and this is the clearest external
justification for it.

---

## LubeLogger and Odoo Fleet — what fleet operators actually want

**How LubeLogger works.** One table shape repeated. `GenericRecord` is
`{Id, VehicleId, Date, Mileage, Description, Cost, Notes, Files, Tags,
ExtraFields}`, and `ServiceRecord`, `UpgradeRecord` and `CollisionRecord` are
literally empty subclasses. The buckets exist *only* so the report helper can sum
them separately into `CostTableForVehicle`: `TotalDistance`, per-bucket
`…PerMile` and `…PerDay`, `TotalPerMile`, `TotalCost`. **That table is the
beloved feature.** Fuel MPG is done properly — partial fills accumulate and
resolve at the next fill-to-full, with a `MissedFuelUp` flag that resets the
accumulator.

**Taken.** **Money attached to a truck.** Both projects answer "what has TRK-012
cost, and per mile". Fleetline answers neither: labor sums fleet-wide, issued
parts sum fleet-wide, and nothing rolls up per asset or divides by distance —
verified, not assumed. Every input already exists, so this is a read-side gap.
Queued as a per-asset cost rollup on the operations report, split scheduled vs
unscheduled from the existing `maintenance_plan` FK, behind `financial.view`, and
labelled **in-house cost** because outside invoices have nowhere to live yet.

**Refused.** Fuel and MPG tracking. Real gap, genuinely absent, and still not
worth a `GasRecord` table plus tank-to-tank accumulation logic until someone asks
for it. The odometer half is what the cost rollup needs.

**We do it better.** **LubeLogger's odometer adjustment rewrites stored facts** —
it loops every record applying `Mileage += difference; Mileage *= multiplier` and
saves; the original reading is gone. Odoo is worse in a different way: it hard-
raises when a new odometer reading is lower than the last, so a cluster swap or a
typo is simply unenterable. Our immutable `MeterReading` with a `corrects`
self-FK, quarantining implausible readings as `SUSPECT` rather than refusing
them, is right on both counts and must not be traded away for their convenience.

---

## What this changed

Applied to `docs/superpowers/specs/2026-09-05-component-tracking-design.md`
before the build:

- The write-once trigger's `ROW()` comparison now leads with the primary key,
  matching the inspection guard it copies. A column absent from that list is
  silently freely updatable.
- `ComponentInstallation.ordering` gained a deterministic tie-break. The backfill
  sets `installed_at = asset.created_at` for every equipment section, so a truck
  with an engine and a transmission serial produces two rows with identical
  timestamps on day one.
- The backfill now rejects placeholder junk (`"N/A"`, `"NONE"`, `"0"`, `"SEE
  PLATE"`, anything under 4 characters) instead of only empty strings.
- `(organization, kind, serial_number)` is kept, with its ceiling and upgrade
  path written down. Narrowing to `(organization, serial_number)` was proposed
  and refused: it would silently collapse two physical parts into one row at
  migration time wherever legacy junk repeats.
- New sections: "Columns deliberately not added" and "Warranty sub-project —
  constraints established here".
- Two cross-app items folded into the same delivery: component serials reach
  `global_search`, and `WorkOrderCloseSnapshot` freezes the asset's meters.

Queued against already-built code, in recommended order:

1. PM missed-occurrence skip (`maintenance/services.py::_reset_plan`) — real bug
2. Meter staleness escalation in `calculate_plan_due` — real bug
3. Per-asset cost and cost-per-mile rollup (`core/reporting.py`)
4. Diagnostics: a message is the complete active fault set; record codes going
   inactive
5. Silent-device row in the data-quality exceptions list
6. Readable per-organization work-order numbers
7. `audit()` provenance: session vs API token; new-value-alongside-old context
8. `LaborEntry` minutes must agree with its own timestamps (API-boundary guard)
9. `PurchaseOrderLine.quantity_received` prefetch-safe body
10. Delete the two `Reservation` statuses nothing can ever set
11. `PATCH /maintenance/plans/{id}/ {"active": bool}` — the cluster-swap recovery
    path
