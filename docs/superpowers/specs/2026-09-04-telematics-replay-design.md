# Telematics replay tool — design

**Status:** approved in conversation 2026-09-04. First of five sub-projects
(replay → fault alerts → components → warranty → camera barcode).

## Problem

Fleetline ingests AutoPi messages through `integrations.services.ingest_autopi`,
but no device is deployed. Nothing exercises that path outside unit tests, and
the research report's acceptance plan (Deliverable 25) requires that "recorded
sample data can be replayed without production device", plus duplicate,
out-of-order and meter-correction cases.

## What we build

A Django management command that feeds a file of saved messages through the
**same** ingest entry point real devices use, and reports what happened.

```
manage.py replay_telematics <file> --device <external_id> [--organization <slug>] [--dry-run]
```

- `<file>`: JSON Lines (one message object per line) or a single JSON array.
  Blank lines and `#`-prefixed lines are ignored.
- `--device`: the `Device.external_id` we claim sent the messages. Required.
  `--organization` disambiguates if the same external id exists in more than
  one organization; otherwise an ambiguous match is an error.
- `--dry-run`: runs everything, then rolls the transaction back. Zero rows
  written — including outbox events and device counters.

### Output

One line per message: index, `messageId`, outcome
(`accepted` / `duplicate` / `rejected <code>`), and for accepted messages the
normalized events with their quality (`accepted`, `quarantined`, …).
Then a summary block: totals per outcome, per event kind, per quality, and a
`DRY RUN — nothing written` footer when applicable. Exit code is non-zero if
the file could not be read or parsed as JSON at all; individual rejected
messages do not fail the command (rejection is a result, not an error).

### Identity handling

`ingest_autopi` rejects a message whose `organizationId`/`deviceId` do not
match the authenticated device. The replay tool **keeps that check** — a tool
that accepts anything proves nothing.

Shipped sample files therefore cannot hard-code UUIDs. They use the literal
placeholders `{{organizationId}}` and `{{deviceId}}`, which the command
substitutes with the target device's real values before parsing. Substitution
replaces only those exact placeholders; real recorded files carry real ids and
are replayed untouched.

### Sample files — `backend/integrations/samples/`

| File | Purpose |
|---|---|
| `healthy-truck.jsonl` | 3 telemetry messages, rising odometer + engine hours |
| `overheating.jsonl` | telemetry then a `diagnostics` message with J1939 SPN 110 / FMI 0 |
| `malformed.jsonl` | mix of valid, wrong `schemaVersion`, missing `observedAt`, bad unit |
| `duplicate-pair.jsonl` | the same message twice (same `messageId`, same content) |
| `out-of-order.jsonl` | two readings where the later-observed one appears first |

Timestamps are fixed ISO-8601 in the past. Meter plausibility is judged
against whatever readings the target asset already has, so replaying a low
odometer onto an asset with higher history will quarantine it. That is real
behavior; the tool reports it rather than hiding it.

## What we deliberately do not build

- Live capture from a device (none exists yet).
- Any HTTP endpoint or UI.
- New models or migrations.
- Fault-code → alert evaluation (sub-project 2).
- Identity rewriting for real recorded files.
- Machine-readable (`--json`) output. Tests assert on database state, not
  stdout. Add when a consumer needs it.

## Files

- `backend/integrations/management/__init__.py`, `management/commands/__init__.py`
- `backend/integrations/management/commands/replay_telematics.py`
- `backend/integrations/samples/*.jsonl`
- `backend/integrations/test_replay.py`
- `README.md`: one short paragraph under the existing operations notes.

## Tests (`test_replay.py`, Django `TestCase`)

1. `healthy-truck` lands the expected `MeterReading` rows on the associated
   asset's meters, via the real association path.
2. `malformed` rejects the bad messages with the expected error codes, accepts
   the good one, and the command exits 0.
3. Replaying `duplicate-pair` — and replaying `healthy-truck` twice — creates
   exactly one accepted `TelematicsMessage` per distinct message.
4. `out-of-order`: both readings persist; the meter's current value is the
   latest-observed reading, not the last-ingested one.
5. `--dry-run` leaves `TelematicsMessage`, `NormalizedTelematicsEvent`,
   `MeterReading`, `OutboxEvent` counts and the device's counters unchanged.
6. Unknown `--device`, ambiguous device without `--organization`, unreadable
   file and non-JSON content each raise `CommandError`.
7. Placeholder substitution: a file with `{{organizationId}}`/`{{deviceId}}`
   replays cleanly; a file with a *different* real organization id is rejected
   with `identity_mismatch`.

Tests 3 and 4 map to the report's "Duplicate sync" and "Out-of-order
telemetry" acceptance rows; test 1 supports "AutoPi replay".

## Notes

- The project is not under version control, so this spec is saved but not
  committed.
- `ruff` (E/F/I/B/S/DJ, line length 100) and `mypy` (strict on non-`tests.py`
  files) must pass; `test_replay.py` is type-checked.
