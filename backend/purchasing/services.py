from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, cast

from core.exceptions import DomainError
from core.models import Organization, User
from core.services import audit, emit
from django.db import transaction
from django.utils import timezone
from inventory.models import Bin, Part

from .models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorPart,
)


def _require_same_organization(organization: Organization, *objects: object) -> None:
    if any(getattr(value, "organization_id", None) != organization.pk for value in objects):
        raise DomainError(
            "Referenced records must belong to the same organization",
            code="organization_mismatch",
            status=404,
        )


def _receipt_lines_signature(lines: list[dict[str, Any]]) -> list[tuple[str, str, Decimal]]:
    return sorted(
        (
            str(cast(PurchaseOrderLine, item["purchase_order_line"]).pk),
            str(cast(Bin, item["bin"]).pk),
            Decimal(str(item["quantity"])),
        )
        for item in lines
    )


@transaction.atomic
def create_vendor(
    *,
    organization: Organization,
    actor: User,
    code: str,
    name: str,
    record_id: uuid.UUID | None = None,
    **details: Any,
) -> Vendor:
    if not code.strip() or not name.strip():
        raise DomainError("Vendor code and name are required", code="invalid_vendor")
    vendor = Vendor.objects.create(
        **({"id": record_id} if record_id is not None else {}),
        organization=organization,
        code=code.strip().upper(),
        name=name.strip(),
        **details,
    )
    audit(
        organization=organization,
        actor=actor,
        action="vendor.created",
        resource=vendor,
    )
    return vendor


@transaction.atomic
def create_purchase_request(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    quantity: Decimal,
    reason: str,
    needed_by: date | None = None,
    record_id: uuid.UUID | None = None,
) -> PurchaseRequest:
    _require_same_organization(organization, actor, part)
    if quantity <= 0 or not reason.strip():
        raise DomainError(
            "A positive quantity and reason are required", code="invalid_purchase_request"
        )
    request = PurchaseRequest.objects.create(
        **({"id": record_id} if record_id is not None else {}),
        organization=organization,
        requested_by=actor,
        part=part,
        quantity=quantity,
        reason=reason.strip(),
        needed_by=needed_by,
    )
    audit(
        organization=organization,
        actor=actor,
        action="purchase_request.submitted",
        resource=request,
        previous_state="",
        new_state=request.status,
    )
    return request


@transaction.atomic
def transition_purchase_request(
    *,
    organization: Organization,
    actor: User,
    purchase_request: PurchaseRequest,
    target: str,
    reason: str = "",
) -> PurchaseRequest:
    _require_same_organization(organization, actor, purchase_request)
    purchase_request = PurchaseRequest.objects.select_for_update().get(
        pk=purchase_request.pk, organization=organization
    )
    previous = purchase_request.status
    reason = reason.strip()

    if target == PurchaseRequest.Status.APPROVED:
        if previous != PurchaseRequest.Status.SUBMITTED:
            raise DomainError(
                "Only a submitted purchase request can be approved",
                code="invalid_transition",
            )
        if purchase_request.requested_by_id == actor.pk:
            raise DomainError(
                "The requester cannot approve their own purchase request",
                code="separation_of_duties",
                status=403,
            )
        purchase_request.status = PurchaseRequest.Status.APPROVED
        purchase_request.approved_by = actor
        purchase_request.approved_at = timezone.now()
    elif target == PurchaseRequest.Status.REJECTED:
        if previous != PurchaseRequest.Status.SUBMITTED:
            raise DomainError(
                "Only a submitted purchase request can be rejected",
                code="invalid_transition",
            )
        if not reason:
            raise DomainError("Rejection requires a reason", code="reason_required")
        purchase_request.status = PurchaseRequest.Status.REJECTED
    elif target == PurchaseRequest.Status.CANCELLED:
        if previous not in {
            PurchaseRequest.Status.SUBMITTED,
            PurchaseRequest.Status.APPROVED,
        }:
            raise DomainError(
                "This purchase request can no longer be cancelled",
                code="invalid_transition",
            )
        if not reason:
            raise DomainError("Cancellation requires a reason", code="reason_required")
        purchase_request.status = PurchaseRequest.Status.CANCELLED
    else:
        raise DomainError("Unsupported purchase-request transition", code="invalid_transition")

    purchase_request.save()
    audit(
        organization=organization,
        actor=actor,
        action=f"purchase_request.{purchase_request.status.lower()}",
        resource=purchase_request,
        previous_state=previous,
        new_state=purchase_request.status,
        context={"reason": reason},
    )
    return purchase_request


