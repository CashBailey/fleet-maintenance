from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

from assets.models import Asset, AssetType
from core.models import (
    ApiToken,
    AuditEvent,
    IdempotencyRecord,
    Location,
    Organization,
    Role,
    SyncConflict,
    User,
)
from django.test import TestCase
from django.utils import timezone
from maintenance.models import (
    Inspection,
    InspectionResponse,
    InspectionTemplate,
    LaborEntry,
    ServicePackage,
    WorkOrder,
    WorkOrderCloseSnapshot,
    WorkOrderTask,
)
from maintenance.services import _offline_fingerprint, sync_field_operation
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorPart,
)
from rest_framework.test import APIClient

from .models import (
    Bin,
    InventoryCount,
    InventoryCountLine,
    Part,
    StockTransaction,
    Warehouse,
)


class FinancialAccessApiTests(TestCase):
    """Regression coverage for financial data at the public REST boundary."""

    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Finance Fleet", slug="finance-fleet")
        self.location = Location.objects.create(
            organization=self.organization,
            name="Main Shop",
            code="MAIN",
        )
        self.roles = {
            slug: Role.objects.create(
                organization=self.organization,
                slug=slug,
                name=slug.replace("_", " ").title(),
            )
            for slug in (
                "technician",
                "supervisor",
                "parts_clerk",
                "purchasing_manager",
                "fleet_manager",
                "system_admin",
            )
        }
        self.technician = self._user("finance-tech", "technician")
        self.supervisor = self._user("finance-supervisor", "supervisor")
        self.parts_clerk = self._user("finance-parts", "parts_clerk")
        self.purchasing_manager = self._user("finance-purchasing", "purchasing_manager")
        self.fleet_manager = self._user("finance-manager", "fleet_manager")
        self.system_admin = self._user("finance-admin", "system_admin")

        asset_type = AssetType.objects.create(
            organization=self.organization,
            name="Truck",
            category="vehicle",
        )
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            home_location=self.location,
            unit_number="FIN-101",
        )
        self.work_order = WorkOrder.objects.create(
            organization=self.organization,
            number="WO-FIN-101",
            asset=self.asset,
            created_by=self.fleet_manager,
            assigned_to=self.technician,
            summary="Replace oil filter",
        )
        self.task = WorkOrderTask.objects.create(
            organization=self.organization,
            work_order=self.work_order,
            title="Torque oil filter housing",
            sequence=1,
            measurement={"torque_ft_lb": "125", "unit_cost": "12.5000"},
        )
        self.inspection_template = InspectionTemplate.objects.create(
            organization=self.organization,
            created_by=self.fleet_manager,
            name="Finance legacy inspection",
            questions=[
                {
                    "id": "filter",
                    "label": "Inspect oil filter",
                    "unit_cost": "12.5000",
                }
            ],
        )
        self.inspection = Inspection.objects.create(
            organization=self.organization,
            asset=self.asset,
            template=self.inspection_template,
            template_snapshot={
                "name": self.inspection_template.name,
                "version": self.inspection_template.version,
                "questions": self.inspection_template.questions,
            },
            performed_by=self.technician,
            started_at=timezone.now(),
        )
        InspectionResponse.objects.create(
            organization=self.organization,
            inspection=self.inspection,
            question_id="filter",
            question="Inspect oil filter",
            response_type="pass_fail",
            answer={"observed": "normal", "unit_cost": "12.5000"},
            result="pass",
        )
        self.inspection.status = "Submitted"
        self.inspection.submitted_at = timezone.now()
        self.inspection.save(update_fields=["status", "submitted_at", "updated_at"])
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="MAIN",
            name="Main warehouse",
        )
        self.bin = Bin.objects.create(
            organization=self.organization,
            warehouse=self.warehouse,
            code="A-01",
            name="Oil filters",
        )
        self.part = Part.objects.create(
            organization=self.organization,
            number="FILTER-FIN",
            name="Oil filter",
            default_unit_cost=Decimal("12.5000"),
        )
        self.transaction = StockTransaction.objects.create(
            organization=self.organization,
            actor=self.technician,
            part=self.part,
            bin=self.bin,
            transaction_type=StockTransaction.Type.ISSUE,
            quantity=Decimal("-1.000"),
            unit_cost=Decimal("12.5000"),
            total_cost=Decimal("12.5000"),
            work_order=self.work_order,
            operation_id=uuid.uuid4(),
        )
        self.labor = LaborEntry.objects.create(
            organization=self.organization,
            work_order=self.work_order,
            technician=self.technician,
            minutes=30,
            hourly_rate=Decimal("75.00"),
            note="Installed replacement filter",
        )
        WorkOrderCloseSnapshot.objects.create(
            organization=self.organization,
            work_order=self.work_order,
            sequence=1,
            closed_by=self.fleet_manager,
            closed_at=timezone.now(),
            snapshot={
                "labor_entries": [{"hourly_rate": "75.0000", "cost": "37.5000"}],
                "stock_transactions": [{"unit_cost": "12.5000", "total_cost": "12.5000"}],
            },
        )
        self.service_package = ServicePackage.objects.create(
            organization=self.organization,
            created_by=self.fleet_manager,
            name="Finance PM package",
            version=1,
            tasks=[{"title": "Replace filter", "required": True, "sequence": 1}],
            expected_parts=[{"part_number": "FILTER-FIN", "quantity": "1", "unit_cost": "12.5000"}],
        )
        self.work_order.service_package = self.service_package
        self.work_order.service_package_snapshot = {
            "id": str(self.service_package.pk),
            "name": self.service_package.name,
            "version": self.service_package.version,
            "expected_parts": self.service_package.expected_parts,
        }
        self.work_order.save(
            update_fields=["service_package", "service_package_snapshot", "updated_at"]
        )
        self.inventory_count = InventoryCount.objects.create(
            organization=self.organization,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            total_variance_value=Decimal("12.5000"),
            approval_threshold=Decimal("5.00"),
            approval_required=True,
            created_by=self.parts_clerk,
        )
        InventoryCountLine.objects.create(
            organization=self.organization,
            inventory_count=self.inventory_count,
            part=self.part,
            bin=self.bin,
            expected_quantity=Decimal("1.000"),
            counted_quantity=Decimal("0.000"),
            variance=Decimal("-1.000"),
            unit_cost_snapshot=Decimal("12.5000"),
        )
        self.vendor = Vendor.objects.create(
            organization=self.organization,
            code="FIN-SUPPLY",
            name="Finance Supply",
            payment_terms="Net 30",
        )
        VendorPart.objects.create(
            organization=self.organization,
            vendor=self.vendor,
            part=self.part,
            vendor_part_number="FIN-FILTER",
            unit_cost=Decimal("11.7500"),
        )
        self.purchase_order = PurchaseOrder.objects.create(
            organization=self.organization,
            number="PO-FIN-101",
            vendor=self.vendor,
            created_by=self.purchasing_manager,
            approval_threshold=Decimal("50.00"),
        )
        self.purchase_order_line = PurchaseOrderLine.objects.create(
            organization=self.organization,
            purchase_order=self.purchase_order,
            part=self.part,
            description="Oil filter",
            quantity_ordered=Decimal("2.000"),
            unit_cost=Decimal("11.7500"),
        )
        self.receipt = Receipt.objects.create(
            organization=self.organization,
            purchase_order=self.purchase_order,
            number="RCPT-FIN-101",
            operation_id=uuid.uuid4(),
            received_by=self.parts_clerk,
        )
        ReceiptLine.objects.create(
            organization=self.organization,
            receipt=self.receipt,
            purchase_order_line=self.purchase_order_line,
            part=self.part,
            bin=self.bin,
            quantity=Decimal("1.000"),
            unit_cost=Decimal("11.7500"),
        )
        AuditEvent.objects.create(
            organization=self.organization,
            actor=self.purchasing_manager,
            action="vendor_part.price_updated",
            resource_type="VendorPart",
            resource_id="finance-vendor-part",
            context={"previous_unit_cost": "10.0000", "unit_cost": "11.7500"},
        )

    def _user(self, username: str, role: str) -> User:
        user = User.objects.create_user(username=username, organization=self.organization)
        user.roles.add(self.roles[role])
        return user

    @staticmethod
    def _assert_no_financial_fields(payload: object) -> None:
        forbidden = {
            "approval_threshold",
            "amount",
            "contribution",
            "cost",
            "current_labor_cost",
            "default_unit_cost",
            "hourly_rate",
            "line_total",
            "line_amount",
            "open_purchase_order_value",
            "part_cost",
            "payment_terms",
            "previous_unit_cost",
            "total",
            "total_cost",
            "total_variance_value",
            "unit_cost",
            "unit_cost_snapshot",
            "unit_price",
            "terms_snapshot",
            "estimated_cost",
            "expected_cost",
            "vendor_cost",
            "total_value",
            "inventory_value",
            "markup",
            "discount",
            "rate",
            "rate_per_hour",
        }

        def visit(value: object) -> None:
            if isinstance(value, dict):
                assert not (set(value) & forbidden), value
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)

    def _client_for(self, user: User, token: ApiToken | None = None) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user, token=token)
        return client

    def test_operational_roles_receive_locations_and_quantities_without_prices(self) -> None:
        technician = self._client_for(self.technician)
        parts = technician.get("/api/v1/inventory/parts/")
        history = technician.get(f"/api/v1/inventory/parts/{self.part.pk}/history/")
        bin_history = technician.get(f"/api/v1/inventory/bins/{self.bin.pk}/history/")
        work_order = technician.get(f"/api/v1/maintenance/work-orders/{self.work_order.pk}/")
        counts = technician.get("/api/v1/inventory/counts/")
        bootstrap = technician.get("/api/v1/bootstrap/")
        templates = technician.get("/api/v1/maintenance/inspection-templates/")
        inspection = technician.get(f"/api/v1/maintenance/inspections/{self.inspection.pk}/")
        task = technician.get(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{self.task.pk}/"
        )

        for response in (
            parts,
            history,
            bin_history,
            work_order,
            counts,
            bootstrap,
            templates,
            inspection,
            task,
        ):
            self.assertEqual(response.status_code, 200, response.content)
            self._assert_no_financial_fields(response.json())

        part_payload = parts.json()["parts"][0]
        self.assertEqual(part_payload["number"], self.part.number)
        self.assertEqual(history.json()["transactions"][0]["quantity"], "-1.000")
        self.assertEqual(bin_history.json()["transactions"][0]["bin_code"], "MAIN/A-01")
        work_payload = work_order.json()["work_order"]
        self.assertEqual(work_payload["labor_entries"][0]["minutes"], 30)
        self.assertEqual(work_payload["stock_transactions"][0]["part_number"], self.part.number)
        self.assertEqual(task.json()["task"]["measurement"]["torque_ft_lb"], "125")
        self.assertEqual(
            inspection.json()["inspection"]["responses"][0]["answer"]["observed"], "normal"
        )

    def test_purchasing_and_audit_views_redact_prices_for_nonfinancial_roles(self) -> None:
        parts_clerk = self._client_for(self.parts_clerk)
        purchase_orders = parts_clerk.get("/api/v1/purchasing/purchase-orders/")
        receipts = parts_clerk.get("/api/v1/purchasing/receipts/")
        vendors = parts_clerk.get("/api/v1/purchasing/vendors/")
        audit = self._client_for(self.supervisor).get("/api/v1/audit-events/")

        for response in (purchase_orders, receipts, vendors, audit):
            self.assertEqual(response.status_code, 200, response.content)
            self._assert_no_financial_fields(response.json())

        self.assertEqual(
            purchase_orders.json()["purchase_orders"][0]["lines"][0]["quantity_remaining"],
            "2.000",
        )
        self.assertEqual(receipts.json()["receipts"][0]["lines"][0]["bin_code"], "A-01")
        self.assertNotIn("payment_terms", vendors.json()["vendors"][0])

    def test_pm_expected_parts_are_operational_only_and_snapshots_are_redacted(self) -> None:
        technician = self._client_for(self.technician)
        packages = technician.get("/api/v1/maintenance/service-packages/")
        work_order = technician.get(f"/api/v1/maintenance/work-orders/{self.work_order.pk}/")
        for response in (packages, work_order):
            self.assertEqual(response.status_code, 200, response.content)
            self._assert_no_financial_fields(response.json())

        package = next(
            row
            for row in packages.json()["service_packages"]
            if row["id"] == str(self.service_package.pk)
        )
        self.assertEqual(package["expected_parts"][0]["part_number"], self.part.number)
        self.assertEqual(
            work_order.json()["work_order"]["service_package_snapshot"]["expected_parts"][0][
                "quantity"
            ],
            "1",
        )

        manager = self._client_for(self.fleet_manager)
        manager_package = manager.get("/api/v1/maintenance/service-packages/")
        manager_work_order = manager.get(f"/api/v1/maintenance/work-orders/{self.work_order.pk}/")
        self.assertEqual((manager_package.status_code, manager_work_order.status_code), (200, 200))
        finance_package = next(
            row
            for row in manager_package.json()["service_packages"]
            if row["id"] == str(self.service_package.pk)
        )
        self.assertEqual(finance_package["expected_parts"][0]["unit_cost"], "12.5000")
        self.assertEqual(
            manager_work_order.json()["work_order"]["service_package_snapshot"]["expected_parts"][
                0
            ]["unit_cost"],
            "12.5000",
        )

    def test_financial_roles_retain_cost_visibility_and_export_access(self) -> None:
        manager = self._client_for(self.fleet_manager)
        parts = manager.get("/api/v1/inventory/parts/")
        work_order = manager.get(f"/api/v1/maintenance/work-orders/{self.work_order.pk}/")
        purchase_orders = manager.get("/api/v1/purchasing/purchase-orders/")
        audit = manager.get("/api/v1/audit-events/")
        templates = manager.get("/api/v1/maintenance/inspection-templates/")
        inspection = manager.get(f"/api/v1/maintenance/inspections/{self.inspection.pk}/")
        task = manager.get(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{self.task.pk}/"
        )

        for response in (parts, work_order, purchase_orders, audit, templates, inspection, task):
            self.assertEqual(response.status_code, 200, response.content)

        self.assertEqual(parts.json()["parts"][0]["default_unit_cost"], "12.5000")
        bootstrap = manager.get("/api/v1/bootstrap/")
        self.assertEqual(bootstrap.status_code, 200, bootstrap.content)
        self.assertEqual(bootstrap.json()["parts"][0]["default_unit_cost"], "12.5000")
        self.assertEqual(work_order.json()["work_order"]["part_cost"], "12.5000")
        self.assertEqual(
            purchase_orders.json()["purchase_orders"][0]["lines"][0]["unit_cost"],
            "11.7500",
        )
        event = next(
            row for row in audit.json()["events"] if row["action"] == "vendor_part.price_updated"
        )
        self.assertEqual(event["context"]["previous_unit_cost"], "10.0000")
        self.assertEqual(
            templates.json()["inspection_templates"][0]["questions"][0]["unit_cost"],
            "12.5000",
        )
        self.assertEqual(
            inspection.json()["inspection"]["responses"][0]["answer"]["unit_cost"], "12.5000"
        )
        self.assertEqual(task.json()["task"]["measurement"]["unit_cost"], "12.5000")

        self.assertEqual(manager.get("/api/v1/export/").status_code, 200)
        system_export = self._client_for(self.system_admin).get("/api/v1/export/")
        self.assertEqual(system_export.status_code, 403)

    def test_idempotent_receipt_replay_redacts_a_prior_financial_response(self) -> None:
        """A narrower token cannot recover a same-user cached finance response."""

        self.purchase_order.status = PurchaseOrder.Status.SENT
        self.purchase_order.save(update_fields=["status", "updated_at"])
        operation_id = uuid.uuid4()
        payload = {
            "purchase_order_id": str(self.purchase_order.pk),
            "lines": [
                {
                    "purchase_order_line_id": str(self.purchase_order_line.pk),
                    "bin_id": str(self.bin.pk),
                    "quantity": "1.000",
                }
            ],
        }
        first = self._client_for(self.purchasing_manager).post(
            "/api/v1/purchasing/receipts/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["receipt"]["lines"][0]["unit_cost"], "11.7500")

        receive_only_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.purchasing_manager,
            name="Receive only",
            prefix="receipt-fin",
            token_hash=hashlib.sha256(b"receipt-token").hexdigest(),
            scopes=["purchasing.receive"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        replay = self._client_for(self.purchasing_manager, receive_only_token).post(
            "/api/v1/purchasing/receipts/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        self.assertEqual(replay.status_code, 201, replay.content)
        self._assert_no_financial_fields(replay.json())
        self.assertEqual(replay.json()["receipt"]["lines"][0]["quantity"], "1.000")
        self.assertEqual(Receipt.objects.filter(operation_id=operation_id).count(), 1)

    def test_bootstrap_redacts_stored_offline_conflict_for_a_scoped_token(self) -> None:
        """A later restricted credential cannot read a finance-era conflict payload."""

        SyncConflict.objects.create(
            organization=self.organization,
            user=self.fleet_manager,
            operation_id=uuid.uuid4(),
            operation_type="stock.issue",
            message="Stock changed while this device was offline",
            client_payload={
                "type": "stock.issue",
                "payload": {"part_number": self.part.number, "quantity": "1", "unit_cost": "12.50"},
            },
            server_payload={
                "bin_code": "MAIN/A-01",
                "quantity_available": "0",
                "total_cost": "12.50",
            },
        )
        finance_bootstrap = self._client_for(self.fleet_manager).get("/api/v1/bootstrap/")
        self.assertEqual(finance_bootstrap.status_code, 200, finance_bootstrap.content)
        self.assertEqual(
            finance_bootstrap.json()["sync_conflicts"][0]["client_payload"]["payload"]["unit_cost"],
            "12.50",
        )

        dashboard_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.fleet_manager,
            name="Dashboard only",
            prefix="conflict-fin",
            token_hash=hashlib.sha256(b"conflict-token").hexdigest(),
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        restricted_bootstrap = self._client_for(self.fleet_manager, dashboard_token).get(
            "/api/v1/bootstrap/"
        )
        self.assertEqual(restricted_bootstrap.status_code, 200, restricted_bootstrap.content)
        conflict = restricted_bootstrap.json()["sync_conflicts"][0]
        self._assert_no_financial_fields(conflict)
        self.assertEqual(conflict["client_payload"]["payload"]["part_number"], self.part.number)
        self.assertEqual(conflict["server_payload"]["bin_code"], "MAIN/A-01")

    def test_structured_maintenance_payloads_redact_and_replay_by_current_scope(self) -> None:
        """Legacy opaque JSON and offline replay obey the credential used to read it."""

        self.technician.roles.add(self.roles["purchasing_manager"])
        finance_task = self._client_for(self.technician).get(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{self.task.pk}/"
        )
        self.assertEqual(finance_task.status_code, 200, finance_task.content)
        self.assertEqual(finance_task.json()["task"]["measurement"]["unit_cost"], "12.5000")

        execution_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.technician,
            name="Execution only",
            prefix="task-fin",
            token_hash=hashlib.sha256(b"task-finance-token").hexdigest(),
            scopes=["maintenance.execute"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        restricted_task = self._client_for(self.technician, execution_token).get(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{self.task.pk}/"
        )
        self.assertEqual(restricted_task.status_code, 200, restricted_task.content)
        self._assert_no_financial_fields(restricted_task.json())
        self.assertEqual(restricted_task.json()["task"]["measurement"]["torque_ft_lb"], "125")

        operation_id = str(uuid.uuid4())
        operation: dict[str, object] = {
            "operation_id": operation_id,
            "type": "task.complete",
            "payload": {"task_id": str(self.task.pk)},
        }
        IdempotencyRecord.objects.create(
            organization=self.organization,
            user=self.technician,
            route="offline:task.complete",
            key=operation_id,
            request_fingerprint=_offline_fingerprint(operation),
            state="complete",
            response_status=200,
            response_body={
                "id": str(self.task.pk),
                "measurement": {"torque_ft_lb": "125", "unit_cost": "12.5000"},
            },
        )
        finance_replay = sync_field_operation(self.technician, operation)
        self.assertEqual(finance_replay["measurement"]["unit_cost"], "12.5000")
        restricted_replay = sync_field_operation(
            self.technician,
            operation,
            auth=execution_token,
        )
        self._assert_no_financial_fields(restricted_replay)
        self.assertEqual(restricted_replay["measurement"]["torque_ft_lb"], "125")

    def test_cost_bearing_mutations_require_financial_manage_with_operation_scope(self) -> None:
        parts_clerk = self._client_for(self.parts_clerk)
        created = parts_clerk.post(
            "/api/v1/inventory/parts/",
            {"number": "NO-COST", "name": "Denied cost", "default_unit_cost": "1.25"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(created.status_code, 403)
        self.assertEqual(created.json()["error"]["code"], "financial_permission_denied")
        self.assertFalse(Part.objects.filter(number="NO-COST").exists())

        labor = self._client_for(self.technician).post(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/labor/",
            {"minutes": 15, "hourly_rate": "99.00", "note": "Client-supplied rate"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(labor.status_code, 403)
        self.assertEqual(labor.json()["error"]["code"], "financial_permission_denied")

        labor_correction = self._client_for(self.supervisor).post(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/labor/",
            {
                "minutes": 20,
                "note": "Corrected duration",
                "corrects_id": str(self.labor.pk),
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(labor_correction.status_code, 403)
        self.assertEqual(labor_correction.json()["error"]["code"], "financial_permission_denied")

        scoped_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.purchasing_manager,
            name="Purchasing only",
            prefix="fin-test",
            token_hash=hashlib.sha256(b"finance-token").hexdigest(),
            scopes=["purchasing.manage"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        scoped_manager = self._client_for(self.purchasing_manager, scoped_token)
        purchase_order = scoped_manager.post(
            "/api/v1/purchasing/purchase-orders/",
            {
                "vendor_id": str(self.vendor.pk),
                "number": "PO-DENIED-FINANCE",
                "lines": [
                    {
                        "part_id": str(self.part.pk),
                        "quantity_ordered": "1",
                        "unit_cost": "11.75",
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(purchase_order.status_code, 403)
        self.assertEqual(purchase_order.json()["error"]["code"], "financial_permission_denied")
        self.assertFalse(PurchaseOrder.objects.filter(number="PO-DENIED-FINANCE").exists())

        vendor_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.purchasing_manager,
            name="Vendor only",
            prefix="vendor-fin",
            token_hash=hashlib.sha256(b"vendor-token").hexdigest(),
            scopes=["vendors.manage"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        vendor = self._client_for(self.purchasing_manager, vendor_token).post(
            "/api/v1/purchasing/vendors/",
            {"code": "TERMS-DENIED", "name": "Terms denied", "payment_terms": "Net 15"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(vendor.status_code, 403)
        self.assertEqual(vendor.json()["error"]["code"], "financial_permission_denied")
        self.assertFalse(Vendor.objects.filter(code="TERMS-DENIED").exists())

        invalid_package = self._client_for(self.supervisor).post(
            "/api/v1/maintenance/service-packages/",
            {
                "name": "Price injection package",
                "tasks": [{"title": "Inspect filter"}],
                "expected_parts": [
                    {"part_number": "FILTER-FIN", "quantity": "1", "unit_price": "99.99"}
                ],
            },
            format="json",
        )
        self.assertEqual(invalid_package.status_code, 400)
        self.assertEqual(invalid_package.json()["error"]["code"], "invalid_expected_part")
        self.assertFalse(ServicePackage.objects.filter(name="Price injection package").exists())

        task_measurement = self._client_for(self.technician).patch(
            f"/api/v1/maintenance/work-orders/{self.work_order.pk}/tasks/{self.task.pk}/",
            {"measurement": {"unit_cost": "99.99"}},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(task_measurement.status_code, 400)
        self.assertEqual(task_measurement.json()["error"]["code"], "financial_payload_not_allowed")

        template = self._client_for(self.supervisor).post(
            "/api/v1/maintenance/inspection-templates/",
            {
                "name": "Cost injection inspection",
                "questions": [{"id": "cost", "label": "Cost", "unit_cost": "99.99"}],
            },
            format="json",
        )
        self.assertEqual(template.status_code, 400)
        self.assertEqual(template.json()["error"]["code"], "financial_payload_not_allowed")

        inspection = self._client_for(self.technician).post(
            "/api/v1/maintenance/inspections/",
            {
                "asset_id": str(self.asset.pk),
                "template_id": str(self.inspection_template.pk),
                "responses": [
                    {
                        "question_id": "filter",
                        "result": "pass",
                        "answer": {"unit_cost": "99.99"},
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(inspection.status_code, 400)
        self.assertEqual(inspection.json()["error"]["code"], "financial_payload_not_allowed")
