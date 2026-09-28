from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from core.models import ImmutableModel, OrganizationOwnedModel
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


def default_count_variance_approval_threshold() -> Decimal:
    return Decimal(str(settings.INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD))


class Part(OrganizationOwnedModel):
    number = models.CharField(max_length=80)
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    manufacturer_number = models.CharField(max_length=120, blank=True)
    unit_of_measure = models.CharField(max_length=30, default="each")
    barcode = models.CharField(max_length=120, blank=True)
    default_unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_part_number_org"),
            models.UniqueConstraint(
                fields=["organization", "barcode"],
                condition=~Q(barcode=""),
                name="uniq_part_barcode_org",
            ),
            models.CheckConstraint(
                condition=Q(default_unit_cost__gte=0), name="part_default_cost_nonnegative"
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.number = self.number.strip().upper()
        self.barcode = self.barcode.strip()
        super().save(*args, **kwargs)

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "number": self.number,
            "name": self.name,
            "description": self.description,
            "manufacturer": self.manufacturer,
            "manufacturer_number": self.manufacturer_number,
            "unit_of_measure": self.unit_of_measure,
            "barcode": self.barcode,
            "active": self.active,
        }
        if include_financial:
            payload["default_unit_cost"] = format(self.default_unit_cost, ".4f")
        return payload

    def __str__(self) -> str:
        return f"{self.number} — {self.name}"


class PartCrossReference(OrganizationOwnedModel):
    class Kind(models.TextChoices):
        ALTERNATE = "ALTERNATE", "Alternate"
        MANUFACTURER = "MANUFACTURER", "Manufacturer"
        VENDOR = "VENDOR", "Vendor"
        BARCODE = "BARCODE", "Barcode"

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="cross_references")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ALTERNATE)
    value = models.CharField(max_length=120)
    value_normalized = models.CharField(max_length=120, editable=False)

    class Meta:
        ordering = ["value_normalized"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "value_normalized"],
                name="uniq_part_cross_ref_org",
            )
        ]

    def clean(self) -> None:
        if self.part_id and self.organization_id != self.part.organization_id:
            raise ValidationError("Part cross-reference must belong to the same organization")

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.value = self.value.strip()
        self.value_normalized = self.value.upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.value} → {self.part.number}"


class Warehouse(OrganizationOwnedModel):
    location = models.ForeignKey(
        "core.Location", on_delete=models.PROTECT, related_name="warehouses"
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_warehouse_code_org")
        ]

    def clean(self) -> None:
        if self.location_id and self.organization_id != self.location.organization_id:
            raise ValidationError("Warehouse location must belong to the same organization")

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.code = self.code.strip().upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "code": self.code,
            "name": self.name,
            "location_id": str(self.location_id),
            "active": self.active,
        }

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Bin(OrganizationOwnedModel):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="bins")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["warehouse__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["warehouse", "code"], name="uniq_bin_code_warehouse")
        ]

    def clean(self) -> None:
        if self.warehouse_id and self.organization_id != self.warehouse.organization_id:
            raise ValidationError("Bin warehouse must belong to the same organization")

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.code = self.code.strip().upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "warehouse_id": str(self.warehouse_id),
            "warehouse_code": self.warehouse.code,
            "code": self.code,
            "name": self.name,
            "active": self.active,
        }

    def __str__(self) -> str:
        return f"{self.warehouse.code}/{self.code}"


class StockBalance(OrganizationOwnedModel):
    """Locked projection of the immutable stock ledger; never edit outside inventory services."""

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="balances")
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, related_name="balances")
    quantity_on_hand = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    quantity_reserved = models.DecimalField(max_digits=14, decimal_places=3, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["part", "bin"], name="uniq_stock_balance_part_bin"),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0), name="stock_on_hand_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(quantity_reserved__gte=0), name="stock_reserved_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(quantity_reserved__lte=F("quantity_on_hand")),
                name="stock_reserved_lte_on_hand",
            ),
        ]

    @property
    def available_quantity(self) -> Decimal:
        return self.quantity_on_hand - self.quantity_reserved

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "bin_id": str(self.bin_id),
            "bin_code": str(self.bin),
            "quantity_on_hand": format(self.quantity_on_hand, ".3f"),
            "quantity_reserved": format(self.quantity_reserved, ".3f"),
            "available_quantity": format(self.available_quantity, ".3f"),
        }


