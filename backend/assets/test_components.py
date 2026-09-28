from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from core.exceptions import DomainError
from core.models import AuditEvent, Organization, Role, User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Asset, AssetType, Component, Meter, MeterReading
from .services import install_component


class ComponentInstallTests(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Yard", slug="yard")
        self.truck_type = AssetType.objects.create(
            organization=self.org, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.org, asset_type=self.truck_type, unit_number="TRK-012"
        )
        manager_role = Role.objects.create(
            organization=self.org, slug="fleet_manager", name="Fleet manager"
        )
        self.manager = User.objects.create(username="manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.odometer = Meter.objects.create(
            organization=self.org,
            asset=self.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        self.now = timezone.now()
        self.reading = MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("120500"),
            observed_at=self.now - timedelta(hours=1),
            source="manual",
        )

    def test_install_creates_component_and_snapshots_meters(self) -> None:
        installation = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="  cum-4567 ",
            manufacturer="Cummins",
            model="X15",
        )
        component = installation.component
        self.assertEqual(component.serial_number, "CUM-4567")
        self.assertEqual(component.kind, "engine")
        self.assertIsNone(installation.removed_at)
        self.assertEqual(installation.installed_by_id, self.manager.pk)
        self.assertEqual(len(installation.installed_meters), 1)
        snapshot = installation.installed_meters[0]
        self.assertEqual(snapshot["reading_id"], str(self.reading.pk))
        self.assertEqual(snapshot["kind"], Meter.Kind.ODOMETER)
        self.assertEqual(snapshot["unit"], "mi")
        actions = set(
            AuditEvent.objects.filter(organization=self.org).values_list("action", flat=True)
        )
        self.assertIn("component.created", actions)
        self.assertIn("component.installed", actions)

    def test_install_snapshots_the_reading_as_of_installed_at(self) -> None:
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("121000"),
            observed_at=self.now,
            source="manual",
        )
        installation = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="CUM-4567",
            installed_at=self.now - timedelta(minutes=30),
        )
        self.assertEqual(installation.installed_meters[0]["reading_id"], str(self.reading.pk))

    def test_install_of_a_component_open_elsewhere_is_rejected(self) -> None:
        other = Asset.objects.create(
            organization=self.org, asset_type=self.truck_type, unit_number="TRK-007"
        )
        install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=other, actor=self.manager, kind="engine", serial_number="CUM-4567"
            )
        error = caught.exception
        self.assertEqual(error.code, "component_installed_elsewhere")
        self.assertEqual(error.status, 409)
        self.assertEqual(error.details["unit_number"], "TRK-012")
        self.assertEqual(Component.objects.get(serial_number="CUM-4567").installations.count(), 1)

    def test_future_installed_at_is_rejected(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.manager,
                kind="engine",
                serial_number="CUM-4567",
                installed_at=self.now + timedelta(days=1),
            )
        self.assertEqual(caught.exception.code, "invalid_installed_at")

    def test_identity_is_required_when_no_component_is_given(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(asset=self.asset, actor=self.manager, kind="engine")
        self.assertEqual(caught.exception.code, "component_identity_required")

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.manager, kind="turbo", serial_number="T-1234"
            )
        self.assertEqual(caught.exception.code, "invalid_component_kind")

    def test_install_onto_a_retired_asset_is_rejected(self) -> None:
        self.asset.status = Asset.Status.RETIRED
        self.asset.archived_at = timezone.now()
        self.asset.save(update_fields=["status", "archived_at"])
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
            )
        self.assertEqual(caught.exception.code, "asset_retired")

    def test_a_meter_correction_leaves_the_snapshot_alone(self) -> None:
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        original = installation.installed_meters[0]
        MeterReading.objects.create(
            organization=self.org,
            meter=self.odometer,
            value=Decimal("119000"),
            observed_at=self.reading.observed_at,
            source="manual",
            corrects=self.reading,
        )
        installation.refresh_from_db()
        self.assertEqual(installation.installed_meters[0], original)
        self.assertEqual(installation.installed_meters[0]["reading_id"], str(self.reading.pk))

    def test_an_asset_with_no_readings_still_installs(self) -> None:
        bare = Asset.objects.create(
            organization=self.org, asset_type=self.truck_type, unit_number="TRK-101"
        )
        installation = install_component(
            asset=bare, actor=self.manager, kind="apu", serial_number="APU-2211"
        )
        self.assertEqual(installation.installed_meters, [])

    def test_backdated_install_inside_a_closed_period_is_rejected(self) -> None:
        from .services import remove_component

        first = install_component(
            asset=self.asset,
            actor=self.manager,
            kind="engine",
            serial_number="CUM-4567",
            installed_at=self.now - timedelta(days=10),
        )
        remove_component(
            component=first.component,
            actor=self.manager,
            reason="Bench test only",
            removed_at=self.now - timedelta(days=5),
        )
        with self.assertRaises((DomainError, ValidationError)):
            install_component(
                asset=self.asset,
                actor=self.manager,
                component=first.component,
                installed_at=self.now - timedelta(days=7),
            )


