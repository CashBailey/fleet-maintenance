from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.models import AuditEvent, Organization, Role, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Asset, AssetStatusEvent, AssetType, Meter, MeterReading
from .services import create_asset, create_meter, record_meter_reading


class AssetRetirementTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Retirement Fleet",
            slug="retirement-fleet",
        )
        manager_role = Role.objects.create(
            organization=self.organization,
            slug="fleet_manager",
            name="Fleet manager",
        )
        supervisor_role = Role.objects.create(
            organization=self.organization,
            slug="supervisor",
            name="Shop supervisor",
        )
        self.manager = User.objects.create_user(
            username="retirement-manager",
            organization=self.organization,
        )
        self.manager.roles.add(manager_role)
        self.supervisor = User.objects.create_user(
            username="retirement-supervisor",
            organization=self.organization,
        )
        self.supervisor.roles.add(supervisor_role)
        self.asset_type = AssetType.objects.create(
            organization=self.organization,
            name="Truck",
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.manager)

    def create_asset(self, unit_number: str = "RET-01") -> Asset:
        return create_asset(
            organization=self.organization,
            actor=self.manager,
            asset_type=self.asset_type,
            unit_number=unit_number,
        )

    def record_meter(
        self,
        asset: Asset,
        *,
        name: str,
        kind: str,
        unit: str,
        value: str,
        observed_at: Any | None = None,
    ) -> tuple[Meter, MeterReading]:
        meter = create_meter(
            organization=self.organization,
            asset=asset,
            actor=self.manager,
            name=name,
            kind=kind,
            unit=unit,
        )
        reading = record_meter_reading(
            meter=meter,
            value=value,
            observed_at=observed_at or timezone.now(),
            source="manual",
            actor=self.manager,
        )
        return meter, reading

    def retire(
        self,
        asset: Asset,
        *,
        reason: str = "Removed from active fleet",
        disposition: str = "Sold at public auction",
        reading_ids: list[object] | None = None,
        operation_id: str = "00000000-0000-0000-0000-000000000201",
    ) -> Any:
        return self.api_client.post(
            reverse("asset-availability", args=[asset.pk]),
            {
                "status": Asset.Status.RETIRED,
                "reason": reason,
                "disposition": disposition,
                "final_meter_reading_ids": [
                    str(getattr(value, "pk", value)) for value in reading_ids or []
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=operation_id,
        )

    def test_retirement_requires_separate_disposition(self) -> None:
        asset = self.create_asset()

        response = self.retire(asset, disposition="")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "retirement_disposition_required")
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.AVAILABLE)
        self.assertIsNone(asset.archived_at)
        self.assertFalse(
            AssetStatusEvent.objects.filter(asset=asset, new_status=Asset.Status.RETIRED).exists()
        )

    def test_retirement_requires_every_current_accepted_cumulative_reading(self) -> None:
        asset = self.create_asset()
        _, odometer = self.record_meter(
            asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
            value="120000",
        )
        engine_meter, engine_hours = self.record_meter(
            asset,
            name="Engine hours",
            kind=Meter.Kind.ENGINE_HOURS,
            unit="h",
            value="8500",
        )
        self.record_meter(
            asset,
            name="Fuel counter",
            kind=Meter.Kind.OTHER,
            unit="gal",
            value="30000",
        )

        response = self.retire(asset, reading_ids=[odometer])

        self.assertEqual(response.status_code, 400)
        problem = response.json()["error"]
        self.assertEqual(problem["code"], "final_meter_readings_required")
        self.assertEqual(problem["details"]["meters"][0]["meter_id"], str(engine_meter.pk))
        self.assertEqual(
            problem["details"]["meters"][0]["current_reading_id"],
            str(engine_hours.pk),
        )
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.AVAILABLE)

    def test_retirement_records_immutable_meter_snapshots_and_replays_once(self) -> None:
        asset = self.create_asset()
        _, odometer = self.record_meter(
            asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
            value="120000",
        )
        _, engine_hours = self.record_meter(
            asset,
            name="Engine hours",
            kind=Meter.Kind.ENGINE_HOURS,
            unit="h",
            value="8500",
        )
        operation_id = "00000000-0000-0000-0000-000000000202"

        first = self.retire(
            asset,
            reading_ids=[engine_hours, odometer],
            operation_id=operation_id,
        )
        replay = self.retire(
            asset,
            reading_ids=[engine_hours, odometer],
            operation_id=operation_id,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["asset"]["id"], first.json()["asset"]["id"])
        self.assertEqual(
            replay.json()["status_event"]["id"],
            first.json()["status_event"]["id"],
        )
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.RETIRED)
        self.assertIsNotNone(asset.archived_at)
        assert asset.archived_at is not None
        event = AssetStatusEvent.objects.get(asset=asset, new_status=Asset.Status.RETIRED)
        self.assertEqual(event.reason, "Removed from active fleet")
        self.assertEqual(event.context["retirement_disposition"], "Sold at public auction")
        self.assertEqual(event.context["retired_at"], asset.archived_at.isoformat())
        snapshots = event.context["final_meter_readings"]
        self.assertEqual(
            {snapshot["reading_id"] for snapshot in snapshots},
            {str(odometer.pk), str(engine_hours.pk)},
        )
        self.assertEqual({snapshot["quality"] for snapshot in snapshots}, {"accepted"})
        self.assertEqual({snapshot["source"] for snapshot in snapshots}, {"manual"})
        self.assertEqual(
            AssetStatusEvent.objects.filter(asset=asset, new_status=Asset.Status.RETIRED).count(),
            1,
        )
        audit_event = AuditEvent.objects.get(
            resource_type="Asset",
            resource_id=str(asset.pk),
            action="asset.availability_changed",
            new_state=Asset.Status.RETIRED,
        )
        self.assertEqual(audit_event.context["retirement_disposition"], "Sold at public auction")

        history = self.api_client.get(reverse("asset-history", args=[asset.pk]))
        self.assertEqual(history.status_code, 200)
        retired = next(
            row
            for row in history.json()["status_events"]
            if row["new_status"] == Asset.Status.RETIRED
        )
        self.assertEqual(retired["context"], event.context)
        retirement_timeline = next(
            row
            for row in history.json()["timeline"]
            if row["type"] == "asset_status" and row["status"] == Asset.Status.RETIRED
        )
        self.assertEqual(retirement_timeline["context"], event.context)

    def test_retirement_rejects_stale_or_wrong_asset_meter_evidence(self) -> None:
        asset = self.create_asset()
        meter, stale = self.record_meter(
            asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
            value="119900",
            observed_at=timezone.now() - timedelta(hours=2),
        )
        current = record_meter_reading(
            meter=meter,
            value="120000",
            observed_at=timezone.now() - timedelta(hours=1),
            source="manual",
            actor=self.manager,
        )
        other_asset = self.create_asset("RET-02")
        _, other_reading = self.record_meter(
            other_asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
            value="50000",
        )

        stale_response = self.retire(asset, reading_ids=[stale])
        self.assertEqual(stale_response.status_code, 400)
        self.assertEqual(
            stale_response.json()["error"]["code"],
            "invalid_final_meter_readings",
        )
        self.assertEqual(
            stale_response.json()["error"]["details"]["expected_reading_ids"],
            [str(current.pk)],
        )

        wrong_asset = self.retire(
            asset,
            reading_ids=[current, other_reading],
            operation_id="00000000-0000-0000-0000-000000000203",
        )
        self.assertEqual(wrong_asset.status_code, 400)
        self.assertEqual(
            wrong_asset.json()["error"]["code"],
            "invalid_final_meter_readings",
        )
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.AVAILABLE)

    def test_asset_without_cumulative_meter_can_be_retired(self) -> None:
        asset = self.create_asset()

        response = self.retire(asset)

        self.assertEqual(response.status_code, 200)
        event = AssetStatusEvent.objects.get(asset=asset, new_status=Asset.Status.RETIRED)
        self.assertEqual(event.context["final_meter_readings"], [])

    def test_assets_status_role_cannot_retire(self) -> None:
        asset = self.create_asset()
        self.api_client.force_authenticate(self.supervisor)

        response = self.retire(asset)

        self.assertEqual(response.status_code, 403)
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.AVAILABLE)
