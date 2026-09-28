from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from assets.models import Asset, AssetType, Meter, MeterReading
from assets.services import correct_meter_reading, record_meter_reading
from core.models import AuditEvent, IdempotencyRecord, Organization, OutboxEvent, Role, User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Device, DeviceAssetAssociation, NormalizedTelematicsEvent, TelematicsMessage


class AutoPiIngestTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Gator Hauling", slug="gator")
        self.integration_role = Role.objects.create(
            organization=self.organization,
            slug="integration_admin",
            name="Integration administrator",
        )
        self.integration_admin = User.objects.create_user(
            username="integration",
            password="unused-test-password",  # noqa: S106
            organization=self.organization,
        )
        self.integration_admin.roles.add(self.integration_role)
        self.driver = User.objects.create_user(
            username="driver",
            password="unused-test-password",  # noqa: S106
            organization=self.organization,
        )
        driver_role = Role.objects.create(
            organization=self.organization, slug="driver", name="Driver"
        )
        self.driver.roles.add(driver_role)
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.organization, asset_type=asset_type, unit_number="TRUCK-12"
        )
        self.meter = Meter.objects.create(
            organization=self.organization,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        self.token = "fdev_test-token"  # noqa: S105
        self.device = Device.objects.create(
            organization=self.organization,
            name="AutoPi 12",
            provider="autopi",
            serial_number="AP-12",
            external_id="autopi-12",
            token_prefix=self.token[:12],
            token_hash=hashlib.sha256(self.token.encode()).hexdigest(),
        )
        self.start = timezone.now() - timedelta(days=2)
        DeviceAssetAssociation.objects.create(
            organization=self.organization,
            device=self.device,
            asset=self.asset,
            effective_from=self.start,
            assigned_by=self.integration_admin,
        )
        self.client: Any = APIClient()

    def payload(
        self,
        *,
        message_id: str,
        observed_at: datetime,
        value: object,
        device_id: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": "1.0",
            "messageId": message_id,
            "organizationId": str(self.organization.pk),
            "deviceId": device_id or self.device.external_id,
            "observedAt": observed_at.isoformat(),
            "sentAt": observed_at.isoformat(),
            "source": "autopi",
            "type": "telemetry",
            "values": {"odometer": {"value": value, "unit": "mi"}},
        }
        if sequence is not None:
            payload["sequence"] = sequence
        return payload

    def post(self, payload: dict[str, object], token: str | None = None) -> Any:
        return self.client.post(
            "/api/v1/integrations/telematics/autopi/v1/messages/",
            payload,
            format="json",
            HTTP_X_DEVICE_TOKEN=token or self.token,
        )

    def test_valid_reading_uses_real_association_and_meter_path(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        response = self.post(
            self.payload(message_id="msg-valid", observed_at=observed_at, value=100)
        )

        self.assertEqual(response.status_code, 202)
        reading = MeterReading.objects.get()
        event = NormalizedTelematicsEvent.objects.get()
        self.assertEqual(reading.value, Decimal("100"))
        self.assertEqual(reading.quality, MeterReading.Quality.ACCEPTED)
        self.assertEqual(event.meter_reading, reading)
        self.assertEqual(event.asset, self.asset)
        self.assertEqual(TelematicsMessage.objects.get().raw_payload["messageId"], "msg-valid")

    @override_settings(TELEMATICS_MAX_BODY_BYTES=700)
    def test_oversized_body_is_rejected_before_json_or_database_amplification(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        oversized = self.payload(message_id="oversized", observed_at=observed_at, value=100)
        values = oversized["values"]
        assert isinstance(values, dict)
        values["ignoredVendorField"] = "x" * 1000

        response = self.post(oversized)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "telematics_payload_too_large")
        self.assertFalse(TelematicsMessage.objects.exists())
        self.assertFalse(NormalizedTelematicsEvent.objects.exists())
        self.assertFalse(OutboxEvent.objects.exists())

        accepted = self.post(
            self.payload(message_id="after-oversized", observed_at=observed_at, value=100)
        )
        self.assertEqual(accepted.status_code, 202)

    @override_settings(TELEMATICS_MAX_DIAGNOSTICS=1)
    def test_diagnostic_cardinality_is_bounded_before_event_fanout(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        payload = self.payload(message_id="diagnostic-limit", observed_at=observed_at, value=0)
        payload.update(
            {
                "type": "diagnostics",
                "values": {},
                "diagnostics": [{"spn": 1}, {"spn": 2}],
            }
        )

        rejected = self.post(payload)

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"]["code"], "too_many_diagnostics")
        self.assertEqual(
            TelematicsMessage.objects.get().status,
            TelematicsMessage.Status.REJECTED,
        )
        self.assertFalse(NormalizedTelematicsEvent.objects.exists())
        self.assertFalse(OutboxEvent.objects.exists())

        diagnostics = payload["diagnostics"]
        assert isinstance(diagnostics, list)
        payload["diagnostics"] = diagnostics[:1]
        accepted = self.post(payload)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(NormalizedTelematicsEvent.objects.count(), 1)
        self.assertEqual(OutboxEvent.objects.count(), 1)

    def test_replay_by_message_id_creates_no_duplicate_facts(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        payload = self.payload(message_id="msg-replay", observed_at=observed_at, value=100)

        self.assertEqual(self.post(payload).status_code, 202)
        replay = self.post(payload)

        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["message"]["duplicate"])
        self.assertEqual(TelematicsMessage.objects.count(), 1)
        self.assertEqual(NormalizedTelematicsEvent.objects.count(), 1)
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_semantic_fallback_dedupe_uses_normalized_meter_values(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        first = self.payload(message_id="", observed_at=observed_at, value=100, sequence=10)
        replay = self.payload(message_id="", observed_at=observed_at, value="100.0", sequence=10)
        replay_values = replay["values"]
        assert isinstance(replay_values, dict)
        replay_values["ignoredVendorField"] = {"representation": "changed"}

        self.assertEqual(self.post(first).status_code, 202)
        response = self.post(replay)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["message"]["duplicate"])
        self.assertEqual(TelematicsMessage.objects.count(), 1)
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_semantic_fallback_dedupe_normalizes_unit_aliases(self) -> None:
        Meter.objects.create(
            organization=self.organization,
            asset=self.asset,
            name="Engine hours",
            kind=Meter.Kind.ENGINE_HOURS,
            unit="h",
        )
        observed_at = timezone.now() - timedelta(minutes=20)
        first = self.payload(message_id="", observed_at=observed_at, value=0, sequence=11)
        replay = self.payload(message_id="", observed_at=observed_at, value=0, sequence=11)
        first["values"] = {"engineHours": {"value": 100, "unit": "h"}}
        replay["values"] = {"engineHours": {"value": "100.0", "unit": "hours"}}

        self.assertEqual(self.post(first).status_code, 202)
        response = self.post(replay)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["message"]["duplicate"])
        self.assertEqual(TelematicsMessage.objects.count(), 1)
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_wrong_org_rejection_cannot_poison_corrected_message(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        payload = self.payload(
            message_id="identity-poison", observed_at=observed_at, value=100, sequence=12
        )
        payload["organizationId"] = "00000000-0000-0000-0000-000000000099"

        rejected = self.post(payload)
        payload["organizationId"] = str(self.organization.pk)
        accepted = self.post(payload)

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 202)
        evidence = TelematicsMessage.objects.get(status=TelematicsMessage.Status.REJECTED)
        self.assertEqual(evidence.message_id, "")
        self.assertEqual(evidence.raw_payload["messageId"], "identity-poison")
        self.assertTrue(
            TelematicsMessage.objects.filter(
                status=TelematicsMessage.Status.ACCEPTED,
                message_id="identity-poison",
            ).exists()
        )
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_reused_sequence_with_different_content_is_rejected_with_evidence(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        first = self.payload(
            message_id="sequence-first", observed_at=observed_at, value=100, sequence=44
        )
        conflict = self.payload(
            message_id="sequence-conflict",
            observed_at=observed_at + timedelta(minutes=1),
            value=101,
            sequence=44,
        )

        self.assertEqual(self.post(first).status_code, 202)
        response = self.post(conflict)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "sequence_conflict")
        evidence = TelematicsMessage.objects.get(status=TelematicsMessage.Status.REJECTED)
        self.assertEqual(evidence.message_id, "")
        self.assertEqual(evidence.raw_payload["messageId"], "sequence-conflict")
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_message_id_conflict_preserves_rejected_raw_evidence(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        first = self.payload(message_id="source-id", observed_at=observed_at, value=100)
        conflict = self.payload(message_id="source-id", observed_at=observed_at, value=101)

        self.assertEqual(self.post(first).status_code, 202)
        response = self.post(conflict)

        self.assertEqual(response.status_code, 409)
        evidence = TelematicsMessage.objects.get(status=TelematicsMessage.Status.REJECTED)
        self.assertEqual(evidence.message_id, "")
        self.assertEqual(evidence.raw_payload["messageId"], "source-id")
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_late_reading_is_history_but_does_not_replace_current(self) -> None:
        newer = timezone.now() - timedelta(hours=1)
        older = newer - timedelta(hours=1)
        self.assertEqual(
            self.post(self.payload(message_id="newer", observed_at=newer, value=100)).status_code,
            202,
        )

        response = self.post(self.payload(message_id="older", observed_at=older, value=90))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(MeterReading.objects.filter(quality="accepted").count(), 2)
        self.assertEqual(self.meter.current_value, Decimal("100"))

    def test_decreasing_reading_is_suspect_and_current_is_unchanged(self) -> None:
        first = timezone.now() - timedelta(hours=2)
        second = first + timedelta(hours=1)
        self.post(self.payload(message_id="first", observed_at=first, value=100))

        response = self.post(self.payload(message_id="decrease", observed_at=second, value=90))

        self.assertEqual(response.status_code, 202)
        event = NormalizedTelematicsEvent.objects.get(message__message_id="decrease")
        self.assertEqual(event.quality, NormalizedTelematicsEvent.Quality.SUSPECT)
        self.assertIn("decreases", event.reason)
        self.assertEqual(self.meter.current_value, Decimal("100"))

    def test_late_reading_checks_implied_rate_to_following_reading(self) -> None:
        later = timezone.now() - timedelta(hours=1)
        earlier = later - timedelta(minutes=1)
        self.assertEqual(
            self.post(
                self.payload(message_id="later-fast", observed_at=later, value=1000)
            ).status_code,
            202,
        )

        response = self.post(
            self.payload(message_id="earlier-fast", observed_at=earlier, value=990)
        )

        self.assertEqual(response.status_code, 202)
        event = NormalizedTelematicsEvent.objects.get(message__message_id="earlier-fast")
        self.assertEqual(event.quality, NormalizedTelematicsEvent.Quality.SUSPECT)
        self.assertIn("later reading", event.reason)

    def test_plausibility_ignores_a_superseded_meter_reading(self) -> None:
        original_time = timezone.now() - timedelta(hours=2)
        original = record_meter_reading(
            meter=self.meter,
            value=1000,
            observed_at=original_time,
            source="manual",
            actor=self.integration_admin,
        )
        correct_meter_reading(
            reading=original,
            value=100,
            reason="Correct transposed reading",
            actor=self.integration_admin,
        )

        response = self.post(
            self.payload(
                message_id="after-correction",
                observed_at=original_time + timedelta(hours=1),
                value=110,
            )
        )

        self.assertEqual(response.status_code, 202)
        event = NormalizedTelematicsEvent.objects.get(message__message_id="after-correction")
        self.assertEqual(event.quality, NormalizedTelematicsEvent.Quality.ACCEPTED)

    def test_payload_identity_cannot_override_authenticated_device(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        payload = self.payload(
            message_id="wrong-identity",
            observed_at=observed_at,
            value=100,
            device_id="some-other-device",
        )

        response = self.post(payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "identity_mismatch")
        self.assertEqual(TelematicsMessage.objects.get().status, TelematicsMessage.Status.REJECTED)
        self.assertFalse(MeterReading.objects.exists())

    def test_reading_outside_association_is_quarantined(self) -> None:
        observed_at = self.start - timedelta(seconds=1)

        response = self.post(
            self.payload(message_id="before-assignment", observed_at=observed_at, value=100)
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "no_association")
        self.assertEqual(
            TelematicsMessage.objects.get().status, TelematicsMessage.Status.QUARANTINED
        )
        self.assertFalse(MeterReading.objects.exists())

    def test_quarantined_message_can_be_replayed_after_association_is_corrected(self) -> None:
        observed_at = self.start - timedelta(hours=1)
        payload = self.payload(
            message_id="association-corrected",
            observed_at=observed_at,
            value=100,
            sequence=62,
        )
        first = self.post(payload)
        DeviceAssetAssociation.objects.create(
            organization=self.organization,
            device=self.device,
            asset=self.asset,
            effective_from=observed_at - timedelta(hours=1),
            effective_to=self.start,
            assigned_by=self.integration_admin,
        )

        replay = self.post(payload)

        self.assertEqual(first.status_code, 422)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(
            TelematicsMessage.objects.filter(
                message_id="association-corrected", status=TelematicsMessage.Status.ACCEPTED
            ).count(),
            1,
        )
        self.assertEqual(
            TelematicsMessage.objects.filter(
                message_id="association-corrected", status=TelematicsMessage.Status.QUARANTINED
            ).count(),
            1,
        )
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_wrong_device_token_is_rejected_before_payload_identity(self) -> None:
        observed_at = timezone.now() - timedelta(minutes=20)
        response = self.post(
            self.payload(message_id="bad-token", observed_at=observed_at, value=100),
            token="wrong-token",  # noqa: S106
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(TelematicsMessage.objects.exists())

    def test_device_management_is_integration_admin_only_and_token_is_hashed(self) -> None:
        self.client.force_authenticate(self.driver)
        self.assertEqual(self.client.get("/api/v1/integrations/devices/").status_code, 403)

        self.client.force_authenticate(self.integration_admin)
        key = "00000000-0000-0000-0000-000000000612"
        payload = {"name": "Spare AutoPi", "serial_number": "AP-SPARE"}
        response = self.client.post(
            "/api/v1/integrations/devices/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

        self.assertEqual(response.status_code, 201)
        token = response.json()["device"]["token"]
        device = Device.objects.get(serial_number="AP-SPARE")
        self.assertNotEqual(device.token_hash, token)
        self.assertEqual(device.token_hash, hashlib.sha256(token.encode()).hexdigest())

        replay = self.client.post(
            "/api/v1/integrations/devices/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(replay.status_code, 201)
        self.assertNotIn("token", replay.json()["device"])
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertIn("cannot be recovered", replay.json()["message"])
        record = IdempotencyRecord.objects.get(key=key)
        self.assertNotIn(token, json.dumps(record.response_body))
        self.assertEqual(Device.objects.filter(serial_number="AP-SPARE").count(), 1)

    def test_device_association_replay_with_implicit_time_creates_one_transition(self) -> None:
        self.client.force_authenticate(self.integration_admin)
        key = "00000000-0000-0000-0000-000000000616"
        url = f"/api/v1/integrations/devices/{self.device.pk}/associate/"
        payload = {"asset_id": str(self.asset.pk)}

        first = self.client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
        replay = self.client.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY=key)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(DeviceAssetAssociation.objects.filter(device=self.device).count(), 2)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="device.associated", resource_id=first.json()["association"]["id"]
            ).count(),
            1,
        )

    def test_device_token_rotation_and_status_are_idempotent_and_immediate(self) -> None:
        rotate_key = "00000000-0000-0000-0000-000000000613"
        rotate_url = f"/api/v1/integrations/devices/{self.device.pk}/rotate-token/"
        initial_hash = self.device.token_hash
        self.client.force_authenticate(self.driver)
        denied = self.client.post(rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key)
        self.assertEqual(denied.status_code, 403)
        self.device.refresh_from_db()
        self.assertEqual(self.device.token_hash, initial_hash)

        self.client.force_authenticate(self.integration_admin)

        rotated = self.client.post(rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key)

        self.assertEqual(rotated.status_code, 200)
        new_token = rotated.json()["device"]["token"]
        self.assertTrue(rotated.json()["secret_recoverable"])
        self.device.refresh_from_db()
        rotated_hash = self.device.token_hash
        self.assertEqual(rotated_hash, hashlib.sha256(new_token.encode()).hexdigest())
        old_token_attempt = self.post(
            self.payload(
                message_id="old-token-after-rotation",
                observed_at=timezone.now() - timedelta(minutes=20),
                value=100,
            ),
            token=self.token,
        )
        self.assertEqual(old_token_attempt.status_code, 401)

        replay = self.client.post(rotate_url, {}, format="json", HTTP_IDEMPOTENCY_KEY=rotate_key)
        self.device.refresh_from_db()
        self.assertEqual(replay.status_code, 200)
        self.assertNotIn("token", replay.json()["device"])
        self.assertFalse(replay.json()["secret_recoverable"])
        self.assertEqual(self.device.token_hash, rotated_hash)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="device.token_rotated", resource_id=str(self.device.pk)
            ).count(),
            1,
        )
        record = IdempotencyRecord.objects.get(key=rotate_key)
        self.assertNotIn(new_token, json.dumps(record.response_body))

        disable_key = "00000000-0000-0000-0000-000000000614"
        status_url = f"/api/v1/integrations/devices/{self.device.pk}/status/"
        disabled = self.client.post(
            status_url,
            {"status": "disabled"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=disable_key,
        )
        self.assertEqual(disabled.status_code, 200)
        disabled_attempt = self.post(
            self.payload(
                message_id="disabled-device",
                observed_at=timezone.now() - timedelta(minutes=19),
                value=101,
            ),
            token=new_token,
        )
        self.assertEqual(disabled_attempt.status_code, 401)

        enable_key = "00000000-0000-0000-0000-000000000615"
        enabled = self.client.post(
            status_url,
            {"status": "active"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=enable_key,
        )
        self.assertEqual(enabled.status_code, 200)
        accepted = self.post(
            self.payload(
                message_id="reactivated-device",
                observed_at=timezone.now() - timedelta(minutes=18),
                value=102,
            ),
            token=new_token,
        )
        self.assertEqual(accepted.status_code, 202)