class ComponentRemovalTests(ComponentInstallTests):
    def test_remove_requires_a_reason(self) -> None:
        from .services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        with self.assertRaises(DomainError) as caught:
            remove_component(component=installation.component, actor=self.manager, reason="   ")
        self.assertEqual(caught.exception.code, "reason_required")

    def test_remove_closes_the_period_and_snapshots_meters(self) -> None:
        from .services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        closed = remove_component(
            component=installation.component, actor=self.manager, reason="Bench test only"
        )
        self.assertEqual(closed.pk, installation.pk)
        self.assertIsNotNone(closed.removed_at)
        self.assertEqual(closed.removal_reason, "Bench test only")
        self.assertEqual(closed.removed_by_id, self.manager.pk)
        self.assertEqual(len(closed.removed_meters), 1)

    def test_removing_twice_is_rejected(self) -> None:
        from .services import remove_component

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(
            component=installation.component, actor=self.manager, reason="Bench test only"
        )
        with self.assertRaises(DomainError) as caught:
            remove_component(component=installation.component, actor=self.manager, reason="Again")
        self.assertEqual(caught.exception.code, "component_not_installed")

    def test_reinstall_reuses_the_component_and_adds_a_second_period(self) -> None:
        from .services import remove_component

        first = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        remove_component(component=first.component, actor=self.manager, reason="Bench test only")
        second = install_component(asset=self.asset, actor=self.manager, component=first.component)
        self.assertEqual(Component.objects.filter(serial_number="CUM-4567").count(), 1)
        self.assertEqual(first.component.installations.count(), 2)
        self.assertNotEqual(first.pk, second.pk)

    def test_retiring_an_asset_closes_its_open_installations(self) -> None:
        from .models import AssetStatusEvent
        from .services import change_asset_status

        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        change_asset_status(
            asset=self.asset,
            actor=self.manager,
            new_status=Asset.Status.RETIRED,
            reason="Sold at auction",
            disposition="Sold at auction",
            final_meter_readings=[self.reading],
        )
        installation.refresh_from_db()
        self.assertIsNotNone(installation.removed_at)
        self.assertEqual(installation.removal_reason, "Asset retired")
        self.assertTrue(installation.removed_meters)
        event = AssetStatusEvent.objects.filter(
            asset=self.asset, new_status=Asset.Status.RETIRED
        ).latest("occurred_at")
        self.assertEqual(event.context["removed_component_ids"], [str(installation.component_id)])


class ComponentAuthorizationTests(TestCase):
    """Technicians act through an assigned open work order; managers act freely."""

    def setUp(self) -> None:
        from maintenance.services import create_work_order

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
        tech_role = Role.objects.create(organization=self.org, slug="technician", name="Technician")
        driver_role = Role.objects.create(organization=self.org, slug="driver", name="Driver")
        self.manager = User.objects.create(username="manager", organization=self.org)
        self.manager.roles.add(manager_role)
        self.technician = User.objects.create(username="tech", organization=self.org)
        self.technician.roles.add(tech_role)
        self.other_technician = User.objects.create(username="tech2", organization=self.org)
        self.other_technician.roles.add(tech_role)
        self.driver = User.objects.create(username="driver", organization=self.org)
        self.driver.roles.add(driver_role)
        self.work_order = create_work_order(
            organization=self.org,
            actor=self.manager,
            asset=self.asset,
            summary="Transmission swap",
            assigned_to=self.technician,
        )
        self.other_work_order = create_work_order(
            organization=self.org,
            actor=self.manager,
            asset=self.other_asset,
            summary="Unrelated",
        )

    def test_manager_may_install_without_a_work_order(self) -> None:
        installation = install_component(
            asset=self.asset, actor=self.manager, kind="engine", serial_number="CUM-4567"
        )
        self.assertIsNone(installation.installed_work_order_id)

    def test_technician_without_a_work_order_is_refused(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.technician,
                kind="engine",
                serial_number="CUM-4567",
            )
        self.assertEqual(caught.exception.code, "work_order_required")
        self.assertEqual(caught.exception.status, 403)

    def test_technician_with_an_assigned_open_work_order_may_install(self) -> None:
        installation = install_component(
            asset=self.asset,
            actor=self.technician,
            kind="engine",
            serial_number="CUM-4567",
            work_order=self.work_order,
        )
        self.assertEqual(installation.installed_work_order_id, self.work_order.pk)

    def test_technician_not_assigned_is_refused(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.other_technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.work_order,
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_a_closed_work_order_is_refused(self) -> None:
        self.work_order.status = "Closed"
        self.work_order.save(update_fields=["status"])
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.work_order,
            )
        self.assertEqual(caught.exception.code, "work_order_not_open")
        self.assertEqual(caught.exception.status, 409)

    def test_a_work_order_on_another_asset_is_refused(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset,
                actor=self.technician,
                kind="engine",
                serial_number="CUM-4567",
                work_order=self.other_work_order,
            )
        self.assertEqual(caught.exception.code, "invalid_work_order")

    def test_a_driver_may_not_install(self) -> None:
        with self.assertRaises(DomainError) as caught:
            install_component(
                asset=self.asset, actor=self.driver, kind="engine", serial_number="CUM-4567"
            )
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(caught.exception.status, 403)
