from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from core.models import OrganizationOwnedModel
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.db.models.base import ModelBase
from django.utils import timezone


def default_approval_threshold() -> Decimal:
    return Decimal(str(settings.PO_APPROVAL_THRESHOLD))


class Vendor(OrganizationOwnedModel):
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    contact_name = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    payment_terms = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_vendor_code_org")
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "code": self.code,
            "name": self.name,
            "contact_name": self.contact_name,
            "email": self.email,
            "phone": self.phone,
            "address": self.address,
            "active": self.active,
            "parts": [
                part.to_dict(include_financial=include_financial) for part in self.parts.all()
            ],
        }
        if include_financial:
            payload["payment_terms"] = self.payment_terms
        return payload


class VendorPart(OrganizationOwnedModel):
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="parts")
    part = models.ForeignKey(
        "inventory.Part", on_delete=models.PROTECT, related_name="vendor_parts"
    )
    vendor_part_number = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    preferred = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["vendor__name", "vendor_part_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "vendor", "vendor_part_number"],
                name="uniq_vendor_part_number_org",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=0), name="vendor_part_cost_nonnegative"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor.code}: {self.vendor_part_number}"

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "vendor_id": str(self.vendor_id),
            "part_id": str(self.part_id),
            "vendor_part_number": self.vendor_part_number,
            "description": self.description,
            "lead_time_days": self.lead_time_days,
            "preferred": self.preferred,
            "active": self.active,
        }
        if include_financial:
            payload["unit_cost"] = format(self.unit_cost, ".4f")
        return payload


class PurchaseRequest(OrganizationOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "Draft"
        SUBMITTED = "Submitted"
        APPROVED = "Approved"
        REJECTED = "Rejected"
        CONVERTED = "Converted"
        CANCELLED = "Cancelled"

    part = models.ForeignKey(
        "inventory.Part", on_delete=models.PROTECT, related_name="purchase_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_requests",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    reason = models.TextField(max_length=1000)
    needed_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_purchase_requests",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="purchase_request_qty_positive"
            )
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "quantity": format(self.quantity, ".3f"),
            "reason": self.reason,
            "needed_by": self.needed_by.isoformat() if self.needed_by else None,
            "status": self.status,
            "requested_by_id": str(self.requested_by_id),
            "approved_by_id": str(self.approved_by_id) if self.approved_by_id else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "created_at": self.created_at.isoformat(),
        }


class PurchaseOrder(OrganizationOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "Draft"
        SUBMITTED = "Submitted"
        APPROVED = "Approved"
        SENT = "Sent"
        PARTIALLY_RECEIVED = "PartiallyReceived"
        RECEIVED = "Received"
        CLOSED = "Closed"
        CANCELLED = "Cancelled"

    number = models.CharField(max_length=50)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_purchase_orders",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submitted_purchase_orders",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    expected_at = models.DateField(null=True, blank=True)
    approval_threshold = models.DecimalField(
        max_digits=14, decimal_places=2, default=default_approval_threshold
    )
    approval_required = models.BooleanField(default=False)
    emergency = models.BooleanField(default=False)
    terms_snapshot = models.CharField(max_length=120, blank=True)
    notes = models.TextField(max_length=2000, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_purchase_order_number_org"
            ),
            models.CheckConstraint(
                condition=models.Q(approval_threshold__gte=0),
                name="purchase_order_threshold_nonnegative",
            ),
        ]

    @property
    def total(self) -> Decimal:
        return sum(
            (line.quantity_ordered * line.unit_cost for line in self.lines.all()),
            Decimal("0"),
        )

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "number": self.number,
            "vendor_id": str(self.vendor_id),
            "vendor_name": self.vendor.name,
            "status": self.status,
            "approval_required": self.approval_required,
            "created_by_id": str(self.created_by_id),
            "submitted_by_id": str(self.submitted_by_id) if self.submitted_by_id else None,
            "approved_by_id": str(self.approved_by_id) if self.approved_by_id else None,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "expected_at": self.expected_at.isoformat() if self.expected_at else None,
            "emergency": self.emergency,
            "lines": [
                line.to_dict(include_financial=include_financial) for line in self.lines.all()
            ],
            "created_at": self.created_at.isoformat(),
        }
        if include_financial:
            payload["total"] = str(self.total)
            payload["approval_threshold"] = str(self.approval_threshold)
            payload["terms_snapshot"] = self.terms_snapshot
            payload["notes"] = self.notes
        return payload


