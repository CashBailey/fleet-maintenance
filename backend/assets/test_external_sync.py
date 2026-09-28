from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from core.models import ApiToken, AuditEvent, Location, Organization, OutboxEvent, Role, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Asset, AssetStatusEvent, AssetType, Meter, MeterReading


class ExternalAssetSyncTests(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Fleet One", slug="external-fleet-one")
        self.other_org = Organization.objects.create(name="Fleet Two", slug="external-fleet-two")
        self.asset_type = AssetType.objects.create(organization=self.org, name="Truck")
        self.other_asset_type = AssetType.objects.create(organization=self.other_org, name="Truck")
        self.location = Location.objects.create(organization=self.org, name="Main", code="MAIN")
        integration_role = Role.objects.create(
            organization=self.org, slug="integration_admin", name="Integration admin"
        )
        manager_role = Role.objects.create(
            organization=self.org, slug="fleet_manager", name="Fleet manager"
        )
        other_integration_role = Role.objects.create(
            organization=self.other_org,
            slug="integration_admin",
            name="Integration admin",
        )
        self.integration_user = User.objects.create(username="gatorhub-sync", organization=self.org)
        self.integration_user.roles.add(integration_role)
        self.manager = User.objects.create(username="fleet-manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.other_integration_user = User.objects.create(
            username="other-gatorhub-sync", organization=self.other_org
        )
        self.other_integration_user.roles.add(other_integration_role)

    def token_client(self, user: User | None = None, scopes: list[str] | None = None) -> APIClient:
        user = user or self.integration_user
        organization = user.organization
        assert organization is not None
        raw = f"flt_test_{uuid.uuid4().hex}"
        ApiToken.objects.create(
            organization=organization,
            user=user,
            name="GatorHub sync",
            prefix=raw[:12],
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            scopes=scopes or ["assets.view", "assets.sync"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    def upsert(
        self,
        client: APIClient,
        payload: dict[str, Any],
        *,
        source: str = "gatorhub",
        external_id: str = "vehicle-42",
        key: str | None = None,
    ) -> Any:
        return client.put(
            reverse("asset-external", args=[source, external_id]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_upsert_updates_source_fields_and_preserves_fleetline_fields(self) -> None:
        external = self.token_client()
        created = self.upsert(
            external,
            {
                "unit_number": " truck-42 ",
                "asset_type_id": str(self.asset_type.pk),
                "vin": "vin42",
                "year": 2024,
                "make": "Kenworth",
                "model": "T680",
                "source_details": {
                    "license_plate": "TX-42",
                    "vehicle_type": "tractor",
                    "capacity_bbl": 130,
                },
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.data["created"])
        asset = Asset.objects.get(source_system="gatorhub", external_id="vehicle-42")
        self.assertEqual(asset.unit_number, "TRUCK-42")
        self.assertEqual(asset.specs["integrations"]["gatorhub"]["license_plate"], "TX-42")
        self.assertEqual(AssetStatusEvent.objects.filter(asset=asset).count(), 1)
        self.assertEqual(AssetStatusEvent.objects.get(asset=asset).source, "gatorhub")
        self.assertEqual(created.data["deep_link"], f"/assets/{asset.pk}")
        self.assertEqual(created.data["schedule_link"], f"/schedule?asset_id={asset.pk}")
        external_audit = AuditEvent.objects.get(
            action="asset.external_upserted", resource_id=str(asset.pk)
        )
        external_event = OutboxEvent.objects.get(
            event_type="asset.external_upserted", resource_id=str(asset.pk)
        )
        self.assertEqual(external_audit.source, "gatorhub")
        self.assertEqual(external_audit.context["external_id"], "vehicle-42")
        self.assertEqual(
            external_event.payload,
            {
                "source_system": "gatorhub",
                "external_id": "vehicle-42",
                "created": True,
                "fields": [
                    "asset_type",
                    "make",
                    "model",
                    "source_details",
                    "unit_number",
                    "vin",
                    "year",
                ],
            },
        )
        canonical_replay = self.upsert(
            external,
            {
                "unit_number": " Truck-42 ",
                "asset_type_id": str(self.asset_type.pk),
                "vin": "Vin42",
                "year": 2024,
                "make": "Kenworth",
                "model": "T680",
                "source_details": {
                    "license_plate": "TX-42",
                    "vehicle_type": "tractor",
                    "capacity_bbl": 130,
                },
            },
        )
        self.assertEqual(canonical_replay.status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="asset.external_upserted", resource_id=str(asset.pk)
            ).count(),
            1,
        )
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="asset.external_upserted", resource_id=str(asset.pk)
            ).count(),
            1,
        )

        manager = APIClient()
        manager.force_authenticate(self.manager)
        configured = manager.patch(
            reverse("asset-detail", args=[asset.pk]),
            {
                "home_location_id": str(self.location.pk),
                "specs": {
                    "shop_note": "local",
                    "integrations": {"other": {"source_note": "keep"}},
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(configured.status_code, 200)
        out_of_service = manager.post(
            reverse("asset-availability", args=[asset.pk]),
            {"status": "OutOfService", "reason": "Fleetline inspection hold"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(out_of_service.status_code, 200)

        update_key = str(uuid.uuid4())
        updated = self.upsert(
            external,
            {
                "unit_number": "TRUCK-42A",
                "make": "Peterbilt",
                "model": "579",
                "source_details": {"license_plate": "TX-42A", "capacity_bbl": 140},
            },
            key=update_key,
        )
        replay = self.upsert(
            external,
            {
                "unit_number": "TRUCK-42A",
                "make": "Peterbilt",
                "model": "579",
                "source_details": {"license_plate": "TX-42A", "capacity_bbl": 140},
            },
            key=update_key,
        )
        self.assertEqual((updated.status_code, replay.status_code), (200, 200))
        self.assertFalse(updated.data["created"])
        asset.refresh_from_db()
        self.assertEqual(Asset.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(asset.unit_number, "TRUCK-42A")
        self.assertEqual(asset.make, "Peterbilt")
        self.assertEqual(asset.status, "OutOfService")
        self.assertEqual(asset.home_location, self.location)
        self.assertEqual(asset.specs["shop_note"], "local")
        self.assertEqual(asset.specs["integrations"]["other"], {"source_note": "keep"})
        self.assertEqual(
            asset.specs["integrations"]["gatorhub"],
            {
                "vehicle_type": "tractor",
                "license_plate": "TX-42A",
                "capacity_bbl": 140,
            },
        )
        self.assertEqual(AssetStatusEvent.objects.filter(asset=asset).count(), 2)

        rejected_status = self.upsert(external, {"status": "Available"})
        self.assertEqual(rejected_status.status_code, 400)
        self.assertEqual(rejected_status.data["error"]["code"], "unsupported_fields")
        asset.refresh_from_db()
        self.assertEqual(asset.status, "OutOfService")

    def test_ordinary_patch_cannot_change_externally_owned_master_fields(self) -> None:
        asset = Asset.objects.create(
            organization=self.org,
            asset_type=self.asset_type,
            unit_number="T-10",
            source_system="gatorhub",
            external_id="vehicle-10",
            specs={"integrations": {"gatorhub": {"license_plate": "TX-10"}}},
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        rejected = client.patch(
            reverse("asset-detail", args=[asset.pk]),
            {"vin": "NEWVIN", "make": "Changed"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.data["error"]["code"], "external_asset_field_owned")
        asset.refresh_from_db()
        self.assertEqual((asset.vin, asset.make), ("", ""))

        local_update = client.patch(
            reverse("asset-detail", args=[asset.pk]),
            {"home_location_id": str(self.location.pk), "specs": {"local": True}},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(local_update.status_code, 200)
        asset.refresh_from_db()
        self.assertEqual(asset.home_location, self.location)
        self.assertEqual(
            asset.specs,
            {
                "local": True,
                "integrations": {"gatorhub": {"license_plate": "TX-10"}},
            },
        )

        source_detail_update = client.patch(
            reverse("asset-detail", args=[asset.pk]),
            {"specs": {"integrations": {"gatorhub": {"license_plate": "CHANGED"}}}},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(source_detail_update.status_code, 409)
        self.assertEqual(source_detail_update.data["error"]["code"], "external_asset_field_owned")

    def test_nullable_source_fields_can_be_cleared_without_changing_status(self) -> None:
        external = self.token_client()
        created = self.upsert(
            external,
            {
                "unit_number": "T-INITIAL-OOS",
                "asset_type_id": str(self.asset_type.pk),
                "vin": None,
                "serial_number": None,
                "make": None,
                "model": None,
            },
            external_id="initial-status-truck",
        )
        self.assertEqual(created.status_code, 201)
        asset = Asset.objects.get(external_id="initial-status-truck")
        self.assertEqual(asset.status, "Available")
        self.assertEqual(
            (asset.vin, asset.serial_number, asset.make, asset.model),
            ("", "", "", ""),
        )
        self.assertEqual(AssetStatusEvent.objects.filter(asset=asset).count(), 1)

        set_values = self.upsert(
            external,
            {
                "vin": "1M1AN07Y7BM001234",
                "serial_number": "SERIAL-1",
                "make": "Mack",
                "model": "Pinnacle",
            },
            external_id="initial-status-truck",
        )
        self.assertEqual(set_values.status_code, 200)
        cleared = self.upsert(
            external,
            {
                "vin": None,
                "serial_number": None,
                "make": None,
                "model": None,
            },
            external_id="initial-status-truck",
        )
        self.assertEqual(cleared.status_code, 200)
        asset.refresh_from_db()
        self.assertEqual(asset.status, "Available")
        self.assertEqual(
            (asset.vin, asset.serial_number, asset.make, asset.model),
            ("", "", "", ""),
        )
        self.assertEqual(AssetStatusEvent.objects.filter(asset=asset).count(), 1)

        status_attempt = self.upsert(
            external,
            {"initial_status": "OutOfService"},
            external_id="initial-status-truck",
        )
        self.assertEqual(status_attempt.status_code, 400)
        self.assertEqual(status_attempt.data["error"]["code"], "unsupported_fields")
        ownership_attempt = self.upsert(
            external,
            {"ownership": "Owned"},
            external_id="initial-status-truck",
        )
        self.assertEqual(ownership_attempt.status_code, 400)
        self.assertEqual(ownership_attempt.data["error"]["code"], "unsupported_fields")

    def test_external_writes_require_a_properly_scoped_api_token(self) -> None:
        url_payload = {
            "unit_number": "T-20",
            "asset_type_id": str(self.asset_type.pk),
        }
        browser = APIClient()
        browser.force_authenticate(self.integration_user)
        browser_response = self.upsert(browser, url_payload)
        self.assertEqual(browser_response.status_code, 403)
        self.assertEqual(browser_response.data["error"]["code"], "api_token_required")

        view_only = self.token_client(scopes=["assets.view"])
        scope_response = self.upsert(view_only, url_payload)
        self.assertEqual(scope_response.status_code, 403)
        self.assertEqual(scope_response.data["error"]["code"], "permission_denied")
        self.assertFalse(Asset.objects.filter(unit_number="T-20").exists())

    def test_external_identity_is_unique_inside_but_reusable_across_organizations(self) -> None:
        first = self.upsert(
            self.token_client(),
            {"unit_number": "ORG1", "asset_type_id": str(self.asset_type.pk)},
            external_id="shared-vehicle",
        )
        second = self.upsert(
            self.token_client(self.other_integration_user),
            {
                "unit_number": "ORG2",
                "asset_type_id": str(self.other_asset_type.pk),
            },
            external_id="shared-vehicle",
        )
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(
            Asset.objects.filter(source_system="gatorhub", external_id="shared-vehicle").count(),
            2,
        )

        first_lookup = self.token_client().get(
            reverse("asset-external", args=["gatorhub", "shared-vehicle"])
        )
        second_lookup = self.token_client(self.other_integration_user).get(
            reverse("asset-external", args=["gatorhub", "shared-vehicle"])
        )
        self.assertEqual(first_lookup.data["asset"]["unit_number"], "ORG1")
        self.assertEqual(second_lookup.data["asset"]["unit_number"], "ORG2")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Asset.objects.create(
                organization=self.org,
                asset_type=self.asset_type,
                unit_number="DUPLICATE",
                source_system="gatorhub",
                external_id="shared-vehicle",
            )

    def test_external_identity_pair_and_format_are_validated(self) -> None:
        missing_identifier = Asset(
            organization=self.org,
            asset_type=self.asset_type,
            unit_number="INVALID",
            source_system="gatorhub",
        )
        with self.assertRaises(ValidationError):
            missing_identifier.full_clean()
        invalid = self.upsert(
            self.token_client(),
            {"unit_number": "INVALID", "asset_type_id": str(self.asset_type.pk)},
            external_id="bad id",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.data["error"]["code"], "invalid_external_identity")
        invalid_source = self.upsert(
            self.token_client(),
            {"unit_number": "INVALID-SOURCE", "asset_type_id": str(self.asset_type.pk)},
            source="a" * 41,
            external_id="valid-id",
        )
        self.assertEqual(invalid_source.status_code, 400)
        self.assertEqual(invalid_source.data["error"]["code"], "invalid_external_identity")
        invalid_details = self.upsert(
            self.token_client(),
            {
                "unit_number": "INVALID-DETAILS",
                "asset_type_id": str(self.asset_type.pk),
                "source_details": {"bad key": "not allowed"},
            },
            external_id="invalid-details",
        )
        self.assertEqual(invalid_details.status_code, 400)
        self.assertEqual(invalid_details.data["error"]["code"], "invalid_source_details")

    def test_external_meter_handoff_preserves_provenance_and_deduplicates(self) -> None:
        external = self.token_client()
        created = self.upsert(
            external,
            {"unit_number": "METER-1", "asset_type_id": str(self.asset_type.pk)},
            external_id="meter-truck",
        )
        self.assertEqual(created.status_code, 201)
        meter_url = reverse("asset-external-meters", args=["gatorhub", "meter-truck"])
        reading_payload = {
            "kind": "odometer",
            "name": "Odometer",
            "unit": "mi",
            "value": "1200.5",
            "observed_at": timezone.now().isoformat(),
            "external_id": "dvir-reading-1",
        }
        first = external.post(
            meter_url,
            reading_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        replay = external.post(
            meter_url,
            reading_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual((first.status_code, replay.status_code), (201, 201))
        self.assertEqual(MeterReading.objects.count(), 1)
        reading = MeterReading.objects.get()
        self.assertEqual(reading.source, "gatorhub")
        self.assertEqual(reading.external_id, "dvir-reading-1")
        self.assertEqual(
            reading.provenance,
            {"entered_via": "external_asset_api", "source_system": "gatorhub"},
        )
        changed_reason = external.post(
            meter_url,
            {**reading_payload, "reason": "changed replay metadata"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(changed_reason.status_code, 409)
        self.assertEqual(changed_reason.data["error"]["code"], "duplicate_meter_reading_conflict")
        self.assertEqual(MeterReading.objects.count(), 1)
        reading.meter.active = False
        reading.meter.save(update_fields=["active", "updated_at"])
        inactive_replay = external.post(
            meter_url,
            {**reading_payload, "meter_id": str(reading.meter_id)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(inactive_replay.status_code, 201)
        self.assertEqual(MeterReading.objects.count(), 1)
        reading.meter.active = True
        reading.meter.save(update_fields=["active", "updated_at"])
        different_meter_replay = external.post(
            meter_url,
            {**reading_payload, "name": "Secondary odometer"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(different_meter_replay.status_code, 409)
        self.assertEqual(different_meter_replay.data["error"]["code"], "meter_definition_conflict")
        self.assertEqual(MeterReading.objects.count(), 1)
        conflicting_meter = external.post(
            meter_url,
            {**reading_payload, "unit": "km", "external_id": "dvir-reading-2"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(conflicting_meter.status_code, 409)
        self.assertEqual(conflicting_meter.data["error"]["code"], "meter_definition_conflict")

        browser = APIClient()
        browser.force_authenticate(self.integration_user)
        browser_attempt = browser.post(
            meter_url,
            {**reading_payload, "external_id": "spoofed-browser-reading"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(browser_attempt.status_code, 403)
        self.assertEqual(browser_attempt.data["error"]["code"], "api_token_required")

        manager = APIClient()
        manager.force_authenticate(self.manager)
        manual_attempt = manager.post(
            reverse("meter-readings", args=[reading.meter.asset_id]),
            {
                "meter_id": str(reading.meter_id),
                "value": "1201",
                "observed_at": timezone.now().isoformat(),
                "source": "gatorhub",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(manual_attempt.status_code, 400)
        self.assertEqual(manual_attempt.data["error"]["code"], "meter_source_not_allowed")
        self.assertEqual(MeterReading.objects.count(), 1)

    def test_external_reading_id_cannot_move_between_assets(self) -> None:
        external = self.token_client()
        for external_id, unit_number in (("meter-a", "METER-A"), ("meter-b", "METER-B")):
            response = self.upsert(
                external,
                {"unit_number": unit_number, "asset_type_id": str(self.asset_type.pk)},
                external_id=external_id,
            )
            self.assertEqual(response.status_code, 201)

        payload = {
            "kind": "odometer",
            "name": "Odometer",
            "unit": "mi",
            "value": "100",
            "observed_at": (timezone.now() - timedelta(hours=1)).isoformat(),
            "external_id": "shared-source-event",
        }
        first = external.post(
            reverse("asset-external-meters", args=["gatorhub", "meter-a"]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        conflict = external.post(
            reverse("asset-external-meters", args=["gatorhub", "meter-b"]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["error"]["code"], "duplicate_meter_reading_conflict")
        self.assertEqual(MeterReading.objects.filter(external_id="shared-source-event").count(), 1)
        second_asset = Asset.objects.get(external_id="meter-b")
        self.assertFalse(Meter.objects.filter(asset=second_asset).exists())

    @override_settings(
        METER_MAX_MILES_PER_HOUR="100.0",
        METER_MAX_ENGINE_HOURS_PER_HOUR="1.25",
        METER_FUTURE_TOLERANCE_SECONDS=60,
    )
    def test_external_future_and_fast_readings_remain_suspect(self) -> None:
        external = self.token_client()
        created = self.upsert(
            external,
            {"unit_number": "METER-VALIDATION", "asset_type_id": str(self.asset_type.pk)},
            external_id="meter-validation",
        )
        self.assertEqual(created.status_code, 201)
        meter_url = reverse("asset-external-meters", args=["gatorhub", "meter-validation"])
        now = timezone.now()

        def post_reading(value: str, observed_at: Any, event_id: str) -> Any:
            return external.post(
                meter_url,
                {
                    "kind": "odometer",
                    "name": "Odometer",
                    "unit": "mi",
                    "value": value,
                    "observed_at": observed_at.isoformat(),
                    "external_id": event_id,
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )

        baseline = post_reading("100", now - timedelta(hours=2), "baseline")
        fast = post_reading("500", now, "too-fast")
        fast_replay = post_reading("500", now, "too-fast")
        future_at = now + timedelta(minutes=10)
        future = post_reading("101", future_at, "too-far-future")
        future_replay = post_reading("101", future_at, "too-far-future")

        self.assertEqual(
            (baseline.status_code, fast.status_code, future.status_code), (201, 201, 201)
        )
        self.assertEqual(baseline.data["reading"]["quality"], "accepted")
        self.assertEqual(fast.data["reading"]["quality"], "suspect")
        self.assertEqual(fast.data["reading"]["provenance"]["validation"], "implausible_rate")
        self.assertEqual(fast_replay.data["reading"]["id"], fast.data["reading"]["id"])
        self.assertEqual(future.data["reading"]["quality"], "suspect")
        self.assertEqual(future.data["reading"]["provenance"]["validation"], "future_timestamp")
        self.assertEqual(future_replay.data["reading"]["id"], future.data["reading"]["id"])
        meter = Meter.objects.get(asset__external_id="meter-validation", kind="odometer")
        self.assertEqual(meter.current_value, Decimal("100.000"))

        def post_engine(value: str, observed_at: Any, event_id: str) -> Any:
            return external.post(
                meter_url,
                {
                    "kind": "engine_hours",
                    "name": "Engine hours",
                    "unit": "h",
                    "value": value,
                    "observed_at": observed_at.isoformat(),
                    "external_id": event_id,
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )

        self.assertEqual(
            post_engine("10", now - timedelta(hours=2), "engine-baseline").data["reading"][
                "quality"
            ],
            "accepted",
        )
        engine_fast = post_engine("13", now, "engine-too-fast")
        self.assertEqual(engine_fast.data["reading"]["quality"], "suspect")
        engine_meter = Meter.objects.get(asset__external_id="meter-validation", kind="engine_hours")
        self.assertEqual(engine_meter.current_value, Decimal("10.000"))
