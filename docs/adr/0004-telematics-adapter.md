# ADR 0004: Replaceable telematics adapter

- Status: accepted
- Date: 2026-09-03

AutoPi enters through a public adapter endpoint and the same normalized ingestion contract future vendors use. Authentication identity, not the payload device ID, selects the device. Raw messages remain immutable; normalized events drive meter validation. Duplicate and late events remain traceable without changing the current meter incorrectly.

The MVP ingests meters and records suspect values. DTC-to-work-order automation is excluded; any later DTC alert remains human-reviewed.