@transaction.atomic
def create_purchase_order(
    *,
    organization: Organization,
    actor: User,
    vendor: Vendor,
    lines: list[dict[str, Any]],
    number: str = "",
    expected_at: date | None = None,
    emergency: bool = False,
    notes: str = "",
    record_id: uuid.UUID | None = None,
) -> PurchaseOrder:
    _require_same_organization(organization, actor, vendor)
    if not vendor.active:
        raise DomainError("Inactive vendors cannot receive new orders", code="vendor_inactive")
    if not lines:
        raise DomainError("A purchase order needs at least one line", code="lines_required")
    purchase_order = PurchaseOrder.objects.create(
        **({"id": record_id} if record_id is not None else {}),
        organization=organization,
        number=number.strip() or f"PO-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        vendor=vendor,
        created_by=actor,
        expected_at=expected_at,
        emergency=emergency,
        terms_snapshot=vendor.payment_terms,
        notes=notes.strip(),
    )
    seen_requests: list[PurchaseRequest] = []
    seen_request_ids: set[object] = set()
    for line_index, item in enumerate(lines):
        part = cast(Part, item["part"])
        purchase_request = cast(PurchaseRequest | None, item.get("purchase_request"))
        quantity = Decimal(str(item["quantity_ordered"]))
        unit_cost = Decimal(str(item["unit_cost"]))
        _require_same_organization(
            organization,
            part,
            *([purchase_request] if purchase_request else []),
        )
        if purchase_request:
            if purchase_request.pk in seen_request_ids:
                raise DomainError(
                    "A purchase request can only be linked once",
                    code="duplicate_purchase_request",
                    status=409,
                )
            seen_request_ids.add(purchase_request.pk)
            purchase_request = PurchaseRequest.objects.select_for_update().get(
                pk=purchase_request.pk, organization=organization
            )
        if purchase_request and purchase_request.part_id != part.pk:
            raise DomainError(
                "Purchase request and order line must reference the same part",
                code="purchase_request_part_mismatch",
            )
        if purchase_request and purchase_request.status != PurchaseRequest.Status.APPROVED:
            raise DomainError(
                "Only an approved purchase request can be converted",
                code="purchase_request_not_approved",
                status=409,
            )
        if quantity <= 0 or unit_cost < 0:
            raise DomainError(
                "Order quantity must be positive and cost cannot be negative",
                code="invalid_purchase_order_line",
            )
        PurchaseOrderLine.objects.create(
            **(
                {"id": uuid.uuid5(record_id, f"line:{line_index}")} if record_id is not None else {}
            ),
            organization=organization,
            purchase_order=purchase_order,
            part=part,
            purchase_request=purchase_request,
            description=str(item.get("description") or part.name).strip(),
            vendor_part_number=str(item.get("vendor_part_number", "")).strip(),
            quantity_ordered=quantity,
            unit_cost=unit_cost,
        )
        vendor_part_number = str(item.get("vendor_part_number", "")).strip()
        if vendor_part_number:
            vendor_part, created = VendorPart.objects.get_or_create(
                organization=organization,
                vendor=vendor,
                vendor_part_number=vendor_part_number,
                defaults={
                    "id": uuid.uuid5(vendor.pk, f"vendor-part:{vendor_part_number}"),
                    "part": part,
                    "description": str(item.get("description") or part.name).strip(),
                    "unit_cost": unit_cost,
                },
            )
            if not created and vendor_part.part_id != part.pk:
                raise DomainError(
                    "Vendor part number is already assigned to another part",
                    code="vendor_part_conflict",
                )
            if created:
                audit(
                    organization=organization,
                    actor=actor,
                    action="vendor_part.created",
                    resource=vendor_part,
                )
            if not created and vendor_part.unit_cost != unit_cost:
                previous_cost = vendor_part.unit_cost
                vendor_part.unit_cost = unit_cost
                vendor_part.save(update_fields=["unit_cost", "updated_at"])
                audit(
                    organization=organization,
                    actor=actor,
                    action="vendor_part.price_updated",
                    resource=vendor_part,
                    context={
                        "previous_unit_cost": str(previous_cost),
                        "unit_cost": str(unit_cost),
                    },
                )
        if purchase_request:
            seen_requests.append(purchase_request)
    for request in seen_requests:
        previous = request.status
        request.status = PurchaseRequest.Status.CONVERTED
        request.save(update_fields=["status", "updated_at"])
        audit(
            organization=organization,
            actor=actor,
            action="purchase_request.converted",
            resource=request,
            previous_state=previous,
            new_state=request.status,
            context={"purchase_order_id": str(purchase_order.pk)},
        )
    audit(
        organization=organization,
        actor=actor,
        action="purchase_order.created",
        resource=purchase_order,
        new_state=purchase_order.status,
        context={"total": str(purchase_order.total)},
    )
    return purchase_order


