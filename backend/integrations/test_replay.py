from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

from assets.models import Asset, AssetType, Meter, MeterReading
from core.models import AuditEvent, Organization, OutboxEvent, Role, User
from django.core.management import CommandError, call_command
from django.test import TestCase

from .models import Device, DeviceAssetAssociation, NormalizedTelematicsEvent, TelematicsMessage

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
        token = f"fdev_{organization.slug}_{external_id}"
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
        call_command("replay_telematics", str(path), device="autopi-107", stdout=out, **options)
        return out.getvalue()

    # --- Task 1: loading, device resolution, errors -------------------------

    def test_unknown_device_is_an_error(self) -> None:
        with self.assertRaisesMessage(CommandError, "No device with external id 'nope'"):
            call_command("replay_telematics", str(SAMPLES / "healthy-truck.jsonl"), device="nope")

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
        self.assertEqual(
            [r.value for r in readings], [Decimal("121300.000"), Decimal("121500.000")]
        )
        self.assertEqual(self.odometer.current_value, Decimal("121500.000"))

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
