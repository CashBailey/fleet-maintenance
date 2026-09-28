from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from assets.models import Asset, AssetType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from inventory.models import Bin, Part, StockBalance, Warehouse
from maintenance.models import InspectionTemplate, WorkOrder
from rest_framework.test import APIClient

from core.models import ApiToken, Location, Organization, Role, User


class BootstrapScopeTests(TestCase):
    organization: Organization
    location: Location
    roles: dict[str, Role]
    driver: User
    technician: User
    parts_clerk: User
    fleet_manager: User
    system_admin: User
    integration_admin: User
    assigned_asset: Asset
    other_asset: Asset
    assigned_work: WorkOrder
    other_work: WorkOrder
    part: Part
    balance: StockBalance
    template: InspectionTemplate

    @classmethod
    def setUpTestData(cls) -> None:
        cls.organization = Organization.objects.create(name="Bootstrap Fleet", slug="bootstrap")
        cls.location = Location.objects.create(
            organization=cls.organization,
            name="Main Shop",
            code="MAIN",
        )
        role_slugs = (
            "driver",
            "technician",
            "parts_clerk",
            "fleet_manager",
            "system_admin",
            "integration_admin",
        )
        cls.roles = {
            slug: Role.objects.create(
                organization=cls.organization,
                slug=slug,
                name=slug.replace("_", " ").title(),
            )
            for slug in role_slugs
        }
        cls.driver = cls._user("scope-driver", "driver")
        cls.technician = cls._user("scope-technician", "technician")
        cls.parts_clerk = cls._user("scope-parts", "parts_clerk")
        cls.fleet_manager = cls._user("scope-manager", "fleet_manager")
        cls.system_admin = cls._user("scope-admin", "system_admin")
        cls.integration_admin = cls._user("scope-integration", "integration_admin")

        asset_type = AssetType.objects.create(
            organization=cls.organization,
            name="Truck",
        )
        cls.assigned_asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            assigned_driver=cls.driver,
            unit_number="SCOPE-01",
        )
        cls.other_asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="SCOPE-02",
        )
        cls.assigned_work = WorkOrder.objects.create(
            organization=cls.organization,
            asset=cls.assigned_asset,
            created_by=cls.fleet_manager,
            assigned_to=cls.technician,
            number="WO-SCOPE-01",
            summary="Assigned work",
        )
        cls.other_work = WorkOrder.objects.create(
            organization=cls.organization,
            asset=cls.other_asset,
            created_by=cls.fleet_manager,
            number="WO-SCOPE-02",
            summary="Other work",
        )
        cls.template = InspectionTemplate.objects.create(
            organization=cls.organization,
            created_by=cls.fleet_manager,
            name="Scoped pre-trip",
            questions=[],
        )
        warehouse = Warehouse.objects.create(
            organization=cls.organization,
            location=cls.location,
            code="MAIN",
            name="Main warehouse",
        )
        bin_record = Bin.objects.create(
            organization=cls.organization,
            warehouse=warehouse,
            code="A-01",
        )
        cls.part = Part.objects.create(
            organization=cls.organization,
            number="SCOPE-PART",
            name="Scoped part",
        )
        cls.balance = StockBalance.objects.create(
            organization=cls.organization,
            part=cls.part,
            bin=bin_record,
            quantity_on_hand=Decimal("4"),
        )

    @classmethod
    def _user(cls, username: str, role_slug: str) -> User:
        user = User.objects.create_user(
            username=username,
            organization=cls.organization,
            default_location=cls.location,
        )
        user.roles.add(cls.roles[role_slug])
        return user

    def bootstrap(self, user: User, token: ApiToken | None = None) -> dict[str, Any]:
        client = APIClient()
        client.force_authenticate(user=user, token=token)
        response = client.get(reverse("bootstrap"))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_driver_receives_only_assigned_assets_and_field_templates(self) -> None:
        payload = self.bootstrap(self.driver)

        self.assertEqual([row["id"] for row in payload["assets"]], [str(self.assigned_asset.pk)])
        self.assertEqual(payload["work_orders"], [])
        self.assertEqual(payload["parts"], [])
        self.assertEqual(payload["stock"], [])
        self.assertEqual(
            [row["id"] for row in payload["inspection_templates"]],
            [str(self.template.pk)],
        )

    def test_technician_receives_only_assigned_work_and_required_field_data(self) -> None:
        payload = self.bootstrap(self.technician)

        self.assertEqual(
            {row["id"] for row in payload["assets"]},
            {str(self.assigned_asset.pk), str(self.other_asset.pk)},
        )
        self.assertEqual(
            [row["id"] for row in payload["work_orders"]],
            [str(self.assigned_work.pk)],
        )
        self.assertEqual([row["id"] for row in payload["parts"]], [str(self.part.pk)])
        self.assertEqual([row["id"] for row in payload["stock"]], [str(self.balance.pk)])
        self.assertEqual(
            [row["id"] for row in payload["inspection_templates"]],
            [str(self.template.pk)],
        )

    def test_inventory_and_non_operational_roles_receive_only_permitted_domains(self) -> None:
        parts_payload = self.bootstrap(self.parts_clerk)
        self.assertEqual(
            {row["id"] for row in parts_payload["work_orders"]},
            {str(self.assigned_work.pk), str(self.other_work.pk)},
        )
        self.assertEqual([row["id"] for row in parts_payload["parts"]], [str(self.part.pk)])
        self.assertEqual(parts_payload["inspection_templates"], [])

        admin_payload = self.bootstrap(self.system_admin)
        for collection in ("assets", "work_orders", "parts", "stock", "inspection_templates"):
            self.assertEqual(admin_payload[collection], [])

        integration_payload = self.bootstrap(self.integration_admin)
        self.assertEqual(len(integration_payload["assets"]), 2)
        for collection in ("work_orders", "parts", "stock", "inspection_templates"):
            self.assertEqual(integration_payload[collection], [])

    def test_dashboard_only_api_token_cannot_expand_into_operational_bootstrap_data(self) -> None:
        token = ApiToken.objects.create(
            organization=self.organization,
            user=self.fleet_manager,
            name="Dashboard only",
            prefix="scope",
            token_hash="0" * 64,
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )

        payload = self.bootstrap(self.fleet_manager, token)

        self.assertEqual(payload["user"]["permissions"], ["dashboard.view"])
        self.assertEqual(payload["user"]["navigation"], [])
        for collection in ("assets", "work_orders", "parts", "stock", "inspection_templates"):
            self.assertEqual(payload[collection], [])