@transaction.atomic
def transition_purchase_order(
    *,
    organization: Organization,
    actor: User,
    purchase_order: PurchaseOrder,
    target: str,
    reason: str = "",
) -> PurchaseOrder:
    _require_same_organization(organization, purchase_order)
    purchase_order = PurchaseOrder.objects.select_for_update().get(
        pk=purchase_order.pk, organization=organization
    )
    previous = purchase_order.status
    now = timezone.now()

    if target == PurchaseOrder.Status.SUBMITTED:
        if previous != PurchaseOrder.Status.DRAFT or not purchase_order.lines.exists():
            raise DomainError("Only a populated draft can be submitted", code="invalid_transition")
        purchase_order.submitted_by = actor
        purchase_order.submitted_at = now
        purchase_order.approval_required = purchase_order.total >= purchase_order.approval_threshold
        if purchase_order.approval_required:
            purchase_order.status = PurchaseOrder.Status.SUBMITTED
        else:
            purchase_order.status = PurchaseOrder.Status.APPROVED
            purchase_order.approved_by = actor
            purchase_order.approved_at = now
    elif target == PurchaseOrder.Status.APPROVED:
        if previous != PurchaseOrder.Status.SUBMITTED:
            raise DomainError("Only a submitted order can be approved", code="invalid_transition")
        if purchase_order.created_by_id == actor.pk or purchase_order.submitted_by_id == actor.pk:
            raise DomainError(
                "The submitter cannot approve an order requiring approval",
                code="separation_of_duties",
                status=403,
            )
        purchase_order.status = PurchaseOrder.Status.APPROVED
        purchase_order.approved_by = actor
        purchase_order.approved_at = now
    elif target == PurchaseOrder.Status.SENT:
        if previous != PurchaseOrder.Status.APPROVED:
            raise DomainError("Only an approved order can be sent", code="invalid_transition")
        purchase_order.status = PurchaseOrder.Status.SENT
        purchase_order.sent_at = now
    elif target == PurchaseOrder.Status.CLOSED:
        if previous != PurchaseOrder.Status.RECEIVED:
            raise DomainError(
                "Only a fully received order can be closed", code="invalid_transition"
            )
        purchase_order.status = PurchaseOrder.Status.CLOSED
    elif target == PurchaseOrder.Status.CANCELLED:
        if previous not in {
            PurchaseOrder.Status.DRAFT,
            PurchaseOrder.Status.SUBMITTED,
            PurchaseOrder.Status.APPROVED,
            PurchaseOrder.Status.SENT,
        }:
            raise DomainError("This order can no longer be cancelled", code="invalid_transition")
        if not reason.strip():
            raise DomainError("Cancellation requires a reason", code="reason_required")
        purchase_order.status = PurchaseOrder.Status.CANCELLED
    else:
        raise DomainError("Unsupported purchase-order transition", code="invalid_transition")

    purchase_order.save()
    action = f"purchase_order.{purchase_order.status.lower()}"
    audit(
        organization=organization,
        actor=actor,
        action=action,
        resource=purchase_order,
        previous_state=previous,
        new_state=purchase_order.status,
        context={"reason": reason.strip(), "approval_required": purchase_order.approval_required},
    )
    if purchase_order.status == PurchaseOrder.Status.APPROVED:
        emit(
            organization=organization,
            event_type="purchase_order.approved",
            resource=purchase_order,
            payload={"number": purchase_order.number, "total": str(purchase_order.total)},
        )
    return purchase_order


def _update_received_status(
    *, organization: Organization, actor: User, purchase_order: PurchaseOrder
) -> None:
    previous = purchase_order.status
    lines = list(purchase_order.lines.all())
    received = [line.quantity_received for line in lines]
    if lines and all(
        value >= line.quantity_ordered for value, line in zip(received, lines, strict=True)
    ):
        purchase_order.status = PurchaseOrder.Status.RECEIVED
    elif any(value > 0 for value in received):
        purchase_order.status = PurchaseOrder.Status.PARTIALLY_RECEIVED
    else:
        purchase_order.status = (
            PurchaseOrder.Status.SENT if purchase_order.sent_at else PurchaseOrder.Status.APPROVED
        )
    if purchase_order.status != previous:
        purchase_order.save(update_fields=["status", "updated_at"])
        audit(
            organization=organization,
            actor=actor,
            action="purchase_order.receipt_status_changed",
            resource=purchase_order,
            previous_state=previous,
            new_state=purchase_order.status,
        )


