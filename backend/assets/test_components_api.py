from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from core.models import Organization, Role, User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Asset, AssetType, Component, Meter, MeterReading
from .services import install_component, remove_component


class ComponentApiTests(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Yard", slug="yard")
        self.other_org = Organization.objects.create(name="Other", slug="other")
        truck_type = AssetType.objects.create(
            organization=self.org, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.org, asset_type=truck_type, unit_number="TRK-012"
        )
        manager_role = Role.objects.create(
            organization=self.org, slug="fleet_manager", name="Fleet manager"
        )
        driver_role = Role.objects.create(organization=self.org, slug="driver", name="Driver")
        self.manager = User.objects.create(username="manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.driver = User.objects.create(username="driver", organization=self.org)
        self.driver.roles.add(driver_role)
        self.asset.assigned_driver = self.driver
        self.asset.save(update_fields=["assigned_driver"])

        outsider_role = Role.objects.create(
            organization=self.other_org, slug="fleet_manager", name="Fleet manager"
        )
        self.outsider = User.objects.create(username="outsider", organization=self.other_org)
        self.outsider.roles.add(outsider_role)

        self.odometer = Meter.objects.create(
            organization=self.org,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        self.reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("120500"),
            observed_at=timezone.now() - timedelta(hours=1),
            source="manual",
        )
        self.api = APIClient()
        self.api.force_authenticate(self.manager)

    def _install(self, key: str = "", **body: object):
        return self.api.post(
            f"/api/v1/assets/{self.asset.pk}/components/",
            {"kind": "engine", "serial_number": "CUM-4567", **body},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_post_creates_and_returns_201(self) -> None:
        response = self._install()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["component"]["serial_number"], "CUM-4567")
        self.assertEqual(body["installation"]["asset"]["unit_number"], "TRK-012")
        self.assertEqual(len(body["installation"]["installed_meters"]), 1)

    def test_post_rejects_unknown_fields(self) -> None:
        response = self._install(colour="red")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_fields")

    def test_replaying_the_same_key_returns_the_same_installation(self) -> None:
        key = str(uuid.uuid4())
        first = self._install(key=key)
        second = self._install(key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["installation"]["id"], second.json()["installation"]["id"])
        self.assertEqual(Component.objects.filter(serial_number="CUM-4567").count(), 1)

    def test_list_returns_all_periods_newest_first(self) -> None:
        first = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(component=first.component, actor=self.manager, reason="Bench test only")
        install_component(asset=self.asset, actor=self.manager, component=first.component)
        response = self.api.get(f"/api/v1/assets/{self.asset.pk}/components/")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["installations"]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["removed_at"])
        self.assertIsNotNone(rows[1]["removed_at"])

    def test_remove_returns_200_and_the_closed_installation(self) -> None:
        component_id = self._install().json()["component"]["id"]
        response = self.api.post(
            f"/api/v1/assets/components/{component_id}/remove/",
            {"reason": "Bench test only"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 200, response.content)
        installation = response.json()["installation"]
        self.assertEqual(installation["removal_reason"], "Bench test only")
        self.assertIsNotNone(installation["removed_at"])

    def test_remove_without_a_reason_is_rejected(self) -> None:
        component_id = self._install().json()["component"]["id"]
        response = self.api.post(
            f"/api/v1/assets/components/{component_id}/remove/",
            {"reason": "  "},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "reason_required")

    def test_detail_includes_installations_and_service_history(self) -> None:
        component_id = self._install().json()["component"]["id"]
        response = self.api.get(f"/api/v1/assets/components/{component_id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["component"]["serial_number"], "CUM-4567")
        self.assertEqual(len(body["installations"]), 1)
        self.assertEqual(body["tasks"], [])
        self.assertEqual(body["work_orders"], [])

    def test_another_organization_gets_404(self) -> None:
        component_id = self._install().json()["component"]["id"]
        self.api.force_authenticate(self.outsider)
        self.assertEqual(
            self.api.get(f"/api/v1/assets/components/{component_id}/").status_code, 404
        )
        self.assertEqual(
            self.api.get(f"/api/v1/assets/{self.asset.pk}/components/").status_code, 404
        )
        remove = self.api.post(
            f"/api/v1/assets/components/{component_id}/remove/",
            {"reason": "Nope"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(remove.status_code, 404)

    def test_driver_may_read_but_not_write(self) -> None:
        install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        self.api.force_authenticate(self.driver)
        self.assertEqual(
            self.api.get(f"/api/v1/assets/{self.asset.pk}/components/").status_code, 200
        )
        response = self._install()
        self.assertEqual(response.status_code, 403)

    def test_asset_history_includes_installations_and_both_timeline_types(self) -> None:
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(
            component=installation.component, actor=self.manager, reason="Bench test only"
        )
        body = self.api.get(f"/api/v1/assets/{self.asset.pk}/history/").json()
        self.assertEqual(len(body["component_installations"]), 1)
        kinds = {entry["type"] for entry in body["timeline"]}
        self.assertIn("component_installed", kinds)
        self.assertIn("component_removed", kinds)
        entry = next(e for e in body["timeline"] if e["type"] == "component_installed")
        self.assertEqual(entry["links"]["component_id"], str(installation.component_id))

    def test_search_finds_a_component_by_serial(self) -> None:
        install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        response = self.api.get("/api/v1/search/", {"q": "CUM-4567"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        match = next(row for row in rows if row["type"] == "component")
        self.assertEqual(match["label"], "CUM-4567")
        self.assertEqual(match["detail"], "Engine")

    def test_search_hides_components_from_a_driver(self) -> None:
        install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        self.api.force_authenticate(self.driver)
        rows = self.api.get("/api/v1/search/", {"q": "CUM-4567"}).json()["results"]
        self.assertEqual([row for row in rows if row["type"] == "component"], [])
