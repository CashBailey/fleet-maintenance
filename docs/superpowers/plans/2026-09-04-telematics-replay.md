# Telematics Replay Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `manage.py replay_telematics` command that feeds a file of saved AutoPi messages through the real `ingest_autopi` path and reports what happened, with a `--dry-run` that writes nothing.

**Architecture:** One Django management command in the `integrations` app. It loads JSON Lines or a JSON array, substitutes `{{organizationId}}`/`{{deviceId}}` placeholders with the target device's real ids, calls `integrations.services.ingest_autopi` per message, and prints per-message outcomes plus a summary. Dry-run wraps the whole loop in `transaction.atomic()` and rolls back. No new models, no HTTP.

**Tech Stack:** Django 5.2 management commands, `django.test.TestCase`, PostgreSQL 16 (tests need a live server), ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-04-telematics-replay-design.md`

## Global Constraints

- Python 3.12; `ruff` rules E/F/I/B/S/DJ, line length 100 (`pyproject.toml`).
- `mypy` with django-stubs runs on every non-`tests.py` file — `test_replay.py` **is** type-checked, so annotate every test method (`-> None`) and helper.
- **The project is not under git.** There are no commit steps. Each task ends by running the test file plus ruff and mypy instead.
- The identity check in `ingest_autopi` (payload `organizationId`/`deviceId` must match the device) is preserved. Only the exact placeholder strings are ever substituted.
- Sample timestamps are fixed ISO-8601 in August 2026. Test associations start 2026-01-01 so every sample falls inside the association window.

### How to run the backend tests

Tests need PostgreSQL. Nothing listens on 5432 by default, so start the bundled one in a scratch directory (the helper exports `POSTGRES_*` for you):

```bash
cd /home/gatorhub/fleet_maint_track
source scripts/postgres-test-lib.sh
PGDIR=$(mktemp -d "$PWD/.e2e/replay.XXXXXX")
fleetline_start_postgres "$PGDIR" "$(free_port)" fleetline_replay
export DJANGO_SECRET_KEY="test-only-secret-key-not-valid-for-any-deployment-0123456789"
cd backend && ../.venv/bin/python manage.py test integrations.test_replay -v 2
```

When finished: `fleetline_stop_postgres "$PGDIR" && rm -rf "$PGDIR"`.

Lint/type checks, from the repo root:

```bash
.venv/bin/ruff check backend/integrations && .venv/bin/ruff format --check backend/integrations
.venv/bin/mypy backend/integrations
```

---

## File map

| File | Responsibility |
|---|---|
| `backend/integrations/management/__init__.py` | empty; makes the package discoverable |
| `backend/integrations/management/commands/__init__.py` | empty |
| `backend/integrations/management/commands/replay_telematics.py` | the command: load file, resolve device, substitute placeholders, replay, report, dry-run |
| `backend/integrations/samples/*.jsonl` | five shipped sample files |
| `backend/integrations/test_replay.py` | all tests for the command |
| `README.md` | one row in the Common commands table |

---

### Task 1: Command skeleton — file loading, device resolution, errors

**Files:**
- Create: `backend/integrations/management/__init__.py` (empty)
- Create: `backend/integrations/management/commands/__init__.py` (empty)
- Create: `backend/integrations/management/commands/replay_telematics.py`
- Create: `backend/integrations/samples/healthy-truck.jsonl`
- Test: `backend/integrations/test_replay.py`

**Interfaces:**
- Consumes: `integrations.models.Device` (fields `external_id`, `organization`), `core.models.Organization.slug`.
- Produces, for later tasks:
  - `load_messages(path: Path) -> list[object]` — raises `CommandError` on unreadable/non-JSON input
  - `substitute_identity(message: object, *, device: Device) -> object`
  - `resolve_device(*, external_id: str, organization_slug: str | None) -> Device`
  - constants `ORG_PLACEHOLDER = "{{organizationId}}"`, `DEVICE_PLACEHOLDER = "{{deviceId}}"`
  - `Command` with args `file`, `--device` (required), `--organization`, `--dry-run`

- [x] **Step 1: Create the sample file**

`backend/integrations/samples/healthy-truck.jsonl` — three lines, no wrapping:

```
{"schemaVersion":"1.0","messageId":"healthy-001","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-20T12:00:00+00:00","sentAt":"2026-08-20T12:00:05+00:00","sequence":1,"source":"autopi","type":"telemetry","values":{"odometer":{"value":120450,"unit":"mi"},"engineHours":{"value":6100.5,"unit":"h"}}}
{"schemaVersion":"1.0","messageId":"healthy-002","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-20T16:00:00+00:00","sentAt":"2026-08-20T16:00:05+00:00","sequence":2,"source":"autopi","type":"telemetry","values":{"odometer":{"value":120610,"unit":"mi"},"engineHours":{"value":6104.0,"unit":"h"}}}
{"schemaVersion":"1.0","messageId":"healthy-003","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-21T08:00:00+00:00","sentAt":"2026-08-21T08:00:05+00:00","sequence":3,"source":"autopi","type":"telemetry","values":{"odometer":{"value":120790,"unit":"mi"},"engineHours":{"value":6108.5,"unit":"h"}}}
```

(160 mi in 4 h and 180 mi in 16 h stay under the 100 mi/h plausibility ceiling; 3.5 h and 4.5 h of engine time stay under 1.25 h/h.)

- [x] **Step 2: Write the failing tests**

`backend/integrations/test_replay.py`:

```python
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from assets.models import Asset, AssetType, Meter
from core.models import Organization, Role, User
from django.core.management import CommandError, call_command
from django.test import TestCase

from .models import Device, DeviceAssetAssociation

SAMPLES = Path(__file__).resolve().parent / "samples"


class ReplayTelematicsTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Gator Hauling", slug="gator")
        role = Role.objects.create(
            organization=self.organization,
            slug="integration_admin",
            name="Integration administrator",
        )
        self.admin = User.objects.create_user(
            username="integration",
            password="unused-test-password",  # noqa: S106
            organization=self.organization,
        )
        self.admin.roles.add(role)
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.organization, asset_type=asset_type, unit_number="TRUCK-107"
        )
        self.odometer = Meter.objects.create(
            organization=self.organization,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        self.engine_hours = Meter.objects.create(
            organization=self.organization,
            asset=self.asset,
            name="Engine hours",
            kind=Meter.Kind.ENGINE_HOURS,
            unit="h",
        )
        self.device = self.make_device(self.organization, external_id="autopi-107")
        DeviceAssetAssociation.objects.create(
            organization=self.organization,
            device=self.device,
            asset=self.asset,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            assigned_by=self.admin,
        )

    def make_device(self, organization: Organization, *, external_id: str) -> Device:
        token = f"fdev_{external_id}"
        return Device.objects.create(
            organization=organization,
            name=f"AutoPi {external_id}",
            provider="autopi",
            serial_number=f"AP-{external_id}",
            external_id=external_id,
            token_prefix=token[:12],
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )

    def replay(self, sample: str | Path, **options: object) -> str:
        out = StringIO()
        path = sample if isinstance(sample, Path) else SAMPLES / sample
        call_command(
            "replay_telematics", str(path), device="autopi-107", stdout=out, **options
        )
        return out.getvalue()

    # --- Task 1: loading, device resolution, errors -------------------------

    def test_unknown_device_is_an_error(self) -> None:
        with self.assertRaisesMessage(CommandError, "No device with external id 'nope'"):
            call_command(
                "replay_telematics", str(SAMPLES / "healthy-truck.jsonl"), device="nope"
            )

    def test_ambiguous_device_requires_organization(self) -> None:
        other = Organization.objects.create(name="Other Yard", slug="other")
        self.make_device(other, external_id="autopi-107")
        with self.assertRaisesMessage(CommandError, "pass --organization"):
            self.replay("healthy-truck.jsonl")
        # Disambiguated, it resolves and runs without raising.
        self.replay("healthy-truck.jsonl", organization="gator")

    def test_missing_file_is_an_error(self) -> None:
        with self.assertRaisesMessage(CommandError, "Cannot read"):
            self.replay(Path("/nonexistent/replay.jsonl"))

    def test_non_json_content_is_an_error(self) -> None:
        bad = Path(self._testMethodName + ".jsonl")
        bad.write_text("this is not json\n", encoding="utf-8")
        self.addCleanup(bad.unlink)
        with self.assertRaisesMessage(CommandError, "is not valid JSON"):
            self.replay(bad)

    def test_json_array_and_blank_or_comment_lines_are_accepted(self) -> None:
        from integrations.management.commands.replay_telematics import load_messages

        array = Path(self._testMethodName + "-array.json")
        array.write_text('[{"a": 1}, {"b": 2}]', encoding="utf-8")
        self.addCleanup(array.unlink)
        lines = Path(self._testMethodName + "-lines.jsonl")
        lines.write_text('# comment\n{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
        self.addCleanup(lines.unlink)
        self.assertEqual(load_messages(array), [{"a": 1}, {"b": 2}])
        self.assertEqual(load_messages(lines), [{"a": 1}, {"b": 2}])
```

- [x] **Step 3: Run the tests to verify they fail**

Run (from `backend/`, with Postgres up and `DJANGO_SECRET_KEY` exported as in Global Constraints):
`../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: every test errors with `Unknown command: 'replay_telematics'` (or `ModuleNotFoundError` for the load_messages import).

- [x] **Step 4: Create the package markers**

Create two empty files:
- `backend/integrations/management/__init__.py`
- `backend/integrations/management/commands/__init__.py`

- [x] **Step 5: Write the command with loading and resolution only**

`backend/integrations/management/commands/replay_telematics.py`:

```python
from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from integrations.models import Device

ORG_PLACEHOLDER = "{{organizationId}}"
DEVICE_PLACEHOLDER = "{{deviceId}}"


def load_messages(path: Path) -> list[object]:
    """Read a JSON array or JSON Lines file. Blank and #-prefixed lines are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"Cannot read {path}: {exc}") from exc
    if text.lstrip().startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, list):
            raise CommandError(f"{path} must be a JSON array or JSON Lines")
        return list(loaded)
    messages: list[object] = []
    for number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        try:
            messages.append(json.loads(candidate))
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} line {number} is not valid JSON: {exc}") from exc
    return messages


def substitute_identity(message: object, *, device: Device) -> object:
    """Replace only the exact placeholder strings; real ids pass through untouched."""
    if not isinstance(message, dict):
        return message
    result = dict(message)
    if result.get("organizationId") == ORG_PLACEHOLDER:
        result["organizationId"] = str(device.organization_id)
    if result.get("deviceId") == DEVICE_PLACEHOLDER:
        result["deviceId"] = device.external_id
    return result


def resolve_device(*, external_id: str, organization_slug: str | None) -> Device:
    devices = Device.objects.select_related("organization").filter(external_id=external_id)
    if organization_slug:
        devices = devices.filter(organization__slug=organization_slug)
    matches = list(devices.order_by("created_at")[:2])
    if not matches:
        raise CommandError(f"No device with external id {external_id!r}")
    if len(matches) > 1:
        raise CommandError(
            f"Device {external_id!r} exists in more than one organization; pass --organization"
        )
    return matches[0]


class Command(BaseCommand):
    help = "Replay a file of saved AutoPi messages through the real ingest path."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("file", type=Path, help="JSON Lines or JSON array of messages")
        parser.add_argument(
            "--device", required=True, help="Device.external_id that sent the messages"
        )
        parser.add_argument(
            "--organization", default=None, help="Organization slug, if --device is ambiguous"
        )
        parser.add_argument("--dry-run", action="store_true", help="Replay, then roll back")

    def handle(self, *args: object, **options: Any) -> None:
        device = resolve_device(
            external_id=options["device"], organization_slug=options["organization"]
        )
        messages = load_messages(options["file"])
        self.stdout.write(f"Loaded {len(messages)} messages for device {device.external_id}")
```

Note: `Device` inherits `created_at` from `OrganizationOwnedModel`; if `order_by("created_at")` errors, drop the `order_by` — the slice alone is enough.

- [x] **Step 6: Run the tests to verify they pass**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: all 5 tests PASS.

- [x] **Step 7: Lint and type-check**

Run from repo root:
```bash
.venv/bin/ruff check backend/integrations && .venv/bin/ruff format backend/integrations
.venv/bin/mypy backend/integrations
```
Expected: no errors. If ruff reformats anything, re-run the tests.

---

### Task 2: Replay through ingest and report outcomes

**Files:**
- Modify: `backend/integrations/management/commands/replay_telematics.py` (the `handle` method and new helpers)
- Create: `backend/integrations/samples/overheating.jsonl`
- Create: `backend/integrations/samples/malformed.jsonl`
- Test: `backend/integrations/test_replay.py`

**Interfaces:**
- Consumes: `integrations.services.ingest_autopi(*, device: Device, payload: object) -> IngestResult` where `IngestResult` has `message: TelematicsMessage`, `events: tuple[NormalizedTelematicsEvent, ...]`, `duplicate: bool`, `error_code: str`.
- Produces: `outcome_label(result: IngestResult) -> str` returning `"accepted"`, `"duplicate"`, or `"<status> (<error_code>)"` e.g. `"rejected (unsupported_unit)"`, `"quarantined (no_association)"`.

- [x] **Step 1: Create the sample files**

`backend/integrations/samples/overheating.jsonl`:

```
{"schemaVersion":"1.0","messageId":"overheat-001","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-21T09:00:00+00:00","sentAt":"2026-08-21T09:00:05+00:00","sequence":1,"source":"autopi","type":"telemetry","values":{"odometer":{"value":121000,"unit":"mi"},"engineHours":{"value":6115.0,"unit":"h"}}}
{"schemaVersion":"1.0","messageId":"overheat-002","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-21T09:05:00+00:00","sentAt":"2026-08-21T09:05:05+00:00","sequence":2,"source":"autopi","type":"diagnostics","values":{},"diagnostics":[{"protocol":"j1939","spn":110,"fmi":0,"ecu":"engine","occurrences":3}]}
```

`backend/integrations/samples/malformed.jsonl`:

```
# Intentionally broken messages, plus one good one at the end.
{"schemaVersion":"0.9","messageId":"bad-schema","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-22T10:00:00+00:00","source":"autopi","type":"telemetry","values":{"odometer":{"value":121100,"unit":"mi"}}}
{"schemaVersion":"1.0","messageId":"no-observed-at","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","source":"autopi","type":"telemetry","values":{"odometer":{"value":121100,"unit":"mi"}}}
{"schemaVersion":"1.0","messageId":"bad-unit","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-22T10:10:00+00:00","source":"autopi","type":"telemetry","values":{"odometer":{"value":121100,"unit":"furlongs"}}}
{"schemaVersion":"1.0","messageId":"good-one","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-22T10:20:00+00:00","source":"autopi","type":"telemetry","values":{"odometer":{"value":121100,"unit":"mi"}}}
```

- [x] **Step 2: Write the failing tests**

Append to `ReplayTelematicsTests` in `backend/integrations/test_replay.py`. Add these imports at the top of the file:

```python
from decimal import Decimal

from assets.models import MeterReading

from .models import NormalizedTelematicsEvent, TelematicsMessage
```

Then the tests:

```python
    # --- Task 2: replay and report --------------------------------------------

    def test_healthy_truck_lands_meter_readings_through_real_association(self) -> None:
        output = self.replay("healthy-truck.jsonl")

        accepted = TelematicsMessage.objects.filter(status=TelematicsMessage.Status.ACCEPTED)
        self.assertEqual(accepted.count(), 3)
        self.assertEqual(self.odometer.current_value, Decimal("120790.000"))
        self.assertEqual(self.engine_hours.current_value, Decimal("6108.500"))
        self.assertEqual(
            MeterReading.objects.filter(quality=MeterReading.Quality.ACCEPTED).count(), 6
        )
        self.assertIn("healthy-003 accepted", output)
        self.assertIn("accepted: 3", output)

    def test_overheating_records_diagnostic_evidence_without_alert(self) -> None:
        self.replay("overheating.jsonl")

        diagnostics = NormalizedTelematicsEvent.objects.filter(
            kind=NormalizedTelematicsEvent.Kind.DIAGNOSTIC
        )
        self.assertEqual(diagnostics.count(), 1)
        event = diagnostics.get()
        self.assertEqual(event.normalized_payload["spn"], 110)
        self.assertEqual(event.normalized_payload["fmi"], 0)
        self.assertTrue(event.signal.startswith("dtc:0:j1939:110:0"))

    def test_malformed_messages_are_rejected_with_codes_and_command_still_succeeds(self) -> None:
        output = self.replay("malformed.jsonl")

        rejected = TelematicsMessage.objects.filter(status=TelematicsMessage.Status.REJECTED)
        self.assertEqual(rejected.count(), 3)
        self.assertEqual(
            TelematicsMessage.objects.filter(status=TelematicsMessage.Status.ACCEPTED).count(), 1
        )
        self.assertIn("bad-schema rejected (unsupported_schema_version)", output)
        self.assertIn("no-observed-at rejected (invalid_timestamp)", output)
        self.assertIn("bad-unit rejected (unsupported_unit)", output)
        self.assertIn("good-one accepted", output)
        self.assertEqual(self.odometer.current_value, Decimal("121100.000"))
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: the three new tests FAIL (no `TelematicsMessage` rows are created; output lacks the expected lines). The Task 1 tests still pass.

- [x] **Step 4: Implement replay and reporting**

In `replay_telematics.py`, add to the imports:

```python
from collections import Counter

from integrations.services import IngestResult, ingest_autopi
```

Add a helper above `class Command`:

```python
def outcome_label(result: IngestResult) -> str:
    if result.duplicate:
        return "duplicate"
    if result.error_code:
        return f"{result.message.status} ({result.error_code})"
    return "accepted"


def message_label(raw: object, result: IngestResult) -> str:
    if result.message.message_id:
        return result.message.message_id
    if isinstance(raw, dict) and raw.get("messageId"):
        return str(raw["messageId"])
    return "<no messageId>"
```

Replace the `handle` method:

```python
    def handle(self, *args: object, **options: Any) -> None:
        device = resolve_device(
            external_id=options["device"], organization_slug=options["organization"]
        )
        messages = load_messages(options["file"])
        self.stdout.write(f"Replaying {len(messages)} messages as device {device.external_id}")

        outcomes: Counter[str] = Counter()
        qualities: Counter[str] = Counter()
        for index, raw in enumerate(messages, start=1):
            result = ingest_autopi(device=device, payload=substitute_identity(raw, device=device))
            label = outcome_label(result)
            outcomes[label] += 1
            self.stdout.write(f"{index:>4}  {message_label(raw, result)} {label}")
            for event in result.events:
                qualities[f"{event.kind}:{event.quality}"] += 1
                reason = f" — {event.reason}" if event.reason else ""
                self.stdout.write(f"        {event.kind} {event.signal} -> {event.quality}{reason}")

        self.stdout.write("")
        self.stdout.write("Summary")
        for label, count in sorted(outcomes.items()):
            self.stdout.write(f"  {label}: {count}")
        for label, count in sorted(qualities.items()):
            self.stdout.write(f"  events {label}: {count}")
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: all 8 tests PASS.

If `no-observed-at` reports a different code than `invalid_timestamp`, read the actual code from the output and fix the **test assertion** — the adapter's codes are the source of truth (`backend/integrations/adapters.py:47-56` raises `invalid_timestamp` for a missing `observedAt`).

- [x] **Step 6: Lint and type-check**

```bash
.venv/bin/ruff check backend/integrations && .venv/bin/ruff format backend/integrations
.venv/bin/mypy backend/integrations
```
Expected: clean. `NormalizedTelematicsEvent.reason` is a `TextField` — if mypy complains about the f-string, it is because the attribute name differs; check `backend/integrations/models.py` and use the real field name.

---

### Task 3: Duplicate and out-of-order acceptance cases

**Files:**
- Create: `backend/integrations/samples/duplicate-pair.jsonl`
- Create: `backend/integrations/samples/out-of-order.jsonl`
- Test: `backend/integrations/test_replay.py`

**Interfaces:**
- Consumes: the command as built in Task 2; `Meter.current_value` (latest accepted reading by `observed_at`).
- Produces: nothing new in code — these tasks prove behaviour the report names as acceptance tests ("Duplicate sync", "Out-of-order telemetry").

- [x] **Step 1: Create the sample files**

`backend/integrations/samples/duplicate-pair.jsonl` — the same message twice, byte-for-byte:

```
{"schemaVersion":"1.0","messageId":"dup-001","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-23T07:00:00+00:00","sentAt":"2026-08-23T07:00:05+00:00","sequence":1,"source":"autopi","type":"telemetry","values":{"odometer":{"value":121200,"unit":"mi"}}}
{"schemaVersion":"1.0","messageId":"dup-001","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-23T07:00:00+00:00","sentAt":"2026-08-23T07:00:05+00:00","sequence":1,"source":"autopi","type":"telemetry","values":{"odometer":{"value":121200,"unit":"mi"}}}
```

`backend/integrations/samples/out-of-order.jsonl` — the later-observed reading arrives first:

```
{"schemaVersion":"1.0","messageId":"ooo-later","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-23T14:00:00+00:00","sentAt":"2026-08-23T14:00:05+00:00","source":"autopi","type":"telemetry","values":{"odometer":{"value":121500,"unit":"mi"}}}
{"schemaVersion":"1.0","messageId":"ooo-earlier","organizationId":"{{organizationId}}","deviceId":"{{deviceId}}","observedAt":"2026-08-23T08:00:00+00:00","sentAt":"2026-08-23T14:00:05+00:00","source":"autopi","type":"telemetry","values":{"odometer":{"value":121300,"unit":"mi"}}}
```

(200 mi over 6 h is 33 mi/h, under the ceiling, so the late reading is accepted into history.)

- [x] **Step 2: Write the failing tests**

Append to `ReplayTelematicsTests`:

```python
    # --- Task 3: duplicate and out-of-order -----------------------------------

    def test_duplicate_pair_creates_one_message_and_one_reading(self) -> None:
        output = self.replay("duplicate-pair.jsonl")

        self.assertEqual(
            TelematicsMessage.objects.filter(status=TelematicsMessage.Status.ACCEPTED).count(), 1
        )
        self.assertEqual(MeterReading.objects.count(), 1)
        self.assertIn("duplicate: 1", output)
        self.device.refresh_from_db()
        self.assertEqual(self.device.duplicate_count, 1)

    def test_replaying_a_file_twice_creates_no_new_facts(self) -> None:
        self.replay("healthy-truck.jsonl")
        messages_before = TelematicsMessage.objects.count()
        readings_before = MeterReading.objects.count()

        output = self.replay("healthy-truck.jsonl")

        self.assertEqual(TelematicsMessage.objects.count(), messages_before)
        self.assertEqual(MeterReading.objects.count(), readings_before)
        self.assertIn("duplicate: 3", output)

    def test_out_of_order_reading_enters_history_without_replacing_current(self) -> None:
        self.replay("out-of-order.jsonl")

        readings = MeterReading.objects.filter(
            meter=self.odometer, quality=MeterReading.Quality.ACCEPTED
        ).order_by("observed_at")
        self.assertEqual([r.value for r in readings], [Decimal("121300.000"), Decimal("121500.000")])
        self.assertEqual(self.odometer.current_value, Decimal("121500.000"))
```

- [x] **Step 3: Run the tests to verify they fail**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: the three new tests FAIL only because the sample files do not exist yet (`Cannot read`). If you created the files in Step 1 already, they should PASS immediately — that is fine; the behaviour under test lives in `ingest_autopi`, not in this command. Confirm they pass and move on.

- [x] **Step 4: Run the full file**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: all 11 tests PASS.

- [x] **Step 5: Lint and type-check**

```bash
.venv/bin/ruff check backend/integrations && .venv/bin/ruff format backend/integrations
.venv/bin/mypy backend/integrations
```
Expected: clean.

---

### Task 4: `--dry-run` writes nothing

**Files:**
- Modify: `backend/integrations/management/commands/replay_telematics.py` (`handle`)
- Test: `backend/integrations/test_replay.py`

**Interfaces:**
- Consumes: `django.db.transaction.atomic`.
- Produces: `--dry-run` behaviour — full replay output, then rollback, with the footer line `DRY RUN — nothing written`.

- [x] **Step 1: Write the failing test**

Add to the imports in `test_replay.py`:

```python
from core.models import AuditEvent, OutboxEvent
```

Append to `ReplayTelematicsTests`:

```python
    # --- Task 4: dry-run -------------------------------------------------------

    def test_dry_run_reports_outcomes_but_writes_nothing(self) -> None:
        before = self.row_counts()

        output = self.replay("healthy-truck.jsonl", dry_run=True)

        self.assertIn("healthy-003 accepted", output)
        self.assertIn("accepted: 3", output)
        self.assertIn("DRY RUN — nothing written", output)
        self.assertEqual(self.row_counts(), before)
        self.assertIsNone(self.odometer.current_value)
        self.device.refresh_from_db()
        self.assertEqual(self.device.message_count, 0)
        self.assertIsNone(self.device.last_seen_at)

    def row_counts(self) -> dict[str, int]:
        return {
            "messages": TelematicsMessage.objects.count(),
            "events": NormalizedTelematicsEvent.objects.count(),
            "readings": MeterReading.objects.count(),
            "outbox": OutboxEvent.objects.count(),
            "audit": AuditEvent.objects.count(),
        }
```

- [x] **Step 2: Run the test to verify it fails**

Run: `../.venv/bin/python manage.py test integrations.test_replay.ReplayTelematicsTests.test_dry_run_reports_outcomes_but_writes_nothing -v 2`
Expected: FAIL — `dry_run` is accepted but ignored, so rows are written and the footer is missing.

- [x] **Step 3: Implement the rollback**

In `replay_telematics.py` add to the imports:

```python
from django.db import transaction
```

Add above `class Command`:

```python
class DryRunRollback(Exception):
    """Raised inside the atomic block so a --dry-run replay is rolled back."""
```

Restructure `handle` so the loop lives in a nested function and dry-run wraps it:

```python
    def handle(self, *args: object, **options: Any) -> None:
        device = resolve_device(
            external_id=options["device"], organization_slug=options["organization"]
        )
        messages = load_messages(options["file"])
        self.stdout.write(f"Replaying {len(messages)} messages as device {device.external_id}")

        outcomes: Counter[str] = Counter()
        qualities: Counter[str] = Counter()

        def replay_all() -> None:
            for index, raw in enumerate(messages, start=1):
                result = ingest_autopi(
                    device=device, payload=substitute_identity(raw, device=device)
                )
                label = outcome_label(result)
                outcomes[label] += 1
                self.stdout.write(f"{index:>4}  {message_label(raw, result)} {label}")
                for event in result.events:
                    qualities[f"{event.kind}:{event.quality}"] += 1
                    reason = f" — {event.reason}" if event.reason else ""
                    self.stdout.write(
                        f"        {event.kind} {event.signal} -> {event.quality}{reason}"
                    )

        if options["dry_run"]:
            try:
                with transaction.atomic():
                    replay_all()
                    raise DryRunRollback
            except DryRunRollback:
                pass
        else:
            replay_all()

        self.stdout.write("")
        self.stdout.write("Summary")
        for label, count in sorted(outcomes.items()):
            self.stdout.write(f"  {label}: {count}")
        for label, count in sorted(qualities.items()):
            self.stdout.write(f"  events {label}: {count}")
        if options["dry_run"]:
            self.stdout.write("DRY RUN — nothing written")
```

- [x] **Step 4: Run the full file to verify everything passes**

Run: `../.venv/bin/python manage.py test integrations.test_replay -v 2`
Expected: all 12 tests PASS. In particular the Task 2 and 3 tests still pass — the non-dry-run path is unchanged.

- [x] **Step 5: Lint and type-check**

```bash
.venv/bin/ruff check backend/integrations && .venv/bin/ruff format backend/integrations
.venv/bin/mypy backend/integrations
```
Expected: clean. ruff `B` may flag the bare `raise DryRunRollback` inside `with` — if so, change it to `raise DryRunRollback()`.

---

### Task 5: Identity check stays strict; README row; full-suite pass

**Files:**
- Test: `backend/integrations/test_replay.py`
- Modify: `README.md` (Common commands table, after the `make sbom` row)

**Interfaces:**
- Consumes: everything above. No new code in the command.

- [x] **Step 1: Write the failing identity test**

Append to `ReplayTelematicsTests`:

```python
    # --- Task 5: identity ------------------------------------------------------

    def test_real_ids_from_another_organization_are_rejected_not_rewritten(self) -> None:
        foreign = Path(self._testMethodName + ".jsonl")
        foreign.write_text(
            '{"schemaVersion":"1.0","messageId":"foreign-001",'
            '"organizationId":"00000000-0000-0000-0000-000000000099",'
            '"deviceId":"autopi-107",'
            '"observedAt":"2026-08-24T10:00:00+00:00","source":"autopi","type":"telemetry",'
            '"values":{"odometer":{"value":121600,"unit":"mi"}}}\n',
            encoding="utf-8",
        )
        self.addCleanup(foreign.unlink)

        output = self.replay(foreign)

        self.assertIn("foreign-001 rejected (identity_mismatch)", output)
        self.assertEqual(
            TelematicsMessage.objects.filter(status=TelematicsMessage.Status.ACCEPTED).count(), 0
        )
        self.assertEqual(MeterReading.objects.count(), 0)
```

- [x] **Step 2: Run the test to verify it passes**

Run: `../.venv/bin/python manage.py test integrations.test_replay.ReplayTelematicsTests.test_real_ids_from_another_organization_are_rejected_not_rewritten -v 2`
Expected: PASS on the first run. This test exists to guard a property, not to drive new code — if it fails, `substitute_identity` is rewriting something it must not; fix the function, never the test.

- [x] **Step 3: Add the README row**

In `README.md`, in the `## Common commands` table, add this row directly after the `make sbom` row:

```markdown
| `backend/manage.py replay_telematics <file> --device <id> [--dry-run]` | Replay saved AutoPi messages through the real ingest path; samples live in `backend/integrations/samples/`. Sample files use `{{organizationId}}`/`{{deviceId}}` placeholders that are filled from `--device`; real recorded files are replayed untouched |
```

- [x] **Step 4: Run the whole integrations app and the lint/type gates**

Run from `backend/`:
```bash
../.venv/bin/python manage.py test integrations -v 1
```
Expected: every test in `integrations.tests` and `integrations.test_replay` PASSES (13 new + the existing suite).

Run from repo root:
```bash
.venv/bin/ruff check backend && .venv/bin/ruff format --check backend
.venv/bin/mypy backend
```
Expected: clean.

- [x] **Step 5: Manual smoke run against the demo seed**

With the same scratch Postgres still running, from `backend/`:

```bash
../.venv/bin/python manage.py migrate --noinput
../.venv/bin/python manage.py seed_demo
../.venv/bin/python manage.py shell -c "from integrations.models import Device; print(list(Device.objects.values_list('organization__slug','external_id')))"
```

Pick one printed `(slug, external_id)` pair and run:

```bash
../.venv/bin/python manage.py replay_telematics integrations/samples/healthy-truck.jsonl --device <external_id> --organization <slug> --dry-run
```

Expected: three per-message lines, a summary, and `DRY RUN — nothing written`. If the seed has no device, create one via the admin API in `integrations.tests.test_device_management_is_integration_admin_only_and_token_is_hashed` as a model — or skip this step; the automated tests are the gate.

- [x] **Step 6: Stop the scratch Postgres**

```bash
fleetline_stop_postgres "$PGDIR" && rm -rf "$PGDIR"
```

---

## Self-review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| JSONL or JSON array, blank/`#` lines ignored | 1 |
| `--device` required, `--organization` disambiguates, ambiguous is an error | 1 |
| Placeholder substitution, real ids untouched | 1 (impl), 5 (guard test) |
| Per-message outcome lines + summary | 2 |
| Rejection is a result, not a failure; non-JSON is a failure | 1, 2 |
| Identity check preserved | 5 |
| `--dry-run` zero rows incl. outbox and device counters | 4 |
| Samples: healthy, overheating, malformed, duplicate, out-of-order | 1, 2, 3 |
| Tests 1–7 in spec | 2, 2, 3, 3, 4, 1, 5 |
| README paragraph | 5 |
| ruff + mypy clean | every task |

**Placeholder scan:** none. Every code step has full code.

**Type consistency:** `outcome_label`, `message_label`, `load_messages`, `substitute_identity`, `resolve_device`, `DryRunRollback` are named identically in Tasks 1, 2, 4. `replay()` helper signature `(sample: str | Path, **options: object) -> str` is used the same way in every task. `row_counts()` is defined in Task 4 where it is first used.