@transaction.atomic
def post_receipt(
    *,
    organization: Organization,
    actor: User,
    purchase_order: PurchaseOrder,
    lines: list[dict[str, Any]],
    operation_id: uuid.UUID,
    packing_slip: str = "",
    record_id: uuid.UUID | None = None,
) -> Receipt:
    from inventory.services import receive_stock

    _require_same_organization(organization, actor, purchase_order)
    purchase_order = PurchaseOrder.objects.select_for_update().get(
        pk=purchase_order.pk, organization=organization
    )
    existing = Receipt.objects.filter(organization=organization, operation_id=operation_id).first()
    if existing:
        recorded_lines = sorted(
            (str(order_line_id), str(bin_id), quantity)
            for order_line_id, bin_id, quantity in existing.lines.values_list(
                "purchase_order_line_id", "bin_id", "quantity"
            )
        )
        if (
            existing.purchase_order_id != purchase_order.pk
            or existing.reversal_of_id
            or existing.received_by_id != actor.pk
            or existing.packing_slip != packing_slip.strip()
            or recorded_lines != _receipt_lines_signature(lines)
        ):
            raise DomainError(
                "This operation_id was already used for another receipt",
                code="idempotency_conflict",
                status=409,
            )
        return existing
    if purchase_order.status not in {
        PurchaseOrder.Status.SENT,
        PurchaseOrder.Status.PARTIALLY_RECEIVED,
    }:
        raise DomainError("Only sent orders can be received", code="purchase_order_not_receivable")
    if not lines:
        raise DomainError("A receipt needs at least one line", code="lines_required")

    order_lines = {
        line.pk: line
        for line in PurchaseOrderLine.objects.select_for_update().filter(
            purchase_order=purchase_order
        )
    }
    requested: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    prepared: list[tuple[PurchaseOrderLine, Bin, Decimal]] = []
    for item in lines:
        line = order_lines.get(item["purchase_order_line"].pk)
        stock_bin = cast(Bin, item["bin"])
        quantity = Decimal(str(item["quantity"]))
        if line is None:
            raise DomainError(
                "Receipt line is not on this order", code="purchase_order_line_mismatch"
            )
        _require_same_organization(organization, line, line.part, stock_bin)
        if quantity <= 0:
            raise DomainError("Received quantity must be positive", code="invalid_receipt_quantity")
        requested[line.pk] += quantity
        prepared.append((line, stock_bin, quantity))
    for line_id, quantity in requested.items():
        if quantity > order_lines[line_id].quantity_remaining:
            raise DomainError(
                "Received quantity exceeds the open order quantity",
                code="over_receipt",
                details={
                    "purchase_order_line_id": str(line_id),
                    "remaining": str(order_lines[line_id].quantity_remaining),
                    "attempted": str(quantity),
                },
            )

    # Every receipt that spans multiple balances must acquire those row locks in
    # the same order. Otherwise two unrelated purchase orders with reversed line
    # order can each hold one balance lock while waiting for the other.
    prepared.sort(key=lambda row: (str(row[1].pk), str(row[0].part_id), str(row[0].pk)))

    receipt = Receipt.objects.create(
        **({"id": record_id} if record_id is not None else {}),
        organization=organization,
        purchase_order=purchase_order,
        number=f"RCV-{purchase_order.number}-{str(operation_id)[:8].upper()}",
        operation_id=operation_id,
        received_by=actor,
        packing_slip=packing_slip.strip(),
    )
    for line_index, (order_line, stock_bin, quantity) in enumerate(prepared):
        receipt_line = ReceiptLine.objects.create(
            **(
                {"id": uuid.uuid5(record_id, f"line:{line_index}")} if record_id is not None else {}
            ),
            organization=organization,
            receipt=receipt,
            purchase_order_line=order_line,
            part=order_line.part,
            bin=stock_bin,
            quantity=quantity,
            unit_cost=order_line.unit_cost,
        )
        stock_transaction = receive_stock(
            organization=organization,
            actor=actor,
            part=order_line.part,
            bin=stock_bin,
            quantity=quantity,
            unit_cost=order_line.unit_cost,
            operation_id=uuid.uuid5(operation_id, f"receipt-line:{receipt_line.pk}"),
            reference_type="receipt_line",
            reference_id=str(receipt_line.pk),
            reason=f"Receipt {receipt.number}",
        )
        receipt_line.stock_transaction = stock_transaction
        receipt_line.save(update_fields=["stock_transaction", "updated_at"])
    receipt.status = Receipt.Status.POSTED
    receipt.save(update_fields=["status", "updated_at"])
    _update_received_status(organization=organization, actor=actor, purchase_order=purchase_order)
    audit(
        organization=organization,
        actor=actor,
        action="receipt.posted",
        resource=receipt,
        previous_state=Receipt.Status.DRAFT,
        new_state=Receipt.Status.POSTED,
        context={"purchase_order_id": str(purchase_order.pk)},
    )
    emit(
        organization=organization,
        event_type="receipt.posted",
        resource=receipt,
        payload={"purchase_order_id": str(purchase_order.pk), "number": receipt.number},
    )
    return receipt


