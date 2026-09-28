from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Lock, local
from unittest.mock import patch

from core.exceptions import DomainError
from core.models import AuditEvent, Location, Organization, Role, User
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from inventory import services as inventory_services
from inventory.models import Bin, Part, StockBalance, StockTransaction, Warehouse
from inventory.services import reconcile_inventory
from rest_framework.test import APIClient

from .models import PurchaseOrder, PurchaseRequest, Receipt, ReceiptLine, Vendor
from .services import (
    create_purchase_order,
    create_purchase_request,
    post_receipt,
    reverse_receipt,
    transition_purchase_order,
    transition_purchase_request,
)


class PurchasingServiceTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Gator Fleet", slug="gator")
        self.location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )
        self.requester = User.objects.create(username="buyer", organization=self.organization)
        self.approver = User.objects.create(username="approver", organization=self.organization)
        self.receiver = User.objects.create(username="receiver", organization=self.organization)
        self.vendor = self.organization.vendor_set.create(code="SUPPLY", name="Fleet Supply")
        self.part = Part.objects.create(
            organization=self.organization,
            number="FILTER-1",
            name="Oil filter",
            default_unit_cost=Decimal("5.0000"),
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="MAIN",
            name="Main Warehouse",
        )
        self.bin = Bin.objects.create(
            organization=self.organization,
            warehouse=self.warehouse,
            code="A-01",
            name="Filters",
        )

    def ready_order(self, quantity: str = "10.000", unit_cost: str = "5.0000") -> PurchaseOrder:
        order = create_purchase_order(
            organization=self.organization,
            actor=self.requester,
            vendor=self.vendor,
            number=f"PO-{uuid.uuid4().hex[:8]}",
            lines=[
                {
                    "part": self.part,
                    "quantity_ordered": Decimal(quantity),
                    "unit_cost": Decimal(unit_cost),
                }
            ],
        )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.requester,
            purchase_order=order,
            target=PurchaseOrder.Status.SUBMITTED,
        )
        self.assertEqual(order.status, PurchaseOrder.Status.APPROVED)
        return transition_purchase_order(
            organization=self.organization,
            actor=self.requester,
            purchase_order=order,
            target=PurchaseOrder.Status.SENT,
        )

    def receive(self, order: PurchaseOrder, quantity: str, operation_id: uuid.UUID) -> Receipt:
        return post_receipt(
            organization=self.organization,
            actor=self.receiver,
            purchase_order=order,
            operation_id=operation_id,
            lines=[
                {
                    "purchase_order_line": order.lines.get(),
                    "bin": self.bin,
                    "quantity": Decimal(quantity),
                }
            ],
        )

    def test_partial_then_final_receipt_updates_only_received_stock(self) -> None:
        order = self.ready_order()
        first = self.receive(order, "4.000", uuid.uuid4())
        order.refresh_from_db()
        balance = StockBalance.objects.get(part=self.part, bin=self.bin)
        self.assertEqual(first.status, Receipt.Status.POSTED)
        self.assertEqual(order.status, PurchaseOrder.Status.PARTIALLY_RECEIVED)
        self.assertEqual(balance.quantity_on_hand, Decimal("4.000"))
        self.assertEqual(order.lines.get().quantity_remaining, Decimal("6.000"))

        self.receive(order, "6.000", uuid.uuid4())
        order.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.000"))
        self.assertEqual(order.lines.get().quantity_remaining, Decimal("0.000"))
        self.assertEqual(StockTransaction.objects.count(), 2)

    def test_duplicate_receipt_operation_returns_original_without_duplicate_stock(self) -> None:
        order = self.ready_order()
        operation_id = uuid.uuid4()
        original = self.receive(order, "4.000", operation_id)
        duplicate = self.receive(order, "4.000", operation_id)
        self.assertEqual(duplicate.pk, original.pk)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(StockTransaction.objects.count(), 1)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("4.000"),
        )

    def test_over_receipt_is_rejected_atomically(self) -> None:
        order = self.ready_order()
        with self.assertRaisesMessage(DomainError, "exceeds the open order quantity"):
            self.receive(order, "10.001", uuid.uuid4())
        self.assertFalse(Receipt.objects.exists())
        self.assertFalse(StockBalance.objects.exists())
        self.assertFalse(StockTransaction.objects.exists())

    def test_reversal_appends_compensating_receipt_and_stock_transaction(self) -> None:
        order = self.ready_order()
        original = self.receive(order, "10.000", uuid.uuid4())
        reversal = reverse_receipt(
            organization=self.organization,
            actor=self.receiver,
            receipt=original,
            operation_id=uuid.uuid4(),
            reason="Packing slip was for another shop",
        )
        original.refresh_from_db()
        order.refresh_from_db()
        transactions = StockTransaction.objects.order_by("created_at")
        self.assertEqual(original.status, Receipt.Status.REVERSED)
        self.assertEqual(reversal.reversal_of_id, original.pk)
        self.assertEqual(reversal.lines.get().quantity, Decimal("-10.000"))
        self.assertEqual(transactions.count(), 2)
        self.assertEqual(transactions[1].original_transaction_id, transactions[0].pk)
        self.assertEqual(
            StockBalance.objects.get(part=self.part, bin=self.bin).quantity_on_hand,
            Decimal("0.000"),
        )
        self.assertEqual(order.status, PurchaseOrder.Status.SENT)
        self.assertTrue(
            AuditEvent.objects.filter(
                organization=self.organization,
                action="receipt.reversed",
                resource_id=str(original.pk),
            ).exists()
        )

    def test_threshold_order_requires_another_approver(self) -> None:
        order = create_purchase_order(
            organization=self.organization,
            actor=self.requester,
            vendor=self.vendor,
            number="PO-APPROVAL",
            lines=[
                {
                    "part": self.part,
                    "quantity_ordered": Decimal("10"),
                    "unit_cost": Decimal("200"),
                }
            ],
        )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.requester,
            purchase_order=order,
            target=PurchaseOrder.Status.SUBMITTED,
        )
        self.assertEqual(order.status, PurchaseOrder.Status.SUBMITTED)
        with self.assertRaisesMessage(DomainError, "submitter cannot approve"):
            transition_purchase_order(
                organization=self.organization,
                actor=self.requester,
                purchase_order=order,
                target=PurchaseOrder.Status.APPROVED,
            )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.approver,
            purchase_order=order,
            target=PurchaseOrder.Status.APPROVED,
        )
        self.assertEqual(order.status, PurchaseOrder.Status.APPROVED)
        self.assertEqual(order.approved_by_id, self.approver.pk)


class ReceiptConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Concurrent Receiving Fleet", slug="concurrent-receiving"
        )
        self.requester = User.objects.create(
            username="concurrent-buyer", organization=self.organization
        )
        self.receiver = User.objects.create(
            username="concurrent-receiver", organization=self.organization
        )
        self.vendor = Vendor.objects.create(
            organization=self.organization,
            code="CONCURRENT",
            name="Concurrent Supply",
        )
        self.location = Location.objects.create(
            organization=self.organization,
            name="Concurrent Shop",
            code="CONCURRENT",
        )
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="CONCURRENT",
            name="Concurrent Warehouse",
        )
        self.parts = [
            Part.objects.create(
                organization=self.organization,
                number=f"CONCURRENT-{index}",
                name=f"Concurrent part {index}",
            )
            for index in range(2)
        ]
        self.bins = [
            Bin.objects.create(
                organization=self.organization,
                warehouse=self.warehouse,
                code=f"C-{index}",
            )
            for index in range(2)
        ]
        for part, stock_bin in zip(self.parts, self.bins, strict=True):
            StockBalance.objects.create(
                organization=self.organization,
                part=part,
                bin=stock_bin,
            )
        self.orders = [self._ready_order(index) for index in range(2)]

    def _ready_order(self, index: int) -> PurchaseOrder:
        order = create_purchase_order(
            organization=self.organization,
            actor=self.requester,
            vendor=self.vendor,
            number=f"PO-CONCURRENT-{index}",
            lines=[
                {
                    "part": part,
                    "quantity_ordered": Decimal("1"),
                    "unit_cost": Decimal("1"),
                }
                for part in self.parts
            ],
        )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.requester,
            purchase_order=order,
            target=PurchaseOrder.Status.SUBMITTED,
        )
        return transition_purchase_order(
            organization=self.organization,
            actor=self.requester,
            purchase_order=order,
            target=PurchaseOrder.Status.SENT,
        )

    def test_concurrent_receipts_lock_balances_in_canonical_order(self) -> None:
        self.assertEqual(connection.vendor, "postgresql")
        before_first_balance = Barrier(2)
        after_distinct_first_balances = Barrier(2)
        first_balance_keys: list[tuple[object, object]] = []
        first_balance_keys_lock = Lock()
        thread_state = local()
        real_receive_stock = inventory_services.receive_stock

        def synchronized_receive_stock(*args: object, **kwargs: object) -> StockTransaction:
            first_call = not getattr(thread_state, "received_stock", False)
            distinct_first_balances = False
            if first_call:
                thread_state.received_stock = True
                with first_balance_keys_lock:
                    part = kwargs["part"]
                    stock_bin = kwargs["bin"]
                    first_balance_keys.append((part.pk, stock_bin.pk))
                before_first_balance.wait(timeout=10)
                with first_balance_keys_lock:
                    distinct_first_balances = len(set(first_balance_keys)) > 1
            transaction = real_receive_stock(*args, **kwargs)
            if first_call and distinct_first_balances:
                # This branch deterministically recreates the former deadlock:
                # both transactions hold a different first balance before either
                # attempts its second balance. Canonical ordering avoids it.
                after_distinct_first_balances.wait(timeout=10)
            return transaction

        def receive(order_id: object, reverse: bool) -> str:
            close_old_connections()
            try:
                organization = Organization.objects.get(pk=self.organization.pk)
                actor = User.objects.get(pk=self.receiver.pk)
                order = PurchaseOrder.objects.get(pk=order_id)
                order_lines = {
                    line.part_id: line for line in order.lines.select_related("part").all()
                }
                bins = {
                    stock_bin.pk: stock_bin
                    for stock_bin in Bin.objects.select_related("warehouse").filter(
                        pk__in=[stock_bin.pk for stock_bin in self.bins]
                    )
                }
                pairs = list(zip(self.parts, self.bins, strict=True))
                if reverse:
                    pairs.reverse()
                lines = [
                    {
                        "purchase_order_line": order_lines[part.pk],
                        "bin": bins[stock_bin.pk],
                        "quantity": Decimal("1"),
                    }
                    for part, stock_bin in pairs
                ]
                receipt = post_receipt(
                    organization=organization,
                    actor=actor,
                    purchase_order=order,
                    lines=lines,
                    operation_id=uuid.uuid4(),
                )
                return str(receipt.pk)
            finally:
                connections.close_all()

        with (
            patch.object(inventory_services, "receive_stock", new=synchronized_receive_stock),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [
                pool.submit(receive, self.orders[0].pk, False),
                pool.submit(receive, self.orders[1].pk, True),
            ]
            receipt_ids = [future.result(timeout=15) for future in futures]

        self.assertEqual(len(set(first_balance_keys)), 1)
        self.assertEqual(len(set(receipt_ids)), 2)
        self.assertEqual(Receipt.objects.filter(status=Receipt.Status.POSTED).count(), 2)
        self.assertEqual(ReceiptLine.objects.count(), 4)
        self.assertEqual(
            StockTransaction.objects.filter(
                transaction_type=StockTransaction.Type.RECEIPT,
                reference_type="receipt_line",
            ).count(),
            4,
        )
        for part, stock_bin in zip(self.parts, self.bins, strict=True):
            balance = StockBalance.objects.get(part=part, bin=stock_bin)
            self.assertEqual(balance.quantity_on_hand, Decimal("2"))
            self.assertEqual(balance.quantity_reserved, Decimal("0"))
        orders = PurchaseOrder.objects.filter(
            pk__in=[row.pk for row in self.orders]
        ).prefetch_related("lines")
        self.assertTrue(all(order.status == PurchaseOrder.Status.RECEIVED for order in orders))
        self.assertTrue(
            all(line.quantity_remaining == 0 for order in orders for line in order.lines.all())
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.organization,
                action="receipt.posted",
            ).count(),
            2,
        )
        self.assertEqual(reconcile_inventory(self.organization), [])


class PurchaseRequestApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="North Fleet", slug="north-fleet")
        self.other_organization = Organization.objects.create(
            name="South Fleet", slug="south-fleet"
        )
        self.parts_role = Role.objects.create(
            organization=self.organization, slug="parts_clerk", name="Parts clerk"
        )
        self.manager_role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.other_manager_role = Role.objects.create(
            organization=self.other_organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.driver_role = Role.objects.create(
            organization=self.organization, slug="driver", name="Driver"
        )
        self.requester = User.objects.create_user(
            username="parts", password=None, organization=self.organization
        )
        self.requester.roles.add(self.parts_role)
        self.approver = User.objects.create_user(
            username="purchasing", password=None, organization=self.organization
        )
        self.approver.roles.add(self.manager_role)
        self.driver = User.objects.create_user(
            username="driver", password=None, organization=self.organization
        )
        self.driver.roles.add(self.driver_role)
        self.outsider = User.objects.create_user(
            username="outsider", password=None, organization=self.other_organization
        )
        self.outsider.roles.add(self.other_manager_role)
        self.part = Part.objects.create(
            organization=self.organization,
            number="PR-FILTER",
            name="Purchase request filter",
            default_unit_cost=Decimal("7.0000"),
        )
        self.other_part = Part.objects.create(
            organization=self.organization,
            number="PR-BELT",
            name="Purchase request belt",
            default_unit_cost=Decimal("11.0000"),
        )
        self.vendor = self.organization.vendor_set.create(code="PR-SUPPLY", name="PR Supply")
        self.client = APIClient()

    def create_request(self, *, key: uuid.UUID | None = None) -> object:
        self.client.force_authenticate(self.requester)
        return self.client.post(
            "/api/v1/purchasing/purchase-requests/",
            {
                "part_id": str(self.part.pk),
                "quantity": "3",
                "reason": "Replenish service stock",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()),
        )

    def transition(
        self,
        purchase_request: PurchaseRequest,
        *,
        actor: User,
        target: str,
        reason: str = "",
        key: uuid.UUID | None = None,
    ) -> object:
        self.client.force_authenticate(actor)
        return self.client.post(
            f"/api/v1/purchasing/purchase-requests/{purchase_request.pk}/transition/",
            {"target": target, "reason": reason},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()),
        )

    def order_payload(
        self,
        purchase_request: PurchaseRequest,
        *,
        part: Part | None = None,
        duplicate: bool = False,
    ) -> dict[str, object]:
        line = {
            "part_id": str((part or self.part).pk),
            "purchase_request_id": str(purchase_request.pk),
            "quantity_ordered": "3",
            "unit_cost": "7",
        }
        return {
            "vendor_id": str(self.vendor.pk),
            "number": f"PO-{uuid.uuid4().hex[:8]}",
            "lines": [line, dict(line)] if duplicate else [line],
        }

    def test_create_approve_and_retry_are_idempotent_and_audited(self) -> None:
        create_key = uuid.uuid4()
        first = self.create_request(key=create_key)
        second = self.create_request(key=create_key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(PurchaseRequest.objects.count(), 1)
        purchase_request = PurchaseRequest.objects.get()

        self.requester.roles.add(self.manager_role)
        own_approval = self.transition(
            purchase_request,
            actor=self.requester,
            target=PurchaseRequest.Status.APPROVED,
        )
        self.assertEqual(own_approval.status_code, 403)
        self.assertEqual(own_approval.json()["error"]["code"], "separation_of_duties")

        approval_key = uuid.uuid4()
        approved = self.transition(
            purchase_request,
            actor=self.approver,
            target=PurchaseRequest.Status.APPROVED,
            key=approval_key,
        )
        replayed = self.transition(
            purchase_request,
            actor=self.approver,
            target=PurchaseRequest.Status.APPROVED,
            key=approval_key,
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json(), replayed.json())
        purchase_request.refresh_from_db()
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.APPROVED)
        self.assertEqual(purchase_request.approved_by_id, self.approver.pk)
        self.assertIsNotNone(purchase_request.approved_at)
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.organization,
                resource_id=str(purchase_request.pk),
                action="purchase_request.approved",
            ).count(),
            1,
        )

        wrong_state = self.transition(
            purchase_request,
            actor=self.approver,
            target=PurchaseRequest.Status.APPROVED,
        )
        self.assertEqual(wrong_state.status_code, 400)
        self.assertEqual(wrong_state.json()["error"]["code"], "invalid_transition")

    def test_rejection_and_cancellation_require_reasons_and_permissions(self) -> None:
        purchase_request = create_purchase_request(
            organization=self.organization,
            actor=self.requester,
            part=self.part,
            quantity=Decimal("1"),
            reason="Request to cancel",
        )
        other_clerk = User.objects.create_user(
            username="other-parts", password=None, organization=self.organization
        )
        other_clerk.roles.add(self.parts_role)
        denied = self.transition(
            purchase_request,
            actor=other_clerk,
            target=PurchaseRequest.Status.CANCELLED,
            reason="Not needed",
        )
        self.assertEqual(denied.status_code, 403)

        missing_reason = self.transition(
            purchase_request,
            actor=self.requester,
            target=PurchaseRequest.Status.CANCELLED,
        )
        self.assertEqual(missing_reason.status_code, 400)
        cancelled = self.transition(
            purchase_request,
            actor=self.requester,
            target=PurchaseRequest.Status.CANCELLED,
            reason="Work scope changed",
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(
            AuditEvent.objects.filter(
                resource_id=str(purchase_request.pk),
                action="purchase_request.cancelled",
                context__reason="Work scope changed",
            ).exists()
        )

        rejected_request = create_purchase_request(
            organization=self.organization,
            actor=self.requester,
            part=self.part,
            quantity=Decimal("2"),
            reason="Request to reject",
        )
        rejected = self.transition(
            rejected_request,
            actor=self.approver,
            target=PurchaseRequest.Status.REJECTED,
            reason="Existing supply is sufficient",
        )
        self.assertEqual(rejected.status_code, 200)
        rejected_request.refresh_from_db()
        self.assertEqual(rejected_request.status, PurchaseRequest.Status.REJECTED)

    def test_only_approved_matching_request_converts_once_and_po_replay_is_safe(self) -> None:
        purchase_request = PurchaseRequest.objects.create(
            organization=self.organization,
            requested_by=self.requester,
            part=self.part,
            quantity=Decimal("3"),
            reason="Convert me",
        )
        self.client.force_authenticate(self.approver)
        unapproved = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            self.order_payload(purchase_request),
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(unapproved.status_code, 409)
        self.assertEqual(unapproved.json()["error"]["code"], "purchase_request_not_approved")

        transition_purchase_request(
            organization=self.organization,
            actor=self.approver,
            purchase_request=purchase_request,
            target=PurchaseRequest.Status.APPROVED,
        )
        mismatch = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            self.order_payload(purchase_request, part=self.other_part),
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["error"]["code"], "purchase_request_part_mismatch")
        duplicate = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            self.order_payload(purchase_request, duplicate=True),
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "duplicate_purchase_request")

        payload = self.order_payload(purchase_request)
        operation_id = uuid.uuid4()
        created = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        replayed = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(operation_id),
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json(), replayed.json())
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        purchase_request.refresh_from_db()
        self.assertEqual(purchase_request.status, PurchaseRequest.Status.CONVERTED)
        self.assertEqual(
            AuditEvent.objects.filter(
                resource_id=str(purchase_request.pk), action="purchase_request.converted"
            ).count(),
            1,
        )

        second_conversion = self.client.post(
            "/api/v1/purchasing/purchase-orders/",
            self.order_payload(purchase_request),
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(second_conversion.status_code, 409)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_requests_are_permission_and_tenant_scoped(self) -> None:
        local_request = create_purchase_request(
            organization=self.organization,
            actor=self.requester,
            part=self.part,
            quantity=Decimal("1"),
            reason="Tenant test",
        )
        self.client.force_authenticate(self.driver)
        denied = self.client.get("/api/v1/purchasing/purchase-requests/")
        self.assertEqual(denied.status_code, 403)
        denied_create = self.client.post(
            "/api/v1/purchasing/purchase-requests/",
            {"part_id": str(self.part.pk), "quantity": "1", "reason": "Not allowed"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(denied_create.status_code, 403)

        self.client.force_authenticate(self.outsider)
        listed = self.client.get("/api/v1/purchasing/purchase-requests/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["purchase_requests"], [])
        foreign_part = self.client.post(
            "/api/v1/purchasing/purchase-requests/",
            {"part_id": str(self.part.pk), "quantity": "1", "reason": "Cross tenant"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(foreign_part.status_code, 404)
        missing = self.client.post(
            f"/api/v1/purchasing/purchase-requests/{local_request.pk}/transition/",
            {"target": PurchaseRequest.Status.APPROVED},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(missing.status_code, 404)


class PurchasingBoundaryApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Boundary Fleet", slug="boundary")
        role = Role.objects.create(
            organization=self.organization,
            slug="purchasing_manager",
            name="Purchasing manager",
        )
        self.user = User.objects.create_user(
            username="boundary-buyer", password=None, organization=self.organization
        )
        self.user.roles.add(role)
        self.part = Part.objects.create(
            organization=self.organization,
            number="BOUNDARY-1",
            name="Boundary part",
        )
        self.vendor = Vendor.objects.create(
            organization=self.organization,
            code="BOUNDARY",
            name="Boundary Supply",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, path: str, payload: dict[str, object]) -> object:
        return self.client.post(
            path,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    def test_vendor_create_is_idempotent_and_duplicate_code_is_a_conflict(self) -> None:
        payload = {"code": "idem", "name": "Idempotent Supply"}
        key = str(uuid.uuid4())
        first = self.client.post(
            "/api/v1/purchasing/vendors/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        replay = self.client.post(
            "/api/v1/purchasing/vendors/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        duplicate = self.post("/api/v1/purchasing/vendors/", payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(Vendor.objects.filter(code="IDEM").count(), 1)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "duplicate_vendor")

    def test_invalid_uuid_and_non_finite_purchase_request_quantity_are_rejected(self) -> None:
        invalid_id = self.post(
            "/api/v1/purchasing/purchase-requests/",
            {"part_id": "not-a-uuid", "quantity": "1", "reason": "Boundary test"},
        )
        non_finite = self.post(
            "/api/v1/purchasing/purchase-requests/",
            {"part_id": str(self.part.pk), "quantity": "Infinity", "reason": "Boundary test"},
        )

        self.assertEqual(invalid_id.status_code, 400)
        self.assertEqual(invalid_id.json()["error"]["code"], "invalid_part_id")
        self.assertEqual(non_finite.status_code, 400)
        self.assertEqual(non_finite.json()["error"]["code"], "invalid_quantity")
        self.assertFalse(PurchaseRequest.objects.exists())

    def test_purchase_order_numeric_precision_and_ranges_are_rejected(self) -> None:
        cases = [
            ("0.0001", "1", "invalid_quantity_ordered"),
            ("100000000000", "1", "invalid_quantity_ordered"),
            ("1", "0.00001", "invalid_unit_cost"),
            ("1", "10000000000", "invalid_unit_cost"),
            ("99999999999", "9999999999", "invalid_line_total"),
        ]
        for quantity, unit_cost, code in cases:
            with self.subTest(code=code, quantity=quantity, unit_cost=unit_cost):
                response = self.post(
                    "/api/v1/purchasing/purchase-orders/",
                    {
                        "vendor_id": str(self.vendor.pk),
                        "lines": [
                            {
                                "part_id": str(self.part.pk),
                                "quantity_ordered": quantity,
                                "unit_cost": unit_cost,
                            }
                        ],
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], code)
        self.assertFalse(PurchaseOrder.objects.exists())
