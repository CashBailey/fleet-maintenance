from __future__ import annotations

import uuid

from assets.models import Asset, AssetType, Component
from assets.services import install_component
from core.models import Organization, Role, User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import WorkOrderCloseSnapshot
from .services import create_work_order


class ComponentTaskTests(TestCase):
    """A work-order task may point at a component, making service history a query."""

    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Yard", slug="yard")
        truck_type = AssetType.objects.create(
            organization=self.org, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.org, asset_type=truck_type, unit_number="TRK-012"
        )
        self.other_asset = Asset.objects.create(
            organization=self.org, asset_type=truck_type, unit_number="TRK-007"
        )
        manager_role = Role.objects.create(
            organization=self.org, slug="fleet_manager", name="Fleet manager"
        )
        self.manager = User.objects.create(username="manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.api = APIClient()
        self.api.force_authenticate(self.manager)

        self.component = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        ).component
        self.elsewhere = install_component(
            asset=self.other_asset,
            actor=self.manager,
            kind="transmission",
            serial_number="ALLISON-3000-778",
        ).component
        self.bench = Component.objects.create(
            organization=self.org,
            kind=Component.Kind.TRANSMISSION,
            serial_number="BENCH-9001",
        )
        self.work_order = create_work_order(
            organization=self.org,
            actor=self.manager,
            asset=self.asset,
            summary="Transmission swap",
        )

    def _post_task(self, key: str = "", **body: object):
        return self.api.post(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/",
            {"title": "Replace filter", **body},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_task_accepts_a_component_installed_on_the_asset(self) -> None:
        response = self._post_task(component_id=str(self.component.pk))
        self.assertEqual(response.status_code, 201, response.content)
        task = response.json()["task"]
        self.assertEqual(task["component_id"], str(self.component.pk))
        self.assertEqual(task["component"]["serial_number"], "CUM-4567")
        self.assertEqual(task["component"]["kind_label"], "Engine")

    def test_task_without_a_component_reports_null(self) -> None:
        response = self._post_task()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["task"]["component_id"])
        self.assertIsNone(response.json()["task"]["component"])

    def test_task_rejects_a_component_open_on_another_asset(self) -> None:
        response = self._post_task(component_id=str(self.elsewhere.pk))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "component_not_on_asset")

    def test_task_accepts_an_uninstalled_component(self) -> None:
        """The transmission about to go in is not on the asset yet."""
        response = self._post_task(component_id=str(self.bench.pk))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["task"]["component_id"], str(self.bench.pk))

    def test_patch_null_clears_the_component(self) -> None:
        created = self._post_task(component_id=str(self.component.pk)).json()["task"]
        response = self.api.patch(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{created['id']}/",
            {"component_id": None},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["task"]["component_id"])

    def test_patch_without_component_id_leaves_it_alone(self) -> None:
        created = self._post_task(component_id=str(self.component.pk)).json()["task"]
        response = self.api.patch(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{created['id']}/",
            {"notes": "torqued to spec"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"]["component_id"], str(self.component.pk))

    def test_component_survives_into_the_close_snapshot(self) -> None:
        """Additive under snapshot schema_version 1 -- no snapshot code changes."""
        from .services import transition_work_order

        created = self._post_task(component_id=str(self.component.pk)).json()["task"]
        self.api.patch(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{created['id']}/",
            {"status": "Completed"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        for status in ("Ready", "InProgress", "Completed", "Closed"):
            transition_work_order(
                work_order=self.work_order,
                actor=self.manager,
                new_status=status,
                completion_summary="Swap complete" if status == "Completed" else "",
            )
        snapshot = WorkOrderCloseSnapshot.objects.get(work_order=self.work_order)
        task_rows = snapshot.snapshot["tasks"]
        self.assertEqual(task_rows[0]["component_id"], str(self.component.pk))
        self.assertEqual(task_rows[0]["component"]["serial_number"], "CUM-4567")

    def test_close_snapshot_freezes_the_asset_meters(self) -> None:
        """A repair closed without a completion meter still records usage."""
        from datetime import timedelta
        from decimal import Decimal

        from assets.models import Meter, MeterReading
        from django.utils import timezone

        from .services import transition_work_order

        odometer = Meter.objects.create(
            organization=self.org,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        reading = MeterReading.objects.create(
            organization=self.org,
            meter=odometer,
            value=Decimal("120500"),
            observed_at=timezone.now() - timedelta(hours=1),
            source="manual",
        )
        for status in ("Ready", "InProgress", "Completed", "Closed"):
            transition_work_order(
                work_order=self.work_order,
                actor=self.manager,
                new_status=status,
                completion_summary="Swap complete" if status == "Completed" else "",
            )
        snapshot = WorkOrderCloseSnapshot.objects.get(work_order=self.work_order)
        self.assertTrue(snapshot.snapshot["asset_meters"])
        self.assertEqual(snapshot.snapshot["asset_meters"][0]["reading_id"], str(reading.pk))