@transaction.atomic
def reverse_receipt(
    *,
    organization: Organization,
    actor: User,
    receipt: Receipt,
    operation_id: uuid.UUID,
    reason: str,
) -> Receipt:
    from inventory.services import reverse_transaction

    if not reason.strip():
        raise DomainError("Receipt reversal requires a reason", code="reason_required")
    _require_same_organization(organization, actor, receipt)
    receipt = (
        Receipt.objects.select_for_update()
        .select_related("purchase_order")
        .get(pk=receipt.pk, organization=organization)
    )
    existing = Receipt.objects.filter(organization=organization, operation_id=operation_id).first()
    if existing:
        if (
            existing.reversal_of_id != receipt.pk
            or existing.received_by_id != actor.pk
            or existing.reason != reason.strip()
        ):
            raise DomainError(
                "This operation_id was already used for another receipt",
                code="idempotency_conflict",
                status=409,
            )
        return existing
    if receipt.status != Receipt.Status.POSTED or receipt.reversal_of_id:
        raise DomainError(
            "Only an unreversed posted receipt can be reversed", code="invalid_reversal"
        )
    purchase_order = PurchaseOrder.objects.select_for_update().get(
        pk=receipt.purchase_order_id, organization=organization
    )
    reversal = Receipt.objects.create(
        organization=organization,
        purchase_order=purchase_order,
        number=f"REV-{receipt.number}-{str(operation_id)[:8].upper()}",
        operation_id=operation_id,
        received_by=actor,
        reason=reason.strip(),
        reversal_of=receipt,
    )
    for original_line in receipt.lines.select_related("part", "bin", "stock_transaction"):
        original_transaction = original_line.stock_transaction
        if original_transaction is None:
            raise DomainError("Receipt stock transaction is missing", code="receipt_not_reversible")
        reversal_line = ReceiptLine.objects.create(
            organization=organization,
            receipt=reversal,
            purchase_order_line=original_line.purchase_order_line,
            part=original_line.part,
            bin=original_line.bin,
            quantity=-original_line.quantity,
            unit_cost=original_line.unit_cost,
            reversal_of=original_line,
        )
        stock_transaction = reverse_transaction(
            organization=organization,
            actor=actor,
            original=original_transaction,
            operation_id=uuid.uuid5(operation_id, f"receipt-reversal:{reversal_line.pk}"),
            reason=reason.strip(),
        )
        reversal_line.stock_transaction = stock_transaction
        reversal_line.save(update_fields=["stock_transaction", "updated_at"])
    reversal.status = Receipt.Status.POSTED
    reversal.save(update_fields=["status", "updated_at"])
    receipt.status = Receipt.Status.REVERSED
    receipt.reversed_by = actor
    receipt.reversed_at = timezone.now()
    receipt.save(update_fields=["status", "reversed_by", "reversed_at", "updated_at"])
    _update_received_status(organization=organization, actor=actor, purchase_order=purchase_order)
    audit(
        organization=organization,
        actor=actor,
        action="receipt.reversed",
        resource=receipt,
        previous_state=Receipt.Status.POSTED,
        new_state=Receipt.Status.REVERSED,
        context={"reversal_id": str(reversal.pk), "reason": reason.strip()},
    )
    emit(
        organization=organization,
        event_type="receipt.reversed",
        resource=receipt,
        payload={"reversal_id": str(reversal.pk), "reason": reason.strip()},
    )
    return reversal
