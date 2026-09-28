from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from core.exceptions import DomainError
from core.models import Location, Organization, User
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from inventory.models import Bin, Part, Warehouse

from .models import PurchaseOrder, Receipt
from .services import (
    create_purchase_order,
    post_receipt,
    reverse_receipt,
    transition_purchase_order,
)


class ReceiptHistoryDatabaseGuardTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Receipt Guard", slug="receipt-guard")
        self.location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )
        self.buyer = User.objects.create(username="guard-buyer", organization=self.organization)
        self.receiver = User.objects.create(
            username="guard-receiver", organization=self.organization
        )
        self.vendor = self.organization.vendor_set.create(code="GUARD", name="Guard Supply")
        self.part = Part.objects.create(
            organization=self.organization,
            number="GUARD-PART",
            name="Guarded part",
            default_unit_cost=Decimal("4.0000"),
        )
        warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="MAIN",
            name="Main Warehouse",
        )
        self.bin = Bin.objects.create(
            organization=self.organization,
            warehouse=warehouse,
            code="A-01",
            name="Guard Bin",
        )

    def _posted_receipt(self) -> Receipt:
        order = create_purchase_order(
            organization=self.organization,
            actor=self.buyer,
            vendor=self.vendor,
            number="PO-GUARD",
            lines=[
                {
                    "part": self.part,
                    "quantity_ordered": Decimal("2.000"),
                    "unit_cost": Decimal("4.0000"),
                }
            ],
        )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.buyer,
            purchase_order=order,
            target=PurchaseOrder.Status.SUBMITTED,
        )
        order = transition_purchase_order(
            organization=self.organization,
            actor=self.buyer,
            purchase_order=order,
            target=PurchaseOrder.Status.SENT,
        )
        return post_receipt(
            organization=self.organization,
            actor=self.receiver,
            purchase_order=order,
            operation_id=uuid.uuid4(),
            lines=[
                {
                    "purchase_order_line": order.lines.get(),
                    "bin": self.bin,
                    "quantity": Decimal("2.000"),
                }
            ],
        )

    def _execute_rejected(self, sql: str, params: list[Any], message: str) -> None:
        with self.assertRaisesMessage(DatabaseError, message):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)

    def test_raw_sql_cannot_rewrite_or_delete_posted_receipt_history(self) -> None:
        receipt = self._posted_receipt()
        line = receipt.lines.get()

        self._execute_rejected(
            "UPDATE purchasing_receiptline SET quantity = %s WHERE id = %s",
            [Decimal("1.000"), line.pk],
            "receipt lines are append-only",
        )
        self._execute_rejected(
            "DELETE FROM purchasing_receiptline WHERE id = %s",
            [line.pk],
            "receipt lines are append-only",
        )
        self._execute_rejected(
            "UPDATE purchasing_receipt SET status = 'Draft' WHERE id = %s",
            [receipt.pk],
            "may only be reversed",
        )
        self._execute_rejected(
            """
            UPDATE purchasing_receipt
               SET status = 'Reversed', reversed_by_id = %s, reversed_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [self.receiver.pk, receipt.pk],
            "may only be reversed",
        )
        self._execute_rejected(
            "DELETE FROM purchasing_receipt WHERE id = %s",
            [receipt.pk],
            "receipts cannot be deleted",
        )

        draft = Receipt.objects.create(
            organization=self.organization,
            purchase_order=receipt.purchase_order,
            number="RCV-INCOMPLETE",
            operation_id=uuid.uuid4(),
            received_by=self.receiver,
        )
        draft_line = draft.lines.create(
            organization=self.organization,
            purchase_order_line=receipt.purchase_order.lines.get(),
            part=self.part,
            bin=self.bin,
            quantity=Decimal("1.000"),
            unit_cost=Decimal("4.0000"),
        )
        self._execute_rejected(
            "UPDATE purchasing_receipt SET status = 'Posted' WHERE id = %s",
            [draft.pk],
            "requires complete, matching stock transactions",
        )
        draft_line.delete()
        self._execute_rejected(
            "UPDATE purchasing_receipt SET status = 'Posted' WHERE id = %s",
            [draft.pk],
            "requires complete, matching stock transactions",
        )

        line.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual(line.quantity, Decimal("2.000"))
        self.assertEqual(receipt.status, Receipt.Status.POSTED)

    def test_posting_and_compensating_reversal_remain_supported(self) -> None:
        receipt = self._posted_receipt()
        reversal_operation = uuid.uuid4()
        reversal = reverse_receipt(
            organization=self.organization,
            actor=self.receiver,
            receipt=receipt,
            operation_id=reversal_operation,
            reason="Receipt belonged to another location",
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, Receipt.Status.REVERSED)
        self.assertEqual(reversal.status, Receipt.Status.POSTED)
        self.assertEqual(reversal.lines.get().quantity, Decimal("-2.000"))
        replayed = reverse_receipt(
            organization=self.organization,
            actor=self.receiver,
            receipt=receipt,
            operation_id=reversal_operation,
            reason="Receipt belonged to another location",
        )
        self.assertEqual(replayed.pk, reversal.pk)
        with self.assertRaisesMessage(DomainError, "already used"):
            reverse_receipt(
                organization=self.organization,
                actor=self.buyer,
                receipt=receipt,
                operation_id=reversal_operation,
                reason="Receipt belonged to another location",
            )
        with self.assertRaisesMessage(DomainError, "already used"):
            reverse_receipt(
                organization=self.organization,
                actor=self.receiver,
                receipt=receipt,
                operation_id=reversal_operation,
                reason="Different reason",
            )

        self._execute_rejected(
            "UPDATE purchasing_receiptline SET quantity = %s WHERE receipt_id = %s",
            [Decimal("-1.000"), reversal.pk],
            "receipt lines are append-only",
        )
        self._execute_rejected(
            "UPDATE purchasing_receipt SET reason = %s WHERE id = %s",
            ["rewrite", receipt.pk],
            "append-only",
        )

    def test_operation_replay_requires_the_same_actor_and_input(self) -> None:
        receipt = self._posted_receipt()
        line = receipt.lines.get()
        replay_lines: list[dict[str, Any]] = [
            {
                "purchase_order_line": line.purchase_order_line,
                "bin": line.bin,
                "quantity": line.quantity,
            }
        ]

        self.assertEqual(
            post_receipt(
                organization=self.organization,
                actor=self.receiver,
                purchase_order=receipt.purchase_order,
                operation_id=receipt.operation_id,
                lines=replay_lines,
            ).pk,
            receipt.pk,
        )
        with self.assertRaisesMessage(DomainError, "already used"):
            post_receipt(
                organization=self.organization,
                actor=self.buyer,
                purchase_order=receipt.purchase_order,
                operation_id=receipt.operation_id,
                lines=replay_lines,
            )
        with self.assertRaisesMessage(DomainError, "already used"):
            post_receipt(
                organization=self.organization,
                actor=self.receiver,
                purchase_order=receipt.purchase_order,
                operation_id=receipt.operation_id,
                lines=replay_lines,
                packing_slip="different",
            )
        replay_lines[0]["quantity"] = Decimal("1.000")
        with self.assertRaisesMessage(DomainError, "already used"):
            post_receipt(
                organization=self.organization,
                actor=self.receiver,
                purchase_order=receipt.purchase_order,
                operation_id=receipt.operation_id,
                lines=replay_lines,
            )

        self.assertEqual(Receipt.objects.count(), 1)
