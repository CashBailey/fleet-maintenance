from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.models import AuditEvent, Location, Organization, Role, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from maintenance.models import Defect, MaintenanceRequest, WorkOrder, WorkOrderTask
from rest_framework.test import APIClient

from .models import Asset, AssetStatusEvent, AssetType, Meter, MeterReading
from .services import create_asset, create_meter, record_meter_reading


class AssetApiTests(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Fleet One", slug="fleet-one")
        self.other_org = Organization.objects.create(name="Fleet Two", slug="fleet-two")
        self.location = Location.objects.create(organization=self.org, name="Main", code="MAIN")
        self.asset_type = AssetType.objects.create(
            organization=self.org, name="Truck", category="vehicle"
        )
        manager_role = Role.objects.create(
            organization=self.org, slug="fleet_manager", name="Fleet manager"
        )
        driver_role = Role.objects.create(organization=self.org, slug="driver", name="Driver")
        self.manager = User.objects.create(username="manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.driver = User.objects.create(username="driver", organization=self.org)
        self.driver.roles.add(driver_role)
        self.api = APIClient()

    def create_asset(self, unit: str = "T-01", **fields: Any) -> Asset:
        return create_asset(
            organization=self.org,
            actor=self.manager,
            asset_type=self.asset_type,
            home_location=self.location,
            unit_number=unit,
            **fields,
        )

    def test_unit_and_vin_are_normalized_and_unique_per_organization(self) -> None:
        self.create_asset(" truck-1 ", vin="abc123")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Asset.objects.create(
                organization=self.org,
                asset_type=self.asset_type,
                unit_number="TRUCK-1",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Asset.objects.create(
                organization=self.org,
                asset_type=self.asset_type,
                unit_number="TRUCK-2",
                vin="ABC123",
            )

    def test_driver_only_reads_assigned_assets_and_cannot_create(self) -> None:
        assigned = self.create_asset("T-01", assigned_driver=self.driver)
        self.create_asset("T-02")
        self.api.force_authenticate(self.driver)
        response: Any = self.api.get(reverse("assets"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["assets"]], [str(assigned.pk)])
        response = self.api.post(
            reverse("assets"),
            {"unit_number": "T-03", "asset_type_id": str(self.asset_type.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000011",
        )
        self.assertEqual(response.status_code, 403)

    def test_cross_organization_asset_is_not_visible(self) -> None:
        other_type = AssetType.objects.create(organization=self.other_org, name="Truck")
        other = Asset.objects.create(
            organization=self.other_org, asset_type=other_type, unit_number="OTHER"
        )
        self.api.force_authenticate(self.manager)
        self.assertEqual(self.api.get(reverse("asset-detail", args=[other.pk])).status_code, 404)

    def test_asset_master_and_equipment_specs_do_not_require_a_home_location(self) -> None:
        self.api.force_authenticate(self.manager)
        created: Any = self.api.post(
            reverse("assets"),
            {
                "unit_number": "TRUCK-EQUIPMENT-01",
                "asset_type_id": str(self.asset_type.pk),
                "vin": "1M8GDM9AXKP042788",
                "make": "Freightliner",
                "model": "M2 106",
                "specs": {
                    "equipment": {
                        "engine": {"type": "Diesel", "serial_number": "ENG-001"},
                    }
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000010",
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data["asset"]["home_location_id"], None)
        self.assertEqual(
            created.data["asset"]["specs"]["equipment"]["engine"],
            {"type": "Diesel", "serial_number": "ENG-001"},
        )
        asset = Asset.objects.get(pk=created.data["asset"]["id"])
        self.assertIsNone(asset.home_location_id)
        self.assertEqual(
            (asset.vin, asset.make, asset.model), ("1M8GDM9AXKP042788", "Freightliner", "M2 106")
        )

        updated: Any = self.api.patch(
            reverse("asset-detail", args=[asset.pk]),
            {
                "specs": {
                    "equipment": {
                        "engine": {"type": "Diesel", "serial_number": "ENG-001"},
                        "transmission": {"manufacturer": "Allison", "model": "3000"},
                        "axle": {"configuration": "6x4 tandem", "ratio": "3.55"},
                    }
                }
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000016",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            updated.data["asset"]["specs"]["equipment"]["transmission"],
            {"manufacturer": "Allison", "model": "3000"},
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                action="asset.updated",
                resource_id=str(asset.pk),
                context__fields=["specs"],
            ).exists()
        )

    def test_manual_reading_is_idempotent_and_correction_is_append_only(self) -> None:
        asset = self.create_asset()
        self.api.force_authenticate(self.manager)
        payload = {
            "kind": "odometer",
            "name": "Odometer",
            "unit": "mi",
            "value": "1000",
            "observed_at": timezone.now().isoformat(),
        }
        url = reverse("meter-readings", args=[asset.pk])
        first = self.api.post(
            url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000012",
        )
        second = self.api.post(
            url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000012",
        )
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(MeterReading.objects.count(), 1)
        original = MeterReading.objects.get()
        corrected = self.api.post(
            reverse("correct-meter", args=[original.pk]),
            {"value": "990", "reason": "Verified against dashboard"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000013",
        )
        self.assertEqual(corrected.status_code, 200)
        self.assertEqual(MeterReading.objects.count(), 2)
        self.assertEqual(
            original.meter.current_value, MeterReading.objects.get(corrects=original).value
        )
        original.value = 1
        with self.assertRaises(ValidationError):
            original.save()

    def test_late_reading_is_historical_not_current(self) -> None:
        asset = self.create_asset()
        meter = create_meter(
            organization=self.org,
            asset=asset,
            actor=self.manager,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        now = timezone.now()
        latest = record_meter_reading(
            meter=meter, value="200", observed_at=now, source="manual", actor=self.manager
        )
        record_meter_reading(
            meter=meter,
            value="150",
            observed_at=now - timedelta(days=1),
            source="autopi",
            actor=None,
            external_id="late-message",
        )
        current = meter.current_reading
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.pk, latest.pk)

    def test_decreasing_cumulative_reading_is_quarantined(self) -> None:
        asset = self.create_asset()
        meter = create_meter(
            organization=self.org,
            asset=asset,
            actor=self.manager,
            name="Engine hours",
            kind=Meter.Kind.ENGINE_HOURS,
            unit="h",
        )
        now = timezone.now()
        accepted = record_meter_reading(
            meter=meter, value="500", observed_at=now, source="manual", actor=self.manager
        )
        suspect = record_meter_reading(
            meter=meter,
            value="400",
            observed_at=now + timedelta(hours=1),
            source="manual",
            actor=self.manager,
        )
        self.assertEqual(suspect.quality, MeterReading.Quality.SUSPECT)
        current = meter.current_reading
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.pk, accepted.pk)

    def test_availability_transition_creates_immutable_history_and_audit(self) -> None:
        asset = self.create_asset()
        self.api.force_authenticate(self.manager)
        response = self.api.post(
            reverse("asset-availability", args=[asset.pk]),
            {"status": Asset.Status.OUT_OF_SERVICE, "reason": "Brake inspection required"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000014",
        )
        self.assertEqual(response.status_code, 200)
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.OUT_OF_SERVICE)
        event = AssetStatusEvent.objects.filter(asset=asset).first()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.reason, "Brake inspection required")
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.org,
                action="asset.availability_changed",
                resource_id=str(asset.pk),
            ).exists()
        )
        with self.assertRaises(ValidationError):
            event.delete()

    def test_history_timeline_traces_defect_through_verified_closure(self) -> None:
        asset = self.create_asset()
        defect = Defect.objects.create(
            organization=self.org,
            asset=asset,
            reported_by=self.driver,
            category="Brakes",
            description="Brake pedal travels too far",
            severity="safety",
            safety_related=True,
            status="InRepair",
        )
        maintenance_request = MaintenanceRequest.objects.create(
            organization=self.org,
            asset=asset,
            defect=defect,
            submitted_by=self.manager,
            status="Converted",
            priority="safety",
            summary="Inspect and repair service brakes",
        )
        completed_at = timezone.now() + timedelta(hours=1)
        work_order = WorkOrder.objects.create(
            organization=self.org,
            number="WO-TRACE-1",
            asset=asset,
            request=maintenance_request,
            created_by=self.manager,
            assigned_to=self.manager,
            status="Closed",
            priority="safety",
            summary="Repair service brakes",
            completion_summary="Brake adjustment completed and road tested",
            completed_at=completed_at,
            completed_by=self.manager,
            closed_at=completed_at + timedelta(minutes=30),
            closed_by=self.manager,
        )
        task = WorkOrderTask.objects.create(
            organization=self.org,
            work_order=work_order,
            title="Adjust brakes",
            status="Completed",
            completed_at=completed_at - timedelta(minutes=15),
            completed_by=self.manager,
        )

        self.api.force_authenticate(self.manager)
        response: Any = self.api.get(reverse("asset-history", args=[asset.pk]))

        self.assertEqual(response.status_code, 200)
        timeline = response.data["timeline"]
        self.assertEqual(
            [entry["type"] for entry in timeline],
            [
                "asset_status",
                "defect",
                "maintenance_request",
                "work_order",
                "work_order_task",
                "work_order_completion",
                "work_order_closure",
            ],
        )
        request_entry = next(row for row in timeline if row["type"] == "maintenance_request")
        task_entry = next(row for row in timeline if row["type"] == "work_order_task")
        work_entry = next(row for row in timeline if row["type"] == "work_order")
        self.assertEqual(request_entry["source"], {"type": "defect", "id": str(defect.pk)})
        self.assertEqual(work_entry["number"], "WO-TRACE-1")
        self.assertEqual(task_entry["id"], str(task.pk))
        self.assertEqual(task_entry["links"]["work_order_id"], str(work_order.pk))
        self.assertEqual(timeline[-1]["status"], "Closed")
        self.assertEqual(
            [entry["occurred_at"] for entry in timeline],
            sorted(entry["occurred_at"] for entry in timeline),
        )
        for entry in timeline:
            self.assertTrue(
                {
                    "type",
                    "id",
                    "number",
                    "label",
                    "status",
                    "reason",
                    "occurred_at",
                    "source",
                    "links",
                }
                <= entry.keys()
            )
        self.assertIn("status_events", response.data)
        self.assertIn("meters", response.data)
        self.assertIn("audit_events", response.data)

    def test_history_timeline_excludes_cross_organization_chain(self) -> None:
        asset = self.create_asset()
        outsider = User.objects.create(username="outsider", organization=self.other_org)
        defect = Defect.objects.create(
            organization=self.other_org,
            asset=asset,
            reported_by=outsider,
            category="Secret",
            description="Other tenant data",
        )
        maintenance_request = MaintenanceRequest.objects.create(
            organization=self.other_org,
            asset=asset,
            defect=defect,
            submitted_by=outsider,
            summary="Other tenant request",
        )
        work_order = WorkOrder.objects.create(
            organization=self.other_org,
            number="WO-FOREIGN",
            asset=asset,
            request=maintenance_request,
            created_by=outsider,
            summary="Other tenant work",
        )
        WorkOrderTask.objects.create(
            organization=self.other_org,
            work_order=work_order,
            title="Other tenant task",
        )
        AssetStatusEvent.objects.create(
            organization=self.other_org,
            asset=asset,
            actor=outsider,
            previous_status="Available",
            new_status="OutOfService",
            reason="Other tenant status reason",
            source="work_order",
        )

        self.api.force_authenticate(self.manager)
        response: Any = self.api.get(reverse("asset-history", args=[asset.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["type"] for row in response.data["timeline"]], ["asset_status"])
        self.assertNotIn("Other tenant status reason", str(response.data["timeline"]))

    def test_history_timeline_includes_oos_and_return_to_service_reasons(self) -> None:
        asset = self.create_asset()
        self.api.force_authenticate(self.manager)
        self.api.post(
            reverse("asset-availability", args=[asset.pk]),
            {"status": "OutOfService", "reason": "Waiting for brake repair"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000015",
        )
        self.api.post(
            reverse("asset-availability", args=[asset.pk]),
            {"status": "Available", "reason": "Brake repair verified"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000016",
        )

        response: Any = self.api.get(reverse("asset-history", args=[asset.pk]))

        self.assertEqual(response.status_code, 200)
        status_entries = [row for row in response.data["timeline"] if row["type"] == "asset_status"]
        self.assertEqual(
            [row["status"] for row in status_entries],
            ["Available", "OutOfService", "Available"],
        )
        self.assertEqual(
            [row["reason"] for row in status_entries],
            ["Initial asset status", "Waiting for brake repair", "Brake repair verified"],
        )
        self.assertEqual(status_entries[-1]["source"], {"type": "web", "id": str(self.manager.pk)})
        self.assertEqual(status_entries[-1]["links"]["asset_id"], str(asset.pk))
        self.assertEqual(
            status_entries[-1]["links"]["asset_status_event_id"], status_entries[-1]["id"]
        )
