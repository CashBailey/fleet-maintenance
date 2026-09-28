# ADR 0011: Serviceable component tracking

- Status: accepted
- Date: 2026-09-05
- Amends: ADR 0007

ADR 0007 stated: "A distinct install/remove history is deferred until the fleet needs serviceable-component lifecycle tracking." That condition is now met. Requirement AST-05 asks for per-component service history, and the warranty sub-project that follows this one needs a stable record to attach coverage and claims to — a JSON key is not one.

## Decision

A serviceable major part — engine, transmission, reefer unit, APU, axle, aftertreatment, other — is its own `Component` record, identified by kind, serial number, make and model. Identity only: no cost, vendor, part link or warranty fields.

Each period a component spends on an asset is one `ComponentInstallation` row carrying the asset, the time, the actor, an optional work order, and a frozen copy of the asset's odometer and engine-hour readings at that moment. The install half is written once. The removal half — time, actor, work order, meters, and a required reason — is filled exactly once. A mistake is corrected by removing with a reason and reinstalling; history is never edited (ADR 0002).

Meter evidence is best-effort: the latest accepted, uncorrected reading at or before the event time, per active cumulative meter. A missing reading is omitted, never invented, and never blocks a technician. Retirement keeps its own strict required-readings check.

A technician (`maintenance.execute`) may install or remove only through an open work order on that asset where they are an active assignee. A manager (`assets.manage` or `maintenance.manage`) may act with or without a work order.

Retiring an asset closes its open installations with the retirement meter snapshot and the reason "Asset retired".

## Consequences and boundaries

**(a) A third shape.** This is a third kind of time-bound record, between `DeviceAssetAssociation` (a period row whose close is fully mutable) and `AssetStatusEvent` (pure append-only facts). `ComponentInstallation` is a period row with a database write-once guard. The shape was chosen because it is the only one that lets the database enforce "one open installation per component" — via `UniqueConstraint(fields=["component"], condition=Q(removed_at__isnull=True))` — while still giving warranty a single stable row to point at. The trade is a genuinely new pattern in a codebase that already has two; the alternatives could not do both.

**(b) The database is the boundary, not the service.** Trigger `component_installation_write_once` rejects every DELETE and every UPDATE except the single removal fill. Its `ROW()` comparison leads with the primary key, matching `maintenance/migrations/0004_inspection_immutability_guards.py`. Any future column on this table requires re-creating that trigger — a column absent from the comparison is silently freely updatable. Re-creating it is an ordinary `RunSQL` DROP + CREATE inside one migration and PostgreSQL DDL is transactional, so there is no window where the guard is absent.

**(c) `assets` may reference `maintenance.WorkOrder` by string FK.** `inventory` already crosses into `maintenance`; this does not introduce a new dependency direction.

**(d) Transfer is remove-then-install.** There is no fourth verb. Moving a component between trucks is two calls, and the history reads correctly either way.

**(e) Serial uniqueness is `(organization, kind, serial_number)`.** Widening the key — adding manufacturer — is a lossless follow-up. **Narrowing it to `(organization, serial_number)` is not**: that needs a row merge. Narrowing was considered and refused because the legacy backfill walks free-text equipment fields, and two sections both holding `"N/A"` would silently collapse into one `Component` representing two different physical parts, at migration time, with no human in the loop. The backfill's placeholder guard — rejecting a known junk set and anything under four characters — is what makes the current key safe.

**(f) `Component` is the source of truth for serials.** The legacy `specs.equipment.*.serial_number` keys stay in the JSON and are never rewritten; the API still accepts them so existing integrations do not break. The UI stops writing and displaying them. `specs.equipment.engine.type` and the other descriptive values remain authoritative — the document library reads `engine.type`.

## Deliberately not built

Recorded so the warranty sub-project does not re-argue them: no `removal_kind` classification (its only consumer is warranty, which is undesigned, and `removal_reason` is required free text that one person can classify later); no warranty date or meter columns on `Component` (coverage at this fleet is "whichever comes first" and neither the axis nor the base is settled); no `Component.part` FK to `inventory.Part` (a catalogue row cannot say who sold *this* serial or for how much); no `Component.retired_at`; no shelf location for removed components, lifetime-hours math, position/slot labels, component search page, offline install/remove, void-and-replace verb, or per-component PM plans.

See `docs/prior-art.md` for the ERPNext and Snipe-IT reading behind several of these — in particular Snipe-IT's component check-in, which hard-deletes the period row and therefore cannot answer "what was installed on this asset on 3 March". That is the clearest external case for this table being write-once.
