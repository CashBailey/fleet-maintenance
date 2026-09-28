from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from assets.models import Asset, AssetType
from core.models import Organization, Role, User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from maintenance.models import MaintenanceAlert

from .models import Device, DeviceAssetAssociation, NormalizedTelematicsEvent
from .services import ingest_autopi

SAMPLES = Path(__file__).resolve().parent / "samples"


class DiagnosticAlertTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Gator Hauling", slug="gator")
        role = Role.objects.create(
            organization=self.organization, slug="integration_admin", name="Integration admin"
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
        token = "fdev_gator_autopi-107"  # noqa: S105
        self.device = Device.objects.create(
            organization=self.organization,
            name="AutoPi 107",
            provider="autopi",
            serial_number="AP-107",
            external_id="autopi-107",
            token_prefix=token[:12],
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
        )
        DeviceAssetAssociation.objects.create(
            organization=self.organization,
            device=self.device,
            asset=self.asset,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            assigned_by=self.admin,
        )

    def report(self, *, spn: int, fmi: int, message_id: str, minutes_ago: int = 60) -> None:
        observed = timezone.now() - timedelta(minutes=minutes_ago)
        result = ingest_autopi(
            device=self.device,
            payload={
                "schemaVersion": "1.0",
                "messageId": message_id,
                "organizationId": str(self.organization.pk),
                "deviceId": self.device.external_id,
                "observedAt": observed.isoformat(),
                "source": "autopi",
                "type": "diagnostics",
                "values": {},
                "diagnostics": [{"protocol": "j1939", "spn": spn, "fmi": fmi, "ecu": "engine"}],
            },
        )
        self.assertEqual(result.error_code, "", result.message.rejection_reason)

    def alerts(self) -> list[MaintenanceAlert]:
        return list(MaintenanceAlert.objects.order_by("first_seen_at"))

    def test_listed_code_raises_a_human_reviewed_alert(self) -> None:
        self.report(spn=110, fmi=0, message_id="dtc-1")

        [alert] = self.alerts()
        self.assertEqual(alert.status, "New")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.title, "Engine coolant temperature above normal")
        self.assertEqual(alert.asset_id, self.asset.pk)
        self.assertEqual(alert.dedupe_key, f"dtc:{self.asset.pk}:110:0")
        self.assertEqual(alert.source_type, "telematics")
        self.assertTrue(alert.rule_version.startswith("dtc-rules-"))
        self.assertIn("SPN 110", alert.description)
        self.assertIn("FMI 0", alert.description)

    def test_unlisted_code_is_recorded_as_evidence_without_an_alert(self) -> None:
        self.report(spn=9999, fmi=31, message_id="dtc-unknown")

        self.assertEqual(self.alerts(), [])
        event = NormalizedTelematicsEvent.objects.get(
            kind=NormalizedTelematicsEvent.Kind.DIAGNOSTIC
        )
        self.assertEqual(event.normalized_payload["spn"], 9999)

    def test_repeat_of_an_open_alert_counts_up_instead_of_duplicating(self) -> None:
        self.report(spn=110, fmi=0, message_id="dtc-1", minutes_ago=60)
        self.report(spn=110, fmi=0, message_id="dtc-2", minutes_ago=30)

        [alert] = self.alerts()
        self.assertEqual(alert.occurrence_count, 2)

    def test_recurrence_inside_cooldown_after_closure_stays_quiet(self) -> None:
        self.report(spn=110, fmi=0, message_id="dtc-1", minutes_ago=60)
        MaintenanceAlert.objects.update(status="Resolved")

        self.report(spn=110, fmi=0, message_id="dtc-2", minutes_ago=30)

        [alert] = self.alerts()
        self.assertEqual(alert.status, "Resolved")
        self.assertEqual(alert.occurrence_count, 1)

    def test_recurrence_after_cooldown_raises_a_fresh_alert(self) -> None:
        self.report(spn=110, fmi=0, message_id="dtc-1", minutes_ago=60)
        MaintenanceAlert.objects.update(
            status="Resolved", updated_at=timezone.now() - timedelta(hours=25)
        )

        self.report(spn=110, fmi=0, message_id="dtc-2", minutes_ago=30)

        resolved, fresh = self.alerts()
        self.assertEqual(resolved.status, "Resolved")
        self.assertEqual(fresh.status, "New")
        self.assertEqual(fresh.dedupe_key, resolved.dedupe_key)

    @override_settings(DTC_ALERT_COOLDOWN_HOURS=0)
    def test_cooldown_is_a_setting(self) -> None:
        self.report(spn=110, fmi=0, message_id="dtc-1", minutes_ago=60)
        MaintenanceAlert.objects.update(status="Dismissed")

        self.report(spn=110, fmi=0, message_id="dtc-2", minutes_ago=30)

        self.assertEqual(len(self.alerts()), 2)

    def test_replaying_the_overheating_sample_surfaces_an_alert(self) -> None:
        call_command(
            "replay_telematics",
            str(SAMPLES / "overheating.jsonl"),
            device="autopi-107",
            stdout=StringIO(),
        )

        [alert] = self.alerts()
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.asset_id, self.asset.pk)