class PurchaseOrderLine(OrganizationOwnedModel):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="lines"
    )
    part = models.ForeignKey(
        "inventory.Part", on_delete=models.PROTECT, related_name="purchase_order_lines"
    )
    purchase_request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_order_lines",
    )
    description = models.CharField(max_length=255)
    vendor_part_number = models.CharField(max_length=100, blank=True)
    quantity_ordered = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_ordered__gt=0),
                name="purchase_order_line_qty_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gte=0),
                name="purchase_order_line_cost_nonnegative",
            ),
        ]

    @property
    def quantity_received(self) -> Decimal:
        return self.receipt_lines.exclude(receipt__status=Receipt.Status.DRAFT).aggregate(
            total=Sum("quantity")
        )["total"] or Decimal("0")

    @property
    def quantity_remaining(self) -> Decimal:
        return self.quantity_ordered - self.quantity_received

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "purchase_request_id": (
                str(self.purchase_request_id) if self.purchase_request_id else None
            ),
            "description": self.description,
            "vendor_part_number": self.vendor_part_number,
            "quantity_ordered": format(self.quantity_ordered, ".3f"),
            "quantity_received": format(self.quantity_received, ".3f"),
            "quantity_remaining": format(self.quantity_remaining, ".3f"),
        }
        if include_financial:
            payload["unit_cost"] = format(self.unit_cost, ".4f")
            payload["line_total"] = format(self.quantity_ordered * self.unit_cost, ".4f")
        return payload


class Receipt(OrganizationOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "Draft"
        POSTED = "Posted"
        REVERSED = "Reversed"

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="receipts"
    )
    number = models.CharField(max_length=80)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    operation_id = models.UUIDField()
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="purchase_receipts",
    )
    received_at = models.DateTimeField(default=timezone.now)
    packing_slip = models.CharField(max_length=120, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
    )
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_purchase_receipts",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"],
                name="uniq_receipt_operation_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_receipt_number_org"
            ),
        ]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "number": self.number,
            "purchase_order_id": str(self.purchase_order_id),
            "purchase_order_number": self.purchase_order.number,
            "status": self.status,
            "operation_id": str(self.operation_id),
            "received_by_id": str(self.received_by_id),
            "received_at": self.received_at.isoformat(),
            "packing_slip": self.packing_slip,
            "reason": self.reason,
            "reversal_of_id": str(self.reversal_of_id) if self.reversal_of_id else None,
            "reversed_by_id": str(self.reversed_by_id) if self.reversed_by_id else None,
            "reversed_at": self.reversed_at.isoformat() if self.reversed_at else None,
            "lines": [
                line.to_dict(include_financial=include_financial) for line in self.lines.all()
            ],
        }


class ReceiptLine(OrganizationOwnedModel):
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="lines")
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines"
    )
    part = models.ForeignKey(
        "inventory.Part", on_delete=models.PROTECT, related_name="receipt_lines"
    )
    bin = models.ForeignKey("inventory.Bin", on_delete=models.PROTECT, related_name="receipt_lines")
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    stock_transaction = models.OneToOneField(
        "inventory.StockTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipt_line",
    )
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(quantity=0), name="receipt_line_qty_nonzero")
        ]

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if self._state.adding and self.receipt.status != Receipt.Status.DRAFT:
            raise ValidationError("Lines may only be added to a draft receipt")
        if not self._state.adding:
            original = type(self).objects.select_related("receipt").get(pk=self.pk)
            if original.receipt.status != Receipt.Status.DRAFT:
                raise ValidationError("Posted receipt lines are append-only")
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        if self.receipt.status != Receipt.Status.DRAFT:
            raise ValidationError("Posted receipt lines cannot be deleted")
        return super().delete(using=using, keep_parents=keep_parents)

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "purchase_order_line_id": str(self.purchase_order_line_id),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "bin_id": str(self.bin_id),
            "bin_code": self.bin.code,
            "quantity": format(self.quantity, ".3f"),
            "stock_transaction_id": (
                str(self.stock_transaction_id) if self.stock_transaction_id else None
            ),
            "reversal_of_id": str(self.reversal_of_id) if self.reversal_of_id else None,
        }
        if include_financial:
            payload["unit_cost"] = format(self.unit_cost, ".4f")
        return payload
