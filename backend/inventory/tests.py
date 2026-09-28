from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from typing import Any

from assets.models import Asset, AssetType
from core.exceptions import DomainError
from core.models import ApiToken, AuditEvent, Location, Organization, Role, User
from core.offline_access import issue_offline_access_grant
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from maintenance.models import WorkOrder
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Receipt,
    Vendor,
    VendorPart,
)
from purchasing.services import post_receipt
from rest_framework.test import APIClient

from .models import (
    Bin,
    InventoryCount,
    Part,
    PartCrossReference,
    Reservation,
    StockBalance,
    StockTransaction,
    Warehouse,
)
from .services import (
    adjust_stock,
    approve_inventory_count,
    issue_stock,
    post_inventory_count,
    receive_stock,
    reconcile_inventory,
    release_reservation,
    reserve_stock,
    return_stock,
    reverse_transaction,
)


class InventoryLedgerTests(TransactionTestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Test Fleet", slug="test-fleet")
        self.user = User.objects.create_user(
            username="parts-test", password=None, organization=self.organization
        )
        role = Role.objects.create(
            organization=self.organization, slug="parts_clerk", name="Parts clerk"
        )
        self.user.roles.add(role)
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.user)
        self.api_client.credentials(
            HTTP_X_OFFLINE_GRANT=issue_offline_access_grant(self.user).token
        )
        self.location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="WH1",
            name="Main warehouse",
        )
        self.bin = Bin.objects.create(
            organization=self.organization, warehouse=self.warehouse, code="A-01"
        )
        self.part = Part.objects.create(
            organization=self.organization,
            number="FLT-100",
            name="Oil filter",
            default_unit_cost=Decimal("10.0000"),
        )
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            home_location=self.location,
            unit_number="T-01",
        )
        self.work_order = WorkOrder.objects.create(
            organization=self.organization,
            asset=asset,
            number="WO-TEST-1",
            summary="Test work",
            created_by=self.user,
        )

    def receive(self, quantity: str = "5") -> StockTransaction:
        return receive_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            quantity=quantity,
            unit_cost="10",
            operation_id=uuid.uuid4(),
            reference_type="test",
        )

    def test_reserve_issue_return_reconciles_and_preserves_cost(self) -> None:
        self.receive()
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="4",
            operation_id=uuid.uuid4(),
        )
        with self.assertRaisesMessage(DomainError, "Insufficient available stock"):
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="2",
                operation_id=uuid.uuid4(),
            )
        issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="3",
            operation_id=uuid.uuid4(),
            reservation=reservation,
        )
        returned = return_stock(
            organization=self.organization,
            actor=self.user,
            original=issue,
            quantity="1",
            operation_id=uuid.uuid4(),
            reason="Unused",
        )
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(balance.quantity_on_hand, Decimal("3"))
        self.assertEqual(balance.quantity_reserved, Decimal("1"))
        self.assertEqual(returned.original_transaction, issue)
        cost = StockTransaction.objects.filter(work_order=self.work_order).aggregate(
            total=Sum("total_cost")
        )["total"]
        self.assertEqual(cost, Decimal("20"))
        self.assertEqual(reconcile_inventory(self.organization), [])
        with self.assertRaises(ValidationError):
            issue.reason = "rewritten"
            issue.save()

    def test_duplicate_operation_posts_once(self) -> None:
        self.receive("2")
        operation_id = uuid.uuid4()
        first = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=operation_id,
        )
        second = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=operation_id,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StockTransaction.objects.filter(operation_id=operation_id).count(), 1)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("1"),
        )

    def test_part_create_rolls_back_cross_references_and_replays_once(self) -> None:
        PartCrossReference.objects.create(
            organization=self.organization,
            part=self.part,
            value="EXISTING-ALT",
        )
        failed = self.api_client.post(
            "/api/v1/inventory/parts/",
            {
                "number": "FLT-200",
                "name": "Fuel filter",
                "cross_references": ["NEW-ALT", "EXISTING-ALT"],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(failed.status_code, 409)
        self.assertFalse(Part.objects.filter(number="FLT-200").exists())
        self.assertFalse(PartCrossReference.objects.filter(value_normalized="NEW-ALT").exists())

        operation_id = uuid.uuid4()
        payload = {
            "number": "FLT-200",
            "name": "Fuel filter",
            "cross_references": ["NEW-ALT"],
        }
        created = self.api_client.post(
            "/api/v1/inventory/parts/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        replayed = self.api_client.post(
            "/api/v1/inventory/parts/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(replayed.status_code, 201)
        self.assertEqual(created.data, replayed.data)
        self.assertEqual(Part.objects.filter(number="FLT-200").count(), 1)
        self.assertEqual(PartCrossReference.objects.filter(value_normalized="NEW-ALT").count(), 1)

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="1000.00")
    def test_count_posts_variance_as_a_ledger_entry(self) -> None:
        self.receive("5")
        count = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "7"}],
        )
        line = count.lines.get()
        self.assertEqual(line.variance, Decimal("2"))
        if line.adjustment_transaction is None:
            self.fail("Variance must create an adjustment transaction")
        self.assertEqual(line.adjustment_transaction.quantity, Decimal("2"))
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("7"),
        )

    def test_issue_cost_is_server_derived_and_override_is_privileged_and_audited(self) -> None:
        self.receive("5")

        with self.assertRaises(DomainError) as denied:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=uuid.uuid4(),
                unit_cost="0.01",
                reason="Attempted client cost",
            )
        self.assertEqual(denied.exception.code, "cost_override_permission_denied")

        derived = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
        )
        self.assertEqual(derived.unit_cost, Decimal("10.0000"))
        self.assertEqual(derived.total_cost, Decimal("10.0000"))

        adjust_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(adjust_role)
        with self.assertRaises(DomainError) as missing_reason:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=uuid.uuid4(),
                unit_cost="2.50",
            )
        self.assertEqual(missing_reason.exception.code, "cost_override_reason_required")

        overridden = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
            unit_cost="2.50",
            reason="Supplier credit replacement",
        )
        self.assertEqual(overridden.unit_cost, Decimal("2.5000"))
        event = AuditEvent.objects.get(
            organization=self.organization,
            action="stock.issued",
            resource_id=str(overridden.pk),
        )
        self.assertTrue(event.context["unit_cost_override"])
        self.assertEqual(event.context["override_reason"], "Supplier credit replacement")
        self.assertEqual(event.context["default_unit_cost"], "10.0000")

        raw_token = "issue-only-cost-token"  # noqa: S105
        ApiToken.objects.create(
            organization=self.organization,
            user=self.user,
            name="Issue only",
            prefix="issue-cost",
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            scopes=["inventory.issue"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        response = bearer.post(
            "/api/v1/inventory/issues/",
            {
                "part_id": str(self.part.pk),
                "bin_id": str(self.bin.pk),
                "work_order_id": str(self.work_order.pk),
                "quantity": "1",
                "unit_cost": "1.00",
                "reason": "Token scope bypass attempt",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "cost_override_permission_denied")

    def test_stock_replay_fingerprint_includes_cost_and_reason(self) -> None:
        self.receive("4")
        adjust_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(adjust_role)
        operation_id = uuid.uuid4()
        first = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=operation_id,
            unit_cost="8.50",
            reason="Approved cost correction",
        )
        exact_replay = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=operation_id,
            unit_cost="8.50",
            reason="Approved cost correction",
        )
        self.assertEqual(exact_replay.pk, first.pk)

        with self.assertRaises(DomainError) as changed_reason:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=operation_id,
                unit_cost="8.50",
                reason="Different explanation",
            )
        self.assertEqual(changed_reason.exception.code, "idempotency_conflict")
        with self.assertRaises(DomainError) as changed_cost:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=operation_id,
                unit_cost="9.50",
                reason="Approved cost correction",
            )
        self.assertEqual(changed_cost.exception.code, "idempotency_conflict")
        with self.assertRaises(DomainError) as oversized_reason:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=uuid.uuid4(),
                reason="x" * 501,
            )
        self.assertEqual(oversized_reason.exception.code, "invalid_reason")
        self.assertEqual(StockTransaction.objects.filter(operation_id=operation_id).count(), 1)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("3"),
        )

    def test_offline_replay_rejects_mutated_reason(self) -> None:
        self.receive("3")
        operation_id = uuid.uuid4()
        operation: dict[str, Any] = {
            "operation_id": str(operation_id),
            "type": "stock.issue",
            "payload": {
                "part_id": str(self.part.pk),
                "bin_id": str(self.bin.pk),
                "work_order_id": str(self.work_order.pk),
                "quantity": "1",
                "reason": "Original offline reason",
            },
        }
        first = self.api_client.post(
            "/api/v1/offline/sync/", {"operations": [operation]}, format="json"
        )
        replay = self.api_client.post(
            "/api/v1/offline/sync/", {"operations": [operation]}, format="json"
        )
        altered = {
            **operation,
            "payload": {**operation["payload"], "reason": "Altered offline reason"},
        }
        conflict = self.api_client.post(
            "/api/v1/offline/sync/", {"operations": [altered]}, format="json"
        )
        self.assertEqual(first.data["results"][0]["status"], "synced")
        self.assertEqual(replay.data["results"][0]["status"], "synced")
        self.assertEqual(conflict.data["results"][0]["status"], "conflict")
        self.assertEqual(StockTransaction.objects.filter(operation_id=operation_id).count(), 1)

    def test_terminal_work_order_allows_compensation_but_blocks_new_commitments(self) -> None:
        self.receive("4")
        returned_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
        )
        reversed_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
        )
        WorkOrder.objects.filter(pk=self.work_order.pk).update(status="Cancelled")

        returned = return_stock(
            organization=self.organization,
            actor=self.user,
            original=returned_issue,
            quantity="1",
            operation_id=uuid.uuid4(),
            reason="Cancelled work did not use part",
        )
        manager = self.make_approver("terminal-reversal-manager")
        reversed_row = reverse_transaction(
            organization=self.organization,
            actor=manager,
            original=reversed_issue,
            operation_id=uuid.uuid4(),
            reason="Cancelled work issue was erroneous",
        )

        with self.assertRaises(DomainError) as issue_denied:
            issue_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=uuid.uuid4(),
            )
        self.assertEqual(issue_denied.exception.code, "work_order_not_open")
        with self.assertRaises(DomainError) as reserve_denied:
            reserve_stock(
                organization=self.organization,
                actor=self.user,
                part=self.part,
                bin=self.bin,
                work_order=self.work_order,
                quantity="1",
                operation_id=uuid.uuid4(),
            )
        self.assertEqual(reserve_denied.exception.code, "work_order_not_open")
        self.assertTrue(StockTransaction.objects.filter(pk=returned_issue.pk).exists())
        self.assertTrue(StockTransaction.objects.filter(pk=reversed_issue.pk).exists())
        self.assertEqual(returned.original_transaction, returned_issue)
        self.assertEqual(reversed_row.original_transaction, reversed_issue)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("4"),
        )
        self.assertEqual(reconcile_inventory(self.organization), [])

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="1000.00")
    def test_count_replay_fingerprint_includes_actor_warehouse_reason_and_lines(self) -> None:
        operation_id = uuid.uuid4()
        lines = [{"part": self.part, "bin": self.bin, "counted_quantity": "0"}]
        first = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=operation_id,
            reason="Empty bin verified",
            lines=lines,
        )
        replay = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=operation_id,
            reason="Empty bin verified",
            lines=lines,
        )
        self.assertEqual(replay.pk, first.pk)

        role = Role.objects.get(organization=self.organization, slug="parts_clerk")
        other_user = User.objects.create_user(
            username="other-counter",
            password=None,
            organization=self.organization,
        )
        other_user.roles.add(role)
        with self.assertRaises(DomainError) as changed_actor:
            post_inventory_count(
                organization=self.organization,
                actor=other_user,
                warehouse=self.warehouse,
                operation_id=operation_id,
                reason="Empty bin verified",
                lines=lines,
            )
        self.assertEqual(changed_actor.exception.code, "idempotency_conflict")

        with self.assertRaises(DomainError) as changed_reason:
            post_inventory_count(
                organization=self.organization,
                actor=self.user,
                warehouse=self.warehouse,
                operation_id=operation_id,
                reason="Different count reason",
                lines=lines,
            )
        self.assertEqual(changed_reason.exception.code, "idempotency_conflict")
        with self.assertRaises(DomainError) as changed_lines:
            post_inventory_count(
                organization=self.organization,
                actor=self.user,
                warehouse=self.warehouse,
                operation_id=operation_id,
                reason="Empty bin verified",
                lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "1"}],
            )
        self.assertEqual(changed_lines.exception.code, "idempotency_conflict")

        other_warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="WH2",
            name="Other warehouse",
        )
        other_bin = Bin.objects.create(
            organization=self.organization,
            warehouse=other_warehouse,
            code="B-01",
        )
        with self.assertRaises(DomainError) as changed_warehouse:
            post_inventory_count(
                organization=self.organization,
                actor=self.user,
                warehouse=other_warehouse,
                operation_id=operation_id,
                reason="Empty bin verified",
                lines=[{"part": self.part, "bin": other_bin, "counted_quantity": "0"}],
            )
        self.assertEqual(changed_warehouse.exception.code, "idempotency_conflict")
        self.assertEqual(InventoryCount.objects.filter(operation_id=operation_id).count(), 1)

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="1000.00")
    def test_concurrent_cross_warehouse_count_operation_id_has_one_winner(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Organization-wide idempotency serialization requires PostgreSQL")
        other_warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="WH2",
            name="Other warehouse",
        )
        other_bin = Bin.objects.create(
            organization=self.organization,
            warehouse=other_warehouse,
            code="B-01",
        )
        role = Role.objects.get(organization=self.organization, slug="parts_clerk")
        other_user = User.objects.create_user(
            username="concurrent-counter",
            password=None,
            organization=self.organization,
        )
        other_user.roles.add(role)
        operation_id = uuid.uuid4()

        def attempt(user_id: uuid.UUID, warehouse_id: uuid.UUID, bin_id: uuid.UUID) -> str:
            close_old_connections()
            try:
                post_inventory_count(
                    organization=Organization.objects.get(pk=self.organization.pk),
                    actor=User.objects.get(pk=user_id),
                    warehouse=Warehouse.objects.get(pk=warehouse_id),
                    operation_id=operation_id,
                    reason="Concurrent count",
                    lines=[
                        {
                            "part": Part.objects.get(pk=self.part.pk),
                            "bin": Bin.objects.select_related("warehouse").get(pk=bin_id),
                            "counted_quantity": "0",
                        }
                    ],
                )
                return "created"
            except DomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        inputs = [
            (self.user.pk, self.warehouse.pk, self.bin.pk),
            (other_user.pk, other_warehouse.pk, other_bin.pk),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda values: attempt(*values), inputs))
        self.assertCountEqual(results, ["created", "idempotency_conflict"])
        self.assertEqual(InventoryCount.objects.filter(operation_id=operation_id).count(), 1)

    def test_reserved_issue_reversal_repairs_partial_and_released_projection(self) -> None:
        self.receive("10")
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="6",
            operation_id=uuid.uuid4(),
            reason="Staged for planned work",
        )
        first_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
            reservation=reservation,
        )
        second_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
            reservation=reservation,
        )
        manager = self.make_approver("projection-reversal-manager")
        partial_reversal = reverse_transaction(
            organization=self.organization,
            actor=manager,
            original=second_issue,
            operation_id=uuid.uuid4(),
            reason="Second issue posted in error",
        )
        reservation.refresh_from_db()
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(reservation.status, Reservation.Status.PARTIALLY_ISSUED)
        self.assertEqual(reservation.issued_quantity, Decimal("2"))
        self.assertEqual(reservation.released_quantity, Decimal("0"))
        self.assertEqual(balance.quantity_reserved, Decimal("4"))
        self.assertEqual(partial_reversal.reservation, reservation)

        release_reservation(
            organization=self.organization,
            actor=self.user,
            reservation=reservation,
            reason="Work cancelled",
        )
        released_reversal = reverse_transaction(
            organization=self.organization,
            actor=manager,
            original=first_issue,
            operation_id=uuid.uuid4(),
            reason="First issue posted in error",
        )
        reservation.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.RELEASED)
        self.assertEqual(reservation.issued_quantity, Decimal("0"))
        self.assertEqual(reservation.released_quantity, Decimal("6"))
        self.assertEqual(reservation.remaining_quantity, Decimal("0"))
        self.assertEqual(reservation.reason, "Staged for planned work")
        self.assertEqual(balance.quantity_on_hand, Decimal("10"))
        self.assertEqual(balance.quantity_reserved, Decimal("0"))
        self.assertEqual(released_reversal.reservation, reservation)
        self.assertEqual(reconcile_inventory(self.organization), [])

    def test_concurrent_reserved_issue_replay_posts_once(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        self.receive("4")
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        operation_id = uuid.uuid4()

        def attempt() -> str:
            close_old_connections()
            try:
                row = issue_stock(
                    organization=Organization.objects.get(pk=self.organization.pk),
                    actor=User.objects.get(pk=self.user.pk),
                    part=Part.objects.get(pk=self.part.pk),
                    bin=Bin.objects.select_related("warehouse").get(pk=self.bin.pk),
                    work_order=WorkOrder.objects.get(pk=self.work_order.pk),
                    quantity="1",
                    operation_id=operation_id,
                    reservation=Reservation.objects.get(pk=reservation.pk),
                    reason="Same offline mutation",
                )
                return str(row.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(StockTransaction.objects.filter(operation_id=operation_id).count(), 1)
        reservation.refresh_from_db()
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(reservation.status, Reservation.Status.PARTIALLY_ISSUED)
        self.assertEqual(reservation.issued_quantity, Decimal("1"))
        self.assertEqual(reservation.remaining_quantity, Decimal("1"))
        self.assertEqual(balance.quantity_on_hand, Decimal("3"))
        self.assertEqual(balance.quantity_reserved, Decimal("1"))
        self.assertEqual(reconcile_inventory(self.organization), [])

    def test_reserved_issue_reversal_rejects_unsafe_projection_without_writes(self) -> None:
        self.receive("3")
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
        )
        issued = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
            reservation=reservation,
        )
        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.PENDING)
        manager = self.make_approver("unsafe-projection-manager")

        with self.assertRaises(DomainError) as rejected:
            reverse_transaction(
                organization=self.organization,
                actor=manager,
                original=issued,
                operation_id=uuid.uuid4(),
                reason="Projection state is unsafe",
            )
        self.assertEqual(rejected.exception.code, "reservation_projection_conflict")
        reservation.refresh_from_db()
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.issued_quantity, Decimal("1"))
        self.assertEqual(balance.quantity_on_hand, Decimal("2"))
        self.assertEqual(balance.quantity_reserved, Decimal("0"))
        self.assertFalse(
            StockTransaction.objects.filter(
                transaction_type=StockTransaction.Type.REVERSAL,
                original_transaction=issued,
            ).exists()
        )

    def test_concurrent_reserved_issue_reversal_repairs_projection_once(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        self.receive("4")
        reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        issued = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
            reservation=reservation,
        )
        manager = self.make_approver("concurrent-reversal-manager")

        def attempt() -> str:
            close_old_connections()
            try:
                reverse_transaction(
                    organization=Organization.objects.get(pk=self.organization.pk),
                    actor=User.objects.get(pk=manager.pk),
                    original=StockTransaction.objects.get(pk=issued.pk),
                    operation_id=uuid.uuid4(),
                    reason="Concurrent correction",
                )
                return "reversed"
            except DomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["reversed", "already_reversed"])
        reservation.refresh_from_db()
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(reservation.status, Reservation.Status.ACTIVE)
        self.assertEqual(reservation.issued_quantity, Decimal("0"))
        self.assertEqual(reservation.remaining_quantity, Decimal("2"))
        self.assertEqual(balance.quantity_on_hand, Decimal("4"))
        self.assertEqual(balance.quantity_reserved, Decimal("2"))
        self.assertEqual(
            StockTransaction.objects.filter(
                transaction_type=StockTransaction.Type.REVERSAL,
                original_transaction=issued,
            ).count(),
            1,
        )
        self.assertEqual(reconcile_inventory(self.organization), [])

    def test_technician_stock_activity_is_scoped_to_assigned_work_orders(self) -> None:
        technician_role = Role.objects.create(
            organization=self.organization,
            slug="technician",
            name="Technician",
        )
        technician = User.objects.create_user(
            username="assigned-technician",
            password=None,
            organization=self.organization,
        )
        technician.roles.add(technician_role)
        other_technician = User.objects.create_user(
            username="other-technician",
            password=None,
            organization=self.organization,
        )
        other_technician.roles.add(technician_role)
        self.work_order.assigned_to = technician
        self.work_order.save(update_fields=["assigned_to", "updated_at"])
        other_work_order = WorkOrder.objects.create(
            organization=self.organization,
            asset=self.work_order.asset,
            number="WO-TEST-OTHER",
            summary="Other technician work",
            created_by=self.user,
            assigned_to=other_technician,
        )
        self.receive("12")
        own_reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        other_reservation = reserve_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=other_work_order,
            quantity="2",
            operation_id=uuid.uuid4(),
        )
        own_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=self.work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
            reservation=own_reservation,
        )
        other_issue = issue_stock(
            organization=self.organization,
            actor=self.user,
            part=self.part,
            bin=self.bin,
            work_order=other_work_order,
            quantity="1",
            operation_id=uuid.uuid4(),
            reservation=other_reservation,
        )
        own_return = return_stock(
            organization=self.organization,
            actor=self.user,
            original=own_issue,
            quantity="0.250",
            operation_id=uuid.uuid4(),
            reason="Unused assigned-work part",
        )
        other_return = return_stock(
            organization=self.organization,
            actor=self.user,
            original=other_issue,
            quantity="0.250",
            operation_id=uuid.uuid4(),
            reason="Unused other-work part",
        )

        clerk_rows = self.api_client.get("/api/v1/inventory/reservations/")
        self.assertEqual(clerk_rows.status_code, 200)
        self.assertEqual(
            {row["id"] for row in clerk_rows.data["reservations"]},
            {str(own_reservation.pk), str(other_reservation.pk)},
        )

        self.api_client.force_authenticate(technician)
        self.api_client.credentials(
            HTTP_X_OFFLINE_GRANT=issue_offline_access_grant(technician).token
        )
        reservation_rows = self.api_client.get("/api/v1/inventory/reservations/")
        issue_rows = self.api_client.get("/api/v1/inventory/issues/")
        return_rows = self.api_client.get("/api/v1/inventory/returns/")
        part_history = self.api_client.get(f"/api/v1/inventory/parts/{self.part.pk}/history/")
        bin_history = self.api_client.get(f"/api/v1/inventory/bins/{self.bin.pk}/history/")
        self.assertEqual(
            [row["id"] for row in reservation_rows.data["reservations"]],
            [str(own_reservation.pk)],
        )
        self.assertEqual(
            [row["id"] for row in issue_rows.data["transactions"]], [str(own_issue.pk)]
        )
        self.assertEqual(
            [row["id"] for row in return_rows.data["transactions"]], [str(own_return.pk)]
        )
        self.assertEqual(
            {row["id"] for row in part_history.data["transactions"]},
            {str(own_issue.pk), str(own_return.pk)},
        )
        self.assertEqual(
            {row["id"] for row in bin_history.data["transactions"]},
            {str(own_issue.pk), str(own_return.pk)},
        )
        self.assertNotIn(
            str(other_return.pk),
            {row["id"] for row in part_history.data["transactions"]},
        )

        denied_reserve = self.api_client.post(
            "/api/v1/inventory/reservations/",
            {
                "part_id": str(self.part.pk),
                "bin_id": str(self.bin.pk),
                "work_order_id": str(other_work_order.pk),
                "quantity": "0.500",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        denied_release = self.api_client.post(
            "/api/v1/inventory/reservations/",
            {
                "action": "release",
                "reservation_id": str(other_reservation.pk),
                "reason": "Unauthorized release",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        denied_issue = self.api_client.post(
            "/api/v1/inventory/issues/",
            {
                "part_id": str(self.part.pk),
                "bin_id": str(self.bin.pk),
                "work_order_id": str(other_work_order.pk),
                "quantity": "0.500",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        denied_return = self.api_client.post(
            "/api/v1/inventory/returns/",
            {
                "original_transaction_id": str(other_issue.pk),
                "quantity": "0.250",
                "reason": "Unauthorized return",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        for response in [denied_reserve, denied_release, denied_issue, denied_return]:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["error"]["code"], "permission_denied")

        allowed_issue = self.api_client.post(
            "/api/v1/inventory/issues/",
            {
                "part_id": str(self.part.pk),
                "bin_id": str(self.bin.pk),
                "work_order_id": str(self.work_order.pk),
                "quantity": "0.500",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(allowed_issue.status_code, 201)
        offline = self.api_client.post(
            "/api/v1/offline/sync/",
            {
                "operations": [
                    {
                        "operation_id": str(uuid.uuid4()),
                        "type": "stock.issue",
                        "payload": {
                            "part_id": str(self.part.pk),
                            "bin_id": str(self.bin.pk),
                            "work_order_id": str(other_work_order.pk),
                            "quantity": "0.250",
                        },
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(offline.status_code, 200)
        self.assertEqual(offline.data["results"][0]["status"], "rejected")
        self.assertEqual(offline.data["results"][0]["code"], "permission_denied")

    def test_narrow_bearer_scope_reaches_offline_inventory_checks(self) -> None:
        self.receive("2")
        raw_token = "dashboard-only-inventory-token"  # noqa: S105
        ApiToken.objects.create(
            organization=self.organization,
            user=self.user,
            name="Dashboard only",
            prefix="dash-stock",
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            scopes=["dashboard.view"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        bearer = APIClient()
        bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        response = bearer.post(
            "/api/v1/offline/sync/",
            {
                "operations": [
                    {
                        "operation_id": str(uuid.uuid4()),
                        "type": "stock.issue",
                        "payload": {
                            "part_id": str(self.part.pk),
                            "bin_id": str(self.bin.pk),
                            "work_order_id": str(self.work_order.pk),
                            "quantity": "1",
                        },
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "invalid_offline_grant")
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("2"),
        )
        self.assertEqual(reconcile_inventory(self.organization), [])

    def test_receipt_stock_requires_receipt_reversal_workflow(self) -> None:
        manager_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(manager_role)
        vendor = Vendor.objects.create(
            organization=self.organization, code="TEST", name="Test vendor"
        )
        order = PurchaseOrder.objects.create(
            organization=self.organization,
            vendor=vendor,
            number="PO-RECEIPT-GUARD",
            status=PurchaseOrder.Status.SENT,
            created_by=self.user,
        )
        order_line = PurchaseOrderLine.objects.create(
            organization=self.organization,
            purchase_order=order,
            part=self.part,
            description=self.part.name,
            quantity_ordered=Decimal("5"),
            unit_cost=Decimal("10"),
        )
        receipt = post_receipt(
            organization=self.organization,
            actor=self.user,
            purchase_order=order,
            operation_id=uuid.uuid4(),
            lines=[
                {
                    "purchase_order_line": order_line,
                    "bin": self.bin,
                    "quantity": Decimal("5"),
                }
            ],
        )
        original = receipt.lines.get().stock_transaction
        if original is None:
            self.fail("Posted receipt must have a stock transaction")

        response = self.api_client.post(
            "/api/v1/inventory/adjustments/",
            {
                "original_transaction_id": str(original.pk),
                "reason": "Wrong receipt",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "receipt_reversal_required")
        self.assertEqual(StockTransaction.objects.count(), 1)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("5"),
        )
        receipt.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(receipt.status, Receipt.Status.POSTED)
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)

    def test_bin_history_returns_same_org_ledger_in_deterministic_order(self) -> None:
        first = self.receive("2")
        second = self.receive("3")

        response = self.api_client.get(f"/api/v1/inventory/bins/{self.bin.pk}/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["bin"]["id"], str(self.bin.pk))
        self.assertEqual(
            [row["id"] for row in response.data["transactions"]],
            [str(second.pk), str(first.pk)],
        )

    def test_bin_history_hides_another_organization(self) -> None:
        self.receive("2")
        other = Organization.objects.create(name="Other Fleet", slug="other-fleet")
        other_user = User.objects.create_user(
            username="other-parts", password=None, organization=other
        )
        other_role = Role.objects.create(organization=other, slug="parts_clerk", name="Parts clerk")
        other_user.roles.add(other_role)
        self.api_client.force_authenticate(other_user)

        response = self.api_client.get(f"/api/v1/inventory/bins/{self.bin.pk}/history/")

        self.assertEqual(response.status_code, 404)

    def test_final_unit_cannot_be_issued_twice(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        self.receive("1")

        def attempt() -> str:
            close_old_connections()
            try:
                issue_stock(
                    organization=Organization.objects.get(pk=self.organization.pk),
                    actor=User.objects.get(pk=self.user.pk),
                    part=Part.objects.get(pk=self.part.pk),
                    bin=Bin.objects.select_related("warehouse").get(pk=self.bin.pk),
                    work_order=WorkOrder.objects.get(pk=self.work_order.pk),
                    quantity="1",
                    operation_id=uuid.uuid4(),
                )
                return "issued"
            except DomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["issued", "insufficient_available_stock"])
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("0"),
        )

    def make_approver(self, username: str = "inventory-approver") -> User:
        role, _ = Role.objects.get_or_create(
            organization=self.organization,
            slug="purchasing_manager",
            defaults={"name": "Purchasing manager"},
        )
        user = User.objects.create_user(
            username=username,
            password=None,
            organization=self.organization,
        )
        user.roles.add(role)
        return user

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_count_variance_waits_for_independent_approval_before_posting(self) -> None:
        self.receive("5")
        count_operation_id = uuid.uuid4()
        response = self.api_client.post(
            "/api/v1/inventory/counts/",
            {
                "warehouse_id": str(self.warehouse.pk),
                "reason": "Monthly count",
                "lines": [
                    {
                        "part_id": str(self.part.pk),
                        "bin_id": str(self.bin.pk),
                        "counted_quantity": "7",
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(count_operation_id),
        )

        self.assertEqual(response.status_code, 201)
        count = InventoryCount.objects.get(pk=response.data["count"]["id"])
        self.assertEqual(count.status, InventoryCount.Status.PENDING_APPROVAL)
        self.assertTrue(count.approval_required)
        self.assertEqual(count.total_variance_value, Decimal("20.0000"))
        self.assertIsNone(count.lines.get().adjustment_transaction_id)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("5"),
        )
        self.part.default_unit_cost = Decimal("99.0000")
        self.part.save(update_fields=["default_unit_cost", "updated_at"])

        approver = self.make_approver()
        self.api_client.force_authenticate(approver)
        approval_operation_id = uuid.uuid4()
        approved = self.api_client.post(
            f"/api/v1/inventory/counts/{count.pk}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(approval_operation_id),
        )

        self.assertEqual(approved.status_code, 200)
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.Status.POSTED)
        self.assertEqual(count.approved_by, approver)
        self.assertEqual(count.posted_by, approver)
        self.assertIsNotNone(count.approved_at)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("7"),
        )
        line = count.lines.get()
        adjustment = line.adjustment_transaction
        if adjustment is None:
            self.fail("Approved variance must create an adjustment transaction")
        self.assertEqual(adjustment.quantity, Decimal("2"))
        self.assertEqual(adjustment.unit_cost, Decimal("10.0000"))
        self.assertEqual(adjustment.total_cost, Decimal("20.0000"))
        events = list(
            AuditEvent.objects.filter(
                organization=self.organization,
                resource_type="InventoryCount",
                resource_id=str(count.pk),
            ).order_by("occurred_at")
        )
        self.assertEqual(
            [(event.previous_state, event.new_state) for event in events],
            [
                (InventoryCount.Status.DRAFT, InventoryCount.Status.PENDING_APPROVAL),
                (InventoryCount.Status.PENDING_APPROVAL, InventoryCount.Status.POSTED),
            ],
        )
        self.assertTrue(all(event.correlation_id == str(count_operation_id) for event in events))
        self.assertEqual(events[-1].context["approval_operation_id"], str(approval_operation_id))

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_no_variance_and_adjust_authority_post_immediately(self) -> None:
        no_variance = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Empty bin verified",
            lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "0"}],
        )
        self.assertEqual(no_variance.status, InventoryCount.Status.POSTED)
        self.assertFalse(no_variance.approval_required)

        self.receive("5")
        approver = self.make_approver()
        self.api_client.force_authenticate(approver)
        response = self.api_client.post(
            "/api/v1/inventory/counts/",
            {
                "warehouse_id": str(self.warehouse.pk),
                "reason": "Manager spot count",
                "lines": [
                    {
                        "part_id": str(self.part.pk),
                        "bin_id": str(self.bin.pk),
                        "counted_quantity": "6",
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 201)
        direct = InventoryCount.objects.get(pk=response.data["count"]["id"])
        self.assertEqual(direct.status, InventoryCount.Status.POSTED)
        self.assertFalse(direct.approval_required)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("6"),
        )

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_count_submitter_cannot_approve_and_other_tenant_cannot_find_count(self) -> None:
        self.receive("5")
        count = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "7"}],
        )
        denied = self.api_client.post(
            f"/api/v1/inventory/counts/{count.pk}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.data["error"]["code"], "permission_denied")

        adjust_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(adjust_role)
        rejected = self.api_client.post(
            f"/api/v1/inventory/counts/{count.pk}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.data["error"]["code"], "separation_of_duties")

        other = Organization.objects.create(name="Other fleet", slug="other-count-fleet")
        other_role = Role.objects.create(
            organization=other,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        other_user = User.objects.create_user(
            username="other-count-approver",
            password=None,
            organization=other,
        )
        other_user.roles.add(other_role)
        self.api_client.force_authenticate(other_user)
        hidden = self.api_client.post(
            f"/api/v1/inventory/counts/{count.pk}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(hidden.status_code, 404)
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.Status.PENDING_APPROVAL)
        self.assertFalse(
            StockTransaction.objects.filter(
                reference_type="inventory_count", reference_id=str(count.pk)
            ).exists()
        )

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_count_only_token_cannot_bypass_adjustment_approval(self) -> None:
        self.receive("5")
        adjust_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(adjust_role)
        raw_token = b"count-only-test-token"
        ApiToken.objects.create(
            organization=self.organization,
            user=self.user,
            name="Count only",
            prefix="count-only",
            token_hash=hashlib.sha256(raw_token).hexdigest(),
            scopes=["inventory.count"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token.decode()}")

        response = client.post(
            "/api/v1/inventory/counts/",
            {
                "warehouse_id": str(self.warehouse.pk),
                "reason": "Scoped count",
                "lines": [
                    {
                        "part_id": str(self.part.pk),
                        "bin_id": str(self.bin.pk),
                        "counted_quantity": "7",
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["count"]["status"], InventoryCount.Status.PENDING_APPROVAL)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("5"),
        )

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_approval_rejects_stale_balance_without_partial_posting(self) -> None:
        self.receive("5")
        count = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "7"}],
        )
        approver = self.make_approver()
        adjust_stock(
            organization=self.organization,
            actor=approver,
            part=self.part,
            bin=self.bin,
            quantity="1",
            operation_id=uuid.uuid4(),
            reason="Stock found before approval",
        )
        self.api_client.force_authenticate(approver)

        response = self.api_client.post(
            f"/api/v1/inventory/counts/{count.pk}/approve/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "count_balance_stale")
        self.assertEqual(response.data["error"]["details"]["current_quantity"], "6.000")
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.Status.PENDING_APPROVAL)
        self.assertIsNone(count.lines.get().adjustment_transaction_id)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("6"),
        )

    @override_settings(INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD="0.00")
    def test_concurrent_count_approval_posts_one_adjustment(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Row-lock concurrency behavior requires PostgreSQL")
        self.receive("5")
        count = post_inventory_count(
            organization=self.organization,
            actor=self.user,
            warehouse=self.warehouse,
            operation_id=uuid.uuid4(),
            reason="Cycle count",
            lines=[{"part": self.part, "bin": self.bin, "counted_quantity": "7"}],
        )
        approvers = [self.make_approver(f"count-approver-{index}") for index in range(2)]

        def attempt(user_id: uuid.UUID) -> str:
            close_old_connections()
            try:
                approve_inventory_count(
                    organization=Organization.objects.get(pk=self.organization.pk),
                    actor=User.objects.get(pk=user_id),
                    count=InventoryCount.objects.get(pk=count.pk),
                    operation_id=uuid.uuid4(),
                )
                return "posted"
            except DomainError as exc:
                return exc.code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [user.pk for user in approvers]))

        self.assertCountEqual(results, ["posted", "count_not_pending_approval"])
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCount.Status.POSTED)
        self.assertEqual(
            StockTransaction.objects.filter(
                reference_type="inventory_count", reference_id=str(count.pk)
            ).count(),
            1,
        )
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("7"),
        )


class PartIdentifierLookupTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Lookup Fleet", slug="lookup-fleet")
        role = Role.objects.create(
            organization=self.organization, slug="parts_clerk", name="Parts clerk"
        )
        self.user = User.objects.create_user(
            username="lookup-parts", password=None, organization=self.organization
        )
        self.user.roles.add(role)
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.user)
        self.part = Part.objects.create(
            organization=self.organization,
            number="FLT-LOOKUP-1",
            name="Lookup oil filter",
            manufacturer_number="MFG-LOOKUP-1",
            barcode="SCAN-LOOKUP-1",
        )
        PartCrossReference.objects.create(
            organization=self.organization,
            part=self.part,
            kind=PartCrossReference.Kind.ALTERNATE,
            value="ALT-LOOKUP-1",
        )
        self.vendor = Vendor.objects.create(
            organization=self.organization, code="LOOKUP", name="Lookup Supply"
        )
        VendorPart.objects.create(
            organization=self.organization,
            vendor=self.vendor,
            part=self.part,
            vendor_part_number="VENDOR-LOOKUP-1",
            unit_cost=Decimal("8.5000"),
        )

    def test_exact_lookup_resolves_supported_identifiers_case_insensitively(self) -> None:
        identifiers = (
            "flt-lookup-1",
            "mfg-lookup-1",
            "scan-lookup-1",
            "alt-lookup-1",
            "vendor-lookup-1",
        )
        for identifier_value in identifiers:
            with self.subTest(identifier=identifier_value):
                response = self.api_client.get(
                    "/api/v1/inventory/parts/", {"identifier": identifier_value, "active": "true"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["id"] for row in response.data["parts"]], [str(self.part.pk)])

    def test_ambiguous_vendor_identifier_returns_choices_instead_of_guessing(self) -> None:
        second_part = Part.objects.create(
            organization=self.organization, number="FLT-LOOKUP-2", name="Second lookup part"
        )
        second_vendor = Vendor.objects.create(
            organization=self.organization, code="LOOKUP-2", name="Second Lookup Supply"
        )
        for vendor, part in ((self.vendor, self.part), (second_vendor, second_part)):
            VendorPart.objects.create(
                organization=self.organization,
                vendor=vendor,
                part=part,
                vendor_part_number="SHARED-VENDOR-NUMBER",
                unit_cost=Decimal("9.0000"),
            )

        response = self.api_client.get(
            "/api/v1/inventory/parts/", {"identifier": "shared-vendor-number"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(
            [row["id"] for row in response.data["parts"]],
            [str(self.part.pk), str(second_part.pk)],
        )

    def test_lookup_and_global_search_are_permission_and_organization_scoped(self) -> None:
        other = Organization.objects.create(name="Private Fleet", slug="private-lookup-fleet")
        private_part = Part.objects.create(
            organization=other,
            number="PRIVATE-LOOKUP",
            name="Private tenant part",
            barcode="PRIVATE-SCAN-ONLY",
        )
        other_vendor = Vendor.objects.create(
            organization=other, code="PRIVATE", name="Private Supply"
        )
        VendorPart.objects.create(
            organization=other,
            vendor=other_vendor,
            part=private_part,
            vendor_part_number="PRIVATE-VENDOR-ONLY",
            unit_cost=Decimal("1.0000"),
        )

        for identifier_value in ("PRIVATE-SCAN-ONLY", "PRIVATE-VENDOR-ONLY"):
            response = self.api_client.get(
                "/api/v1/inventory/parts/", {"identifier": identifier_value}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["parts"], [])
            search = self.api_client.get("/api/v1/search/", {"q": identifier_value})
            self.assertEqual(search.status_code, 200)
            self.assertEqual(search.data["results"], [])

        for identifier_value in ("SCAN-LOOKUP-1", "VENDOR-LOOKUP-1", "ALT-LOOKUP-1"):
            search = self.api_client.get("/api/v1/search/", {"q": identifier_value})
            self.assertEqual(search.status_code, 200)
            self.assertEqual([row["id"] for row in search.data["results"]], [str(self.part.pk)])

        driver_role = Role.objects.create(
            organization=self.organization, slug="driver", name="Driver"
        )
        driver = User.objects.create_user(
            username="lookup-driver", password=None, organization=self.organization
        )
        driver.roles.add(driver_role)
        self.api_client.force_authenticate(driver)
        denied = self.api_client.get("/api/v1/inventory/parts/", {"identifier": "SCAN-LOOKUP-1"})
        self.assertEqual(denied.status_code, 403)
        search = self.api_client.get("/api/v1/search/", {"q": "SCAN-LOOKUP-1"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.data["results"], [])

    def test_part_cost_is_redacted_without_financial_permission(self) -> None:
        response = self.api_client.get("/api/v1/inventory/parts/", {"identifier": self.part.number})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("default_unit_cost", response.data["parts"][0])

        finance_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user.roles.add(finance_role)
        response = self.api_client.get("/api/v1/inventory/parts/", {"identifier": self.part.number})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["parts"][0]["default_unit_cost"],
            format(self.part.default_unit_cost, ".4f"),
        )