class StockTransaction(ImmutableModel):
    class Type(models.TextChoices):
        RECEIPT = "RECEIPT", "Receipt"
        ISSUE = "ISSUE", "Issue"
        RETURN = "RETURN", "Return"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT", "Count adjustment"
        REVERSAL = "REVERSAL", "Reversal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, related_name="stock_transactions"
    )
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="transactions")
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, related_name="transactions")
    transaction_type = models.CharField(max_length=24, choices=Type.choices)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    total_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )
    reservation = models.ForeignKey(
        "Reservation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    original_transaction = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="compensating_transactions",
    )
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    operation_id = models.UUIDField()
    actor = models.ForeignKey(
        "core.User", on_delete=models.PROTECT, related_name="stock_transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"],
                name="uniq_stock_transaction_operation",
            ),
            models.CheckConstraint(condition=~Q(quantity=0), name="stock_tx_quantity_nonzero"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="stock_tx_cost_nonnegative"),
        ]
        indexes = [
            models.Index(fields=["organization", "part", "bin", "created_at"]),
            models.Index(fields=["organization", "work_order", "created_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "operation_id": str(self.operation_id),
            "type": self.transaction_type,
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "bin_id": str(self.bin_id),
            "bin_code": str(self.bin),
            "quantity": format(self.quantity, ".3f"),
            "work_order_id": str(self.work_order_id) if self.work_order_id else None,
            "reservation_id": str(self.reservation_id) if self.reservation_id else None,
            "original_transaction_id": (
                str(self.original_transaction_id) if self.original_transaction_id else None
            ),
            "reference_type": self.reference_type,
            "reference_id": self.reference_id,
            "reason": self.reason,
            "actor_id": str(self.actor_id),
            "created_at": self.created_at,
        }
        if include_financial:
            payload["unit_cost"] = format(self.unit_cost, ".4f")
            payload["total_cost"] = format(self.total_cost, ".4f")
        return payload


class Reservation(OrganizationOwnedModel):
    class Status(models.TextChoices):
        PENDING = "Pending", "Pending"
        ACTIVE = "Active", "Active"
        PARTIALLY_ISSUED = "PartiallyIssued", "Partially issued"
        FULFILLED = "Fulfilled", "Fulfilled"
        RELEASED = "Released", "Released"
        EXPIRED = "Expired", "Expired"

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="reservations")
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, related_name="reservations")
    work_order = models.ForeignKey(
        "maintenance.WorkOrder", on_delete=models.PROTECT, related_name="part_reservations"
    )
    requested_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    issued_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    released_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.ACTIVE)
    operation_id = models.UUIDField()
    created_by = models.ForeignKey(
        "core.User", on_delete=models.PROTECT, related_name="stock_reservations"
    )
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="uniq_reservation_operation"
            ),
            models.CheckConstraint(
                condition=Q(requested_quantity__gt=0), name="reservation_requested_positive"
            ),
            models.CheckConstraint(
                condition=Q(issued_quantity__gte=0), name="reservation_issued_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(released_quantity__gte=0), name="reservation_released_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(issued_quantity__lte=F("requested_quantity") - F("released_quantity")),
                name="reservation_consumed_lte_requested",
            ),
        ]

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.issued_quantity - self.released_quantity

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "operation_id": str(self.operation_id),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "bin_id": str(self.bin_id),
            "bin_code": str(self.bin),
            "work_order_id": str(self.work_order_id),
            "requested_quantity": format(self.requested_quantity, ".3f"),
            "issued_quantity": format(self.issued_quantity, ".3f"),
            "released_quantity": format(self.released_quantity, ".3f"),
            "remaining_quantity": format(self.remaining_quantity, ".3f"),
            "status": self.status,
            "reason": self.reason,
        }


class InventoryCount(OrganizationOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        PENDING_APPROVAL = "PendingApproval", "Pending approval"
        POSTED = "Posted", "Posted"

    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="inventory_counts"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    operation_id = models.UUIDField()
    reason = models.CharField(max_length=500)
    total_variance_value = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    approval_threshold = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=default_count_variance_approval_threshold,
    )
    approval_required = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        "core.User", on_delete=models.PROTECT, related_name="created_inventory_counts"
    )
    approved_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_inventory_counts",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="posted_inventory_counts",
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="uniq_inventory_count_operation"
            ),
            models.CheckConstraint(
                condition=Q(total_variance_value__gte=0),
                name="inventory_count_variance_value_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(approval_threshold__gte=0),
                name="inventory_count_approval_threshold_nonnegative",
            ),
        ]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "operation_id": str(self.operation_id),
            "warehouse_id": str(self.warehouse_id),
            "warehouse_code": self.warehouse.code,
            "status": self.status,
            "reason": self.reason,
            "approval_required": self.approval_required,
            "created_by_id": str(self.created_by_id),
            "approved_by_id": str(self.approved_by_id) if self.approved_by_id else None,
            "approved_at": self.approved_at,
            "posted_by_id": str(self.posted_by_id) if self.posted_by_id else None,
            "posted_at": self.posted_at,
            "lines": [
                line.to_dict(include_financial=include_financial)
                for line in self.lines.select_related("part", "bin")
            ],
        }
        if include_financial:
            payload["total_variance_value"] = format(self.total_variance_value, ".4f")
            payload["approval_threshold"] = str(self.approval_threshold)
        return payload


class InventoryCountLine(OrganizationOwnedModel):
    inventory_count = models.ForeignKey(
        InventoryCount, on_delete=models.PROTECT, related_name="lines"
    )
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="count_lines")
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT, related_name="count_lines")
    expected_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    counted_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    variance = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost_snapshot = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    adjustment_transaction = models.OneToOneField(
        StockTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="count_line",
    )

    class Meta:
        ordering = ["bin__code", "part__number"]
        constraints = [
            models.UniqueConstraint(
                fields=["inventory_count", "part", "bin"], name="uniq_count_line_part_bin"
            ),
            models.CheckConstraint(
                condition=Q(expected_quantity__gte=0), name="count_expected_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(counted_quantity__gte=0), name="counted_quantity_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(unit_cost_snapshot__gte=0),
                name="count_line_unit_cost_nonnegative",
            ),
        ]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": str(self.pk),
            "part_id": str(self.part_id),
            "part_number": self.part.number,
            "bin_id": str(self.bin_id),
            "bin_code": str(self.bin),
            "expected_quantity": format(self.expected_quantity, ".3f"),
            "counted_quantity": format(self.counted_quantity, ".3f"),
            "variance": str(self.variance),
            "adjustment_transaction_id": (
                str(self.adjustment_transaction_id) if self.adjustment_transaction_id else None
            ),
        }
        if include_financial:
            payload["unit_cost_snapshot"] = format(self.unit_cost_snapshot, ".4f")
        return payload
