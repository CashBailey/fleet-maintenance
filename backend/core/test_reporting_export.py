from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from assets.models import Asset, AssetStatusEvent, AssetType, Meter, MeterReading
from django.test import TestCase
from django.utils import timezone
from integrations.models import (
    Device,
    DeviceAssetAssociation,
    NormalizedTelematicsEvent,
    TelematicsMessage,
)
from inventory.models import (
    Bin,
    InventoryCount,
    InventoryCountLine,
    Part,
    PartCrossReference,
    Reservation,
    StockBalance,
    StockTransaction,
    Warehouse,
)
from maintenance.models import (
    Defect,
    Inspection,
    InspectionFinding,
    InspectionResponse,
    InspectionTemplate,
    LaborEntry,
    MaintenanceAlert,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderCloseSnapshot,
    WorkOrderTask,
)
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Vendor,
    VendorPart,
)
from purchasing.services import post_receipt
from rest_framework.test import APIClient

from .models import (
    Attachment,
    AuditEvent,
    Comment,
    Location,
    Notification,
    Organization,
    Role,
    SyncConflict,
    User,
)
from .reporting import EXPORTED_MODEL_KEYS


class ReportingExportTests(TestCase):
    org: Organization
    other_org: Organization
    location: Location
    roles: dict[str, Role]
    users: dict[str, User]
    asset: Asset
    work_order: WorkOrder
    close_snapshot: WorkOrderCloseSnapshot
    current_labor: LaborEntry
    part: Part
    bin: Bin
    device: Device
    message: TelematicsMessage

    @classmethod
    def setUpTestData(cls) -> None:
        cls.org = Organization.objects.create(name="Report Fleet", slug="report-fleet")
        cls.other_org = Organization.objects.create(name="Private Fleet", slug="private-fleet")
        cls.location = Location.objects.create(organization=cls.org, name="Main Shop", code="MAIN")
        other_location = Location.objects.create(
            organization=cls.other_org, name="Private Shop", code="PRIVATE"
        )
        cls.roles = {
            slug: Role.objects.create(
                organization=cls.org, slug=slug, name=slug.replace("_", " ").title()
            )
            for slug in (
                "fleet_manager",
                "management",
                "parts_clerk",
                "purchasing_manager",
                "integration_admin",
                "driver",
            )
        }
        cls.users = {slug: cls._user(slug, role) for slug, role in cls.roles.items()}
        cls.users["fleet_manager"].mfa_secret = "MFA-MUST-NOT-EXPORT"  # noqa: S105
        cls.users["fleet_manager"].save(update_fields=["mfa_secret"])
        other_role = Role.objects.create(
            organization=cls.other_org, slug="fleet_manager", name="Fleet Manager"
        )
        other_user = User.objects.create_user(
            username="private-manager",
            password="PRIVATE-PASSWORD-MUST-NOT-EXPORT",  # noqa: S106
            organization=cls.other_org,
            default_location=other_location,
        )
        other_user.roles.add(other_role)

        asset_type = AssetType.objects.create(
            organization=cls.org, name="Truck", category="vehicle"
        )
        cls.asset = Asset.objects.create(
            organization=cls.org,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="RPT-100",
            status=Asset.Status.OUT_OF_SERVICE,
        )
        AssetStatusEvent.objects.create(
            organization=cls.org,
            asset=cls.asset,
            actor=cls.users["fleet_manager"],
            previous_status=Asset.Status.AVAILABLE,
            new_status=Asset.Status.OUT_OF_SERVICE,
            reason="Report fixture downtime",
        )
        meter = Meter.objects.create(
            organization=cls.org,
            asset=cls.asset,
            name="Odometer",
            kind=Meter.Kind.ODOMETER,
            unit="mi",
        )
        meter_reading = MeterReading.objects.create(
            organization=cls.org,
            meter=meter,
            value=Decimal("1000"),
            observed_at=timezone.now(),
            source="manual",
            created_by=cls.users["fleet_manager"],
        )

        package = ServicePackage.objects.create(
            organization=cls.org,
            name="Annual service",
            tasks=[{"title": "Inspect"}],
            created_by=cls.users["fleet_manager"],
        )
        plan = MaintenancePlan.objects.create(
            organization=cls.org,
            asset=cls.asset,
            service_package=package,
            name="Annual PM",
            due_status="Overdue",
        )
        MaintenanceTrigger.objects.create(
            organization=cls.org,
            plan=plan,
            kind="date",
            interval=Decimal("365"),
        )
        template = InspectionTemplate.objects.create(
            organization=cls.org,
            name="DVIR",
            questions=[{"id": "brakes", "label": "Brakes", "required": True}],
            created_by=cls.users["fleet_manager"],
        )
        inspection = Inspection.objects.create(
            organization=cls.org,
            asset=cls.asset,
            template=template,
            template_snapshot={"name": "DVIR", "version": 1},
            performed_by=cls.users["driver"],
            status="Draft",
            started_at=timezone.now(),
        )
        inspection_response = InspectionResponse.objects.create(
            organization=cls.org,
            inspection=inspection,
            question_id="brakes",
            question="Brakes",
            result="fail",
            required=True,
        )
        finding = InspectionFinding.objects.create(
            organization=cls.org,
            inspection=inspection,
            response=inspection_response,
            asset=cls.asset,
            reported_by=cls.users["driver"],
            description="Brake pull",
        )
        defect = Defect.objects.create(
            organization=cls.org,
            asset=cls.asset,
            inspection_response=inspection_response,
            inspection_finding=finding,
            reported_by=cls.users["driver"],
            category="brakes",
            description="Brake pull",
        )
        MaintenanceAlert.objects.create(
            organization=cls.org,
            asset=cls.asset,
            source_id="fixture-alert",
            title="Meter signal",
            dedupe_key="fixture-alert",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        request = MaintenanceRequest.objects.create(
            organization=cls.org,
            asset=cls.asset,
            defect=defect,
            submitted_by=cls.users["fleet_manager"],
            status="Converted",
            summary="Repair brakes",
        )
        cls.work_order = WorkOrder.objects.create(
            organization=cls.org,
            number="WO-RPT-100",
            asset=cls.asset,
            request=request,
            maintenance_plan=plan,
            service_package=package,
            service_package_snapshot={"name": package.name, "version": package.version},
            created_by=cls.users["fleet_manager"],
            assigned_to=cls.users["fleet_manager"],
            status="Ready",
            summary="Sensitive operational repair",
        )
        cls.close_snapshot = WorkOrderCloseSnapshot.objects.create(
            organization=cls.org,
            work_order=cls.work_order,
            sequence=1,
            closed_by=cls.users["fleet_manager"],
            closed_at=timezone.now(),
            snapshot={"status": "Closed", "summary": "Earlier closure"},
        )
        WorkOrderTask.objects.create(
            organization=cls.org,
            work_order=cls.work_order,
            title="Inspect brakes",
        )
        original_labor = LaborEntry.objects.create(
            organization=cls.org,
            work_order=cls.work_order,
            technician=cls.users["fleet_manager"],
            minutes=60,
            hourly_rate=Decimal("60"),
            note="Superseded labor",
        )
        cls.current_labor = LaborEntry.objects.create(
            organization=cls.org,
            work_order=cls.work_order,
            technician=cls.users["fleet_manager"],
            minutes=30,
            hourly_rate=Decimal("60"),
            note="Corrected labor",
            corrects=original_labor,
        )

        cls.part = Part.objects.create(
            organization=cls.org,
            number="RPT-PART",
            name="Report part",
            barcode="RPT-BARCODE",
            default_unit_cost=Decimal("10"),
        )
        PartCrossReference.objects.create(
            organization=cls.org,
            part=cls.part,
            kind=PartCrossReference.Kind.ALTERNATE,
            value="RPT-ALT",
        )
        warehouse = Warehouse.objects.create(
            organization=cls.org,
            location=cls.location,
            code="MAIN",
            name="Main Warehouse",
        )
        cls.bin = Bin.objects.create(
            organization=cls.org, warehouse=warehouse, code="A1", name="Primary"
        )
        StockBalance.objects.create(
            organization=cls.org,
            part=cls.part,
            bin=cls.bin,
            quantity_on_hand=Decimal("8"),
            quantity_reserved=Decimal("2"),
        )
        issue = cls._stock_transaction("ISSUE", "-2", "10", "20")
        cls._stock_transaction("RETURN", ".5", "10", "-5", original=issue)
        reversed_issue = cls._stock_transaction("ISSUE", "-1", "7", "7")
        cls._stock_transaction("REVERSAL", "1", "7", "-7", original=reversed_issue)
        Reservation.objects.create(
            organization=cls.org,
            part=cls.part,
            bin=cls.bin,
            work_order=cls.work_order,
            requested_quantity=Decimal("2"),
            operation_id=uuid.uuid4(),
            created_by=cls.users["fleet_manager"],
        )
        inventory_count = InventoryCount.objects.create(
            organization=cls.org,
            warehouse=warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            created_by=cls.users["fleet_manager"],
            status=InventoryCount.Status.POSTED,
        )
        InventoryCountLine.objects.create(
            organization=cls.org,
            inventory_count=inventory_count,
            part=cls.part,
            bin=cls.bin,
            expected_quantity=Decimal("8"),
            counted_quantity=Decimal("8"),
            variance=Decimal("0"),
        )

        vendor = Vendor.objects.create(
            organization=cls.org, code="RPT-VENDOR", name="Report Vendor"
        )
        VendorPart.objects.create(
            organization=cls.org,
            vendor=vendor,
            part=cls.part,
            vendor_part_number="VP-RPT",
            unit_cost=Decimal("3"),
        )
        purchase_request = PurchaseRequest.objects.create(
            organization=cls.org,
            part=cls.part,
            requested_by=cls.users["purchasing_manager"],
            quantity=Decimal("10"),
            reason="Reorder",
        )
        purchase_order = PurchaseOrder.objects.create(
            organization=cls.org,
            number="PO-RPT-100",
            vendor=vendor,
            status=PurchaseOrder.Status.SENT,
            created_by=cls.users["purchasing_manager"],
        )
        purchase_order_line = PurchaseOrderLine.objects.create(
            organization=cls.org,
            purchase_order=purchase_order,
            purchase_request=purchase_request,
            part=cls.part,
            description="Report part",
            quantity_ordered=Decimal("10"),
            unit_cost=Decimal("3"),
        )
        post_receipt(
            organization=cls.org,
            actor=cls.users["purchasing_manager"],
            purchase_order=purchase_order,
            lines=[
                {
                    "purchase_order_line": purchase_order_line,
                    "bin": cls.bin,
                    "quantity": Decimal("2"),
                }
            ],
            operation_id=uuid.uuid4(),
        )

        Attachment.objects.create(
            organization=cls.org,
            uploader=cls.users["driver"],
            resource_type="defect",
            resource_id=str(defect.pk),
            file="attachments/report-fixture/evidence.txt",
            original_name="evidence.txt",
            content_type="text/plain",
            size=8,
            sha256="integrity-hash",
        )
        Comment.objects.create(
            organization=cls.org,
            author=cls.users["fleet_manager"],
            resource_type="work_order",
            resource_id=str(cls.work_order.pk),
            body="Repair comment",
        )
        Notification.objects.create(
            organization=cls.org,
            user=cls.users["fleet_manager"],
            title="Work ready",
            body="WO-RPT-100 is ready",
        )
        AuditEvent.objects.create(
            organization=cls.org,
            actor=cls.users["fleet_manager"],
            action="work_order.created",
            resource_type="WorkOrder",
            resource_id=str(cls.work_order.pk),
        )
        SyncConflict.objects.create(
            organization=cls.org,
            user=cls.users["driver"],
            operation_id=uuid.uuid4(),
            operation_type="defect.create",
            message="Conflict fixture",
        )

        cls.device = Device.objects.create(
            organization=cls.org,
            name="AutoPi report device",
            serial_number="RPT-DEVICE",
            external_id="RPT-DEVICE",
            token_prefix="rpt-token",  # noqa: S106
            token_hash="DEVICE-TOKEN-HASH-MUST-NOT-EXPORT",  # noqa: S106
        )
        DeviceAssetAssociation.objects.create(
            organization=cls.org,
            device=cls.device,
            asset=cls.asset,
            effective_from=timezone.now(),
            assigned_by=cls.users["integration_admin"],
        )
        cls.message = TelematicsMessage.objects.create(
            organization=cls.org,
            device=cls.device,
            source="autopi",
            schema_version="1.0",
            message_id="RPT-MESSAGE",
            canonical_hash="integrity-message-hash",
            message_type="meter",
            observed_at=timezone.now(),
            received_at=timezone.now(),
            raw_payload={"value": "1000", "unit": "mi"},
            status=TelematicsMessage.Status.ACCEPTED,
        )
        NormalizedTelematicsEvent.objects.create(
            organization=cls.org,
            message=cls.message,
            device=cls.device,
            asset=cls.asset,
            kind=NormalizedTelematicsEvent.Kind.METER,
            signal="odometer",
            value=Decimal("1000"),
            unit="mi",
            observed_at=timezone.now(),
            quality=NormalizedTelematicsEvent.Quality.ACCEPTED,
            normalized_payload={"value": "1000", "unit": "mi"},
            meter_reading=meter_reading,
        )

        other_type = AssetType.objects.create(
            organization=cls.other_org, name="Private Truck", category="vehicle"
        )
        Asset.objects.create(
            organization=cls.other_org,
            asset_type=other_type,
            home_location=other_location,
            unit_number="PRIVATE-EXPORT-ROW",
        )

    @classmethod
    def _user(cls, slug: str, role: Role) -> User:
        user = User.objects.create_user(
            username=f"{slug}@example.com",
            password="Report-test-password-2026",  # noqa: S106
            organization=cls.org,
            default_location=cls.location,
        )
        user.roles.add(role)
        return user

    @classmethod
    def _stock_transaction(
        cls,
        transaction_type: str,
        quantity: str,
        unit_cost: str,
        total_cost: str,
        *,
        original: StockTransaction | None = None,
    ) -> StockTransaction:
        return StockTransaction.objects.create(
            organization=cls.org,
            part=cls.part,
            bin=cls.bin,
            transaction_type=transaction_type,
            quantity=Decimal(quantity),
            unit_cost=Decimal(unit_cost),
            total_cost=Decimal(total_cost),
            work_order=cls.work_order,
            original_transaction=original,
            operation_id=uuid.uuid4(),
            actor=cls.users["fleet_manager"],
        )

    def _get_report(self, role: str) -> dict[str, Any]:
        client = APIClient()
        client.force_authenticate(self.users[role])
        response = client.get("/api/v1/reports/operations/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_operational_totals_reconcile_to_correction_and_compensation_sources(self) -> None:
        payload = self._get_report("fleet_manager")

        self.assertEqual(payload["summary"]["labor_cost"], "30.00")
        labor = payload["drilldown"]["labor_cost"]
        self.assertEqual(labor["source_total"], payload["summary"]["labor_cost"])
        self.assertEqual([row["id"] for row in labor["sources"]], [str(self.current_labor.pk)])

        self.assertEqual(payload["summary"]["part_cost"], "15.0000")
        parts = payload["drilldown"]["part_cost"]
        self.assertEqual(parts["source_total"], payload["summary"]["part_cost"])
        self.assertEqual(
            Decimal(parts["source_total"]),
            sum((Decimal(row["contribution"]) for row in parts["sources"]), Decimal("0")),
        )
        self.assertEqual(
            {row["transaction_type"] for row in parts["sources"]},
            {"ISSUE", "RETURN", "REVERSAL"},
        )
        self.assertEqual(payload["summary"]["open_work_orders"], 1)
        self.assertEqual(payload["source_records"][0]["id"], str(self.work_order.pk))
        self.assertNotIn("PRIVATE-EXPORT-ROW", json.dumps(payload))

    def test_report_roles_receive_only_their_report_domain(self) -> None:
        executive = self._get_report("management")
        self.assertEqual(
            set(executive["summary"]), {"asset_count", "out_of_service", "downtime_events"}
        )
        self.assertEqual(executive["source_records"], [])
        self.assertNotIn("Sensitive operational repair", json.dumps(executive))

        inventory = self._get_report("parts_clerk")
        self.assertEqual(
            set(inventory["summary"]),
            {"part_count", "quantity_on_hand", "quantity_reserved"},
        )
        self.assertNotIn("labor_cost", inventory["drilldown"])
        self.assertNotIn("part_cost", inventory["drilldown"])
        self.assertNotIn("Sensitive operational repair", json.dumps(inventory))
        self.assertNotIn("WO-RPT-100", json.dumps(inventory))
        self.assertNotIn("work_order_id", json.dumps(inventory))

        purchasing = self._get_report("purchasing_manager")
        self.assertEqual(
            set(purchasing["summary"]),
            {"open_purchase_orders", "open_purchase_order_value", "posted_receipts"},
        )
        self.assertEqual(Decimal(purchasing["summary"]["open_purchase_order_value"]), Decimal("24"))
        self.assertNotIn("work_order", json.dumps(purchasing))
        self.assertNotIn("labor", json.dumps(purchasing))

        integration = self._get_report("integration_admin")
        self.assertEqual(
            set(integration["summary"]),
            {"active_devices", "accepted_messages", "quarantined_messages"},
        )
        self.assertEqual(integration["summary"]["accepted_messages"], 1)
        self.assertNotIn("work_order", json.dumps(integration))
        self.assertNotIn("DEVICE-TOKEN-HASH-MUST-NOT-EXPORT", json.dumps(integration))

    def test_full_export_is_complete_tenant_scoped_and_credential_safe(self) -> None:
        client = APIClient()
        client.force_authenticate(self.users["fleet_manager"])
        response = client.get("/api/v1/export/")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()

        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["scope"]["organization_id"], str(self.org.pk))
        self.assertEqual(payload["scope"]["permissions"], ["export.all", "financial.export"])
        self.assertTrue(EXPORTED_MODEL_KEYS.issubset(payload))
        fleet_role = next(row for row in payload["roles"] if row["slug"] == "fleet_manager")
        self.assertIn("export.all", fleet_role["permissions"])
        self.assertEqual(payload["work_orders"][0]["request_id"], str(self.work_order.request_id))
        self.assertEqual(
            payload["work_order_close_snapshots"][0]["work_order_id"],
            str(self.work_order.pk),
        )
        self.assertEqual(
            {row["transaction_type"] for row in payload["stock_transactions"]},
            {"RECEIPT", "ISSUE", "RETURN", "REVERSAL"},
        )
        self.assertEqual(
            payload["purchase_order_lines"][0]["purchase_request_id"],
            str(payload["purchase_requests"][0]["id"]),
        )
        self.assertEqual(payload["telematics_messages"][0]["raw_payload"]["unit"], "mi")
        self.assertEqual(
            payload["normalized_telematics_events"][0]["message_id"],
            payload["telematics_messages"][0]["id"],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("PRIVATE-EXPORT-ROW", serialized)
        self.assertNotIn("private-manager", serialized)
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("mfa_secret", serialized)
        self.assertNotIn("MFA-MUST-NOT-EXPORT", serialized)
        self.assertNotIn("token_hash", serialized)
        self.assertNotIn("DEVICE-TOKEN-HASH-MUST-NOT-EXPORT", serialized)
        self.assertNotIn("signing_secret", serialized)
        self.assertNotIn("idempotency", serialized.lower())

    def test_export_all_permission_is_required_for_every_report_only_role(self) -> None:
        for role in (
            "management",
            "parts_clerk",
            "purchasing_manager",
            "integration_admin",
            "driver",
        ):
            with self.subTest(role=role):
                client = APIClient()
                client.force_authenticate(self.users[role])
                self.assertEqual(client.get("/api/v1/export/").status_code, 403)
