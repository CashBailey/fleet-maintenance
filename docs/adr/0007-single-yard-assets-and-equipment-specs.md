# ADR 0007: Single-yard asset workflow and structured equipment specifications

- Date: 2026-09-04

Assets do not expose a home-location field in the current single-yard workflow. The existing organization location model remains for warehouses and future multi-yard operation, so historic data and a later expansion do not require a destructive migration.

The required truck master fields are unit number, VIN, make, model, and truck type. Equipment-specific values are retained in the existing asset `specs` JSON field under `equipment` rather than introducing premature component tables. The first visible engine fields are `equipment.engine.type` and `equipment.engine.serial_number`.

Before adding further fields, validate them against the actual truck roster and manuals. Candidate groups are engine configuration, transmission, axle/ratio, emissions aftertreatment, electrical/alternator, brake system, tire/wheel, PTO/hydraulics, body/upfit, and component serial numbers. A distinct install/remove history is deferred until the fleet needs serviceable-component lifecycle tracking.
