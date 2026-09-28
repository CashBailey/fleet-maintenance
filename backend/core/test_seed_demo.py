from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

from assets.models import Asset, Meter, MeterReading
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from inventory.models import Bin, Part, Warehouse
from maintenance.models import Defect, MaintenancePlan, MaintenanceRequest, WorkOrder
from purchasing.models import PurchaseOrder, PurchaseRequest, Receipt, Vendor

from .management.commands.seed_demo import seed_id
from .models import AuditEvent, Location, Organization, Role, User
from .permissions import permissions_for


@override_settings(
    DEBUG=True,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SeedDemoDeterminismTests(TestCase):
    def _seed(self) -> None:
        call_command("seed_demo", stdout=StringIO())

    @staticmethod
    def _snapshot() -> dict[str, object]:
        models = (
            Organization,
            Location,
            Role,
            User,
            Asset,
            Meter,
            MeterReading,
            MaintenancePlan,
            Defect,
            MaintenanceRequest,
            WorkOrder,
            Part,
            Warehouse,
            Bin,
            Vendor,
            PurchaseRequest,
            PurchaseOrder,
            Receipt,
            AuditEvent,
        )
        plan = MaintenancePlan.objects.get(name="5,000 Mile PM B")
        purchase_request = PurchaseRequest.objects.get(reason="Replenish shop safety stock")
        purchase_order = PurchaseOrder.objects.get(number="PO-DEMO-2401")
        asset = Asset.objects.get(unit_number="TRK-012")
        receipt = Receipt.objects.get(operation_id=seed_id("receipt:demo:po-2401:partial"))
        return {
            "ids": {
                model._meta.label_lower: tuple(
                    str(pk) for pk in model.objects.order_by("pk").values_list("pk", flat=True)
                )
                for model in models
            },
            "due_status": plan.due_status,
            "due_reasons": plan.due_reasons,
            "needed_by": purchase_request.needed_by,
            "expected_at": purchase_order.expected_at,
            "asset_created_at": asset.created_at,
            "receipt_received_at": receipt.received_at,
        }

    def test_repeated_seed_preserves_stable_entities_and_due_state(self) -> None:
        self._seed()
        first = self._snapshot()

        self.assertEqual(
            Asset.objects.get(unit_number="TRK-012").pk,
            seed_id("asset:gator-fleet:TRK-012"),
        )
        self.assertEqual(
            Vendor.objects.get(code="NAPA-HD").pk,
            seed_id("vendor:demo:napa-hd"),
        )
        self.assertEqual(
            PurchaseOrder.objects.get(number="PO-DEMO-2401").pk,
            seed_id("purchase-order:demo:2401"),
        )
        self.assertEqual(
            Receipt.objects.get(operation_id=seed_id("receipt:demo:po-2401:partial")).pk,
            seed_id("receipt:demo:po-2401:partial"),
        )
        self.assertEqual(first["due_status"], "Overdue")

        self._seed()
        self.assertEqual(self._snapshot(), first)

    def test_second_technician_has_stable_identity_without_finance_access(self) -> None:
        self._seed()
        original = User.objects.get(username="technician@example.com")
        second = User.objects.get(username="technician.two@example.com")

        self.assertEqual(original.pk, seed_id("user:technician@example.com"))
        self.assertEqual(second.pk, seed_id("user:technician.two@example.com"))
        self.assertEqual(second.role_slugs, {"technician"})
        self.assertTrue(second.check_password(os.environ.get("E2E_PASSWORD") or "DemoPass123!"))
        permissions = permissions_for(second)
        self.assertIn("maintenance.execute", permissions)
        self.assertIn("inventory.issue", permissions)
        self.assertFalse(
            any(permission.startswith(("financial.", "purchasing.")) for permission in permissions)
        )

    @override_settings(DEBUG=False)
    def test_production_mode_requires_an_explicit_isolated_environment_override(self) -> None:
        with patch.dict(os.environ, {"FLEETLINE_ALLOW_DEMO_SEED": ""}):
            with self.assertRaisesMessage(CommandError, "Refusing to load demonstration data"):
                self._seed()
