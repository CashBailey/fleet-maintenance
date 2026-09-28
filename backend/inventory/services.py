from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from core.exceptions import DomainError
from core.models import Organization, User
from core.permissions import (
    can_manage_financials,
    can_view_financials,
    has_permission,
    permissions_for,
)
from core.services import audit, emit
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    Bin,
    InventoryCount,
    InventoryCountLine,
    Part,
    Reservation,
    StockBalance,
    StockTransaction,
    Warehouse,
)


def parse_quantity(value: object, field: str = "quantity", *, allow_zero: bool = False) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainError(f"{field} must be a number", code=f"invalid_{field}") from exc
    if not quantity.is_finite() or (quantity == 0 and not allow_zero):
        raise DomainError(f"{field} must be nonzero", code=f"invalid_{field}")
    exponent = quantity.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -3:
        raise DomainError(f"{field} supports at most 3 decimal places", code=f"invalid_{field}")
    if abs(quantity) >= Decimal("100000000000"):
        raise DomainError(f"{field} is too large", code=f"invalid_{field}")
    return quantity


def parse_cost(value: object) -> Decimal:
    try:
        cost = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainError("unit_cost must be a number", code="invalid_unit_cost") from exc
    exponent = cost.as_tuple().exponent
    if not cost.is_finite() or cost < 0 or not isinstance(exponent, int) or exponent < -4:
        raise DomainError(
            "unit_cost must be nonnegative with at most 4 decimal places",
            code="invalid_unit_cost",
        )
    return cost


def parse_operation_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError(
            "A UUID operation_id or Idempotency-Key is required",
            code="invalid_operation_id",
        ) from exc


def parse_resource_id(value: object, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError(f"{field} must be a UUID", code=f"invalid_{field}") from exc


def _validate_reason(reason: str) -> None:
    if len(reason) > 500:
        raise DomainError(
            "reason supports at most 500 characters",
            code="invalid_reason",
        )


def _same_org(organization: Organization, **resources: object) -> None:
    for name, resource in resources.items():
        if resource is not None and getattr(resource, "organization_id", None) != organization.pk:
            raise DomainError(
                f"{name} does not belong to this organization",
                code="organization_mismatch",
                status=403,
            )


def _validate_location(organization: Organization, part: Part, bin: Bin) -> None:
    _same_org(organization, part=part, bin=bin, warehouse=bin.warehouse)
    if not part.active or not bin.active or not bin.warehouse.active:
        raise DomainError("Part and stock location must be active", code="inactive_stock_resource")


def _validate_work_order(work_order: Any) -> None:
    if getattr(work_order, "status", None) in {"Completed", "Closed", "Cancelled"}:
        raise DomainError(
            "Stock cannot be committed to a completed or closed work order",
            code="work_order_not_open",
            status=409,
        )


def _lock_current_work_order(organization: Organization, work_order: Any) -> Any:
    from maintenance.models import WorkOrder

    return WorkOrder.objects.select_for_update().get(
        organization=organization,
        pk=work_order.pk,
    )


def can_access_all_inventory_work_orders(actor: User) -> bool:
    """Classify role authority without requiring an unrelated API-token scope."""

    granted = permissions_for(actor)
    return "*" in granted or bool(
        granted & {"inventory.transact", "inventory.adjust", "maintenance.manage"}
    )


def _authorize_work_order_stock(actor: User, work_order: Any | None) -> None:
    if work_order is None:
        return
    if can_access_all_inventory_work_orders(actor):
        return
    from maintenance.services import is_active_work_order_assignee

    if is_active_work_order_assignee(work_order, actor):
        return
    raise DomainError(
        "Stock activity is limited to assigned work orders",
        code="permission_denied",
        status=403,
    )


def _locked_balance(organization: Organization, part: Part, bin: Bin) -> StockBalance:
    StockBalance.objects.get_or_create(
        organization=organization,
        part=part,
        bin=bin,
        defaults={"quantity_on_hand": 0, "quantity_reserved": 0},
    )
    return StockBalance.objects.select_for_update().get(
        organization=organization, part=part, bin=bin
    )


def _existing_transaction(
    *,
    organization: Organization,
    actor: User,
    operation_id: uuid.UUID,
    transaction_type: str,
    part: Part,
    bin: Bin,
    quantity: Decimal,
    unit_cost: Decimal,
    reason: str,
    work_order: Any | None = None,
    reservation: Reservation | None = None,
    original_transaction: StockTransaction | None = None,
    reference_type: str = "",
    reference_id: str = "",
) -> StockTransaction | None:
    _validate_reason(reason)
    existing = StockTransaction.objects.filter(
        organization=organization, operation_id=operation_id
    ).first()
    if existing is None:
        return None
    if (
        existing.actor_id != actor.pk
        or existing.transaction_type != transaction_type
        or existing.part_id != part.pk
        or existing.bin_id != bin.pk
        or existing.quantity != quantity
        or existing.unit_cost != unit_cost
        or existing.reason != reason
        or existing.work_order_id != getattr(work_order, "pk", None)
        or existing.reservation_id != getattr(reservation, "pk", None)
        or existing.original_transaction_id != getattr(original_transaction, "pk", None)
        or existing.reference_type != reference_type[:50]
        or existing.reference_id != reference_id[:80]
    ):
        raise DomainError(
            "This operation_id was already used for different stock input",
            code="idempotency_conflict",
            status=409,
        )
    return existing


def _record_transaction(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    transaction_type: str,
    quantity: Decimal,
    operation_id: uuid.UUID,
    unit_cost: Decimal,
    total_cost: Decimal,
    work_order: Any | None = None,
    reservation: Reservation | None = None,
    original_transaction: StockTransaction | None = None,
    reference_type: str = "",
    reference_id: str = "",
    reason: str = "",
    source: str = "web",
    audit_context: dict[str, object] | None = None,
) -> StockTransaction:
    _validate_reason(reason)
    row = StockTransaction.objects.create(
        organization=organization,
        actor=actor,
        part=part,
        bin=bin,
        transaction_type=transaction_type,
        quantity=quantity,
        unit_cost=unit_cost,
        total_cost=total_cost,
        work_order=work_order,
        reservation=reservation,
        original_transaction=original_transaction,
        reference_type=reference_type[:50],
        reference_id=reference_id[:80],
        reason=reason[:500],
        operation_id=operation_id,
    )
    action = {
        "RECEIPT": "stock.received",
        "ISSUE": "stock.issued",
        "RETURN": "stock.returned",
        "ADJUSTMENT": "stock.adjusted",
        "COUNT_ADJUSTMENT": "stock.adjusted",
        "REVERSAL": "stock.reversed",
    }[str(transaction_type)]
    context: dict[str, object] = {
        "part_id": str(part.pk),
        "bin_id": str(bin.pk),
        "quantity": str(quantity),
        "operation_id": str(operation_id),
    }
    if audit_context:
        context.update(audit_context)
    audit(
        organization=organization,
        actor=actor,
        action=action,
        resource=row,
        context=context,
        correlation_id=str(operation_id),
        source=source,
    )
    emit(organization=organization, event_type=action, resource=row, payload=context)
    return row


@transaction.atomic
def post_stock_transaction(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    transaction_type: str,
    quantity: object,
    operation_id: object,
    unit_cost: object = 0,
    work_order: Any | None = None,
    reference_type: str = "",
    reference_id: str = "",
    reason: str = "",
    source: str = "web",
) -> StockTransaction:
    """Post a receipt or explicit adjustment and update its locked projection."""
    if transaction_type not in {
        StockTransaction.Type.RECEIPT,
        StockTransaction.Type.ADJUSTMENT,
        StockTransaction.Type.COUNT_ADJUSTMENT,
    }:
        raise DomainError("Use the dedicated issue, return, or reversal service")
    qty = parse_quantity(quantity)
    if transaction_type == StockTransaction.Type.RECEIPT and qty < 0:
        raise DomainError("Receipt quantity must be positive", code="invalid_quantity")
    if transaction_type != StockTransaction.Type.RECEIPT and not reason.strip():
        raise DomainError("A reason is required for stock adjustments", code="reason_required")
    cost = parse_cost(unit_cost)
    op_id = parse_operation_id(operation_id)
    _validate_location(organization, part, bin)
    _same_org(organization, actor=actor, work_order=work_order)
    if work_order is not None:
        _validate_work_order(work_order)
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=transaction_type,
        part=part,
        bin=bin,
        quantity=qty,
        unit_cost=cost,
        reason=reason,
        work_order=work_order,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    if existing:
        return existing
    balance = _locked_balance(organization, part, bin)
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=transaction_type,
        part=part,
        bin=bin,
        quantity=qty,
        unit_cost=cost,
        reason=reason,
        work_order=work_order,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    if existing:
        return existing
    next_on_hand = balance.quantity_on_hand + qty
    if next_on_hand < balance.quantity_reserved:
        raise DomainError(
            "Stock change would consume reserved or unavailable quantity",
            code="insufficient_available_stock",
            status=409,
            details={"available_quantity": str(balance.available_quantity)},
        )
    balance.quantity_on_hand = next_on_hand
    balance.save(update_fields=["quantity_on_hand", "updated_at"])
    return _record_transaction(
        organization=organization,
        actor=actor,
        part=part,
        bin=bin,
        transaction_type=transaction_type,
        quantity=qty,
        operation_id=op_id,
        unit_cost=cost,
        total_cost=qty * cost,
        work_order=work_order,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
        source=source,
    )


def receive_stock(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    quantity: object,
    unit_cost: object,
    operation_id: object,
    reference_type: str = "receipt",
    reference_id: str = "",
    reason: str = "",
) -> StockTransaction:
    qty = parse_quantity(quantity)
    if qty < 0:
        raise DomainError("Receipt quantity must be positive", code="invalid_quantity")
    return post_stock_transaction(
        organization=organization,
        actor=actor,
        part=part,
        bin=bin,
        transaction_type=StockTransaction.Type.RECEIPT,
        quantity=qty,
        operation_id=operation_id,
        unit_cost=unit_cost,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
    )


def adjust_stock(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    quantity: object,
    operation_id: object,
    reason: str,
    unit_cost: object = 0,
) -> StockTransaction:
    return post_stock_transaction(
        organization=organization,
        actor=actor,
        part=part,
        bin=bin,
        transaction_type=StockTransaction.Type.ADJUSTMENT,
        quantity=quantity,
        operation_id=operation_id,
        unit_cost=unit_cost,
        reason=reason,
    )


@transaction.atomic
def reserve_stock(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    work_order: Any,
    quantity: object,
    operation_id: object,
    reason: str = "",
) -> Reservation:
    _validate_reason(reason)
    qty = parse_quantity(quantity)
    if qty < 0:
        raise DomainError("Reservation quantity must be positive", code="invalid_quantity")
    op_id = parse_operation_id(operation_id)
    _validate_location(organization, part, bin)
    _same_org(organization, actor=actor, work_order=work_order)
    work_order = _lock_current_work_order(organization, work_order)
    _authorize_work_order_stock(actor, work_order)
    existing = Reservation.objects.filter(organization=organization, operation_id=op_id).first()
    if existing:
        if (
            existing.created_by_id != actor.pk
            or existing.part_id != part.pk
            or existing.bin_id != bin.pk
            or existing.work_order_id != work_order.pk
            or existing.requested_quantity != qty
            or existing.reason != reason
        ):
            raise DomainError(
                "This operation_id was already used for a different reservation",
                code="idempotency_conflict",
                status=409,
            )
        return existing
    _validate_work_order(work_order)
    balance = _locked_balance(organization, part, bin)
    existing = Reservation.objects.filter(organization=organization, operation_id=op_id).first()
    if existing:
        if (
            existing.created_by_id != actor.pk
            or existing.part_id != part.pk
            or existing.bin_id != bin.pk
            or existing.work_order_id != work_order.pk
            or existing.requested_quantity != qty
            or existing.reason != reason
        ):
            raise DomainError(
                "This operation_id was already used for a different reservation",
                code="idempotency_conflict",
                status=409,
            )
        return existing
    if balance.available_quantity < qty:
        raise DomainError(
            "Insufficient available stock",
            code="insufficient_available_stock",
            status=409,
            details={"available_quantity": str(balance.available_quantity)},
        )
    reservation = Reservation.objects.create(
        organization=organization,
        part=part,
        bin=bin,
        work_order=work_order,
        requested_quantity=qty,
        operation_id=op_id,
        created_by=actor,
        reason=reason[:500],
    )
    balance.quantity_reserved += qty
    balance.save(update_fields=["quantity_reserved", "updated_at"])
    context = {"quantity": str(qty), "work_order_id": str(work_order.pk)}
    audit(
        organization=organization,
        actor=actor,
        action="stock.reserved",
        resource=reservation,
        new_state=Reservation.Status.ACTIVE,
        context=context,
        correlation_id=str(op_id),
    )
    emit(
        organization=organization,
        event_type="stock.reserved",
        resource=reservation,
        payload=context,
    )
    return reservation


@transaction.atomic
def release_reservation(
    *, organization: Organization, actor: User, reservation: Reservation, reason: str
) -> Reservation:
    _validate_reason(reason)
    if not reason.strip():
        raise DomainError("A reason is required to release stock", code="reason_required")
    _same_org(organization, actor=actor, reservation=reservation)
    locked = (
        Reservation.objects.select_for_update().select_related("work_order").get(pk=reservation.pk)
    )
    _authorize_work_order_stock(actor, locked.work_order)
    if locked.status in {Reservation.Status.RELEASED, Reservation.Status.FULFILLED}:
        return locked
    remaining = locked.remaining_quantity
    if remaining <= 0:
        raise DomainError("Reservation has no remaining quantity", code="reservation_empty")
    balance = _locked_balance(organization, locked.part, locked.bin)
    balance.quantity_reserved -= remaining
    balance.save(update_fields=["quantity_reserved", "updated_at"])
    previous = locked.status
    locked.released_quantity += remaining
    locked.status = Reservation.Status.RELEASED
    locked.save(update_fields=["released_quantity", "status", "updated_at"])
    audit(
        organization=organization,
        actor=actor,
        action="stock.reservation_released",
        resource=locked,
        previous_state=previous,
        new_state=locked.status,
        context={"quantity": str(remaining), "reason": reason},
    )
    return locked


@transaction.atomic
def issue_stock(
    *,
    organization: Organization,
    actor: User,
    part: Part,
    bin: Bin,
    work_order: Any,
    quantity: object,
    operation_id: object,
    reservation: Reservation | None = None,
    unit_cost: object | None = None,
    reason: str = "",
    source: str = "web",
    auth: object | None = None,
) -> StockTransaction:
    qty = parse_quantity(quantity)
    if qty < 0:
        raise DomainError("Issue quantity must be positive", code="invalid_quantity")
    stored_qty = -qty
    cost_override = unit_cost not in (None, "")
    if cost_override and not has_permission(actor, "inventory.adjust", auth):
        raise DomainError(
            "Permission required to override issue cost: inventory.adjust",
            code="cost_override_permission_denied",
            status=403,
        )
    if cost_override and not can_manage_financials(actor, auth):
        raise DomainError(
            "Permission required to override issue cost: financial.manage",
            code="financial_permission_denied",
            status=403,
        )
    if cost_override and not reason.strip():
        raise DomainError(
            "A reason is required to override issue cost",
            code="cost_override_reason_required",
        )
    cost = parse_cost(unit_cost if cost_override else part.default_unit_cost)
    op_id = parse_operation_id(operation_id)
    _validate_location(organization, part, bin)
    _same_org(organization, actor=actor, work_order=work_order, reservation=reservation)
    work_order = _lock_current_work_order(organization, work_order)
    _authorize_work_order_stock(actor, work_order)
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=StockTransaction.Type.ISSUE,
        part=part,
        bin=bin,
        quantity=stored_qty,
        unit_cost=cost,
        reason=reason,
        work_order=work_order,
        reservation=reservation,
    )
    if existing:
        return existing
    _validate_work_order(work_order)
    locked_reservation = None
    if reservation:
        locked_reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
        if (
            locked_reservation.part_id != part.pk
            or locked_reservation.bin_id != bin.pk
            or locked_reservation.work_order_id != work_order.pk
        ):
            raise DomainError("Reservation does not match this issue", code="reservation_mismatch")
        existing = _existing_transaction(
            organization=organization,
            actor=actor,
            operation_id=op_id,
            transaction_type=StockTransaction.Type.ISSUE,
            part=part,
            bin=bin,
            quantity=stored_qty,
            unit_cost=cost,
            reason=reason,
            work_order=work_order,
            reservation=locked_reservation,
        )
        if existing:
            return existing
        if (
            locked_reservation.status
            not in {
                Reservation.Status.ACTIVE,
                Reservation.Status.PARTIALLY_ISSUED,
            }
            or locked_reservation.remaining_quantity < qty
        ):
            raise DomainError(
                "Issue exceeds the active reservation",
                code="reservation_insufficient",
                status=409,
            )
    balance = _locked_balance(organization, part, bin)
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=StockTransaction.Type.ISSUE,
        part=part,
        bin=bin,
        quantity=stored_qty,
        unit_cost=cost,
        reason=reason,
        work_order=work_order,
        reservation=locked_reservation,
    )
    if existing:
        return existing
    if locked_reservation:
        if balance.quantity_on_hand < qty or balance.quantity_reserved < qty:
            raise DomainError("Reserved stock is unavailable", code="stock_conflict", status=409)
        balance.quantity_reserved -= qty
    elif balance.available_quantity < qty:
        raise DomainError(
            "Insufficient available stock",
            code="insufficient_available_stock",
            status=409,
            details={"available_quantity": str(balance.available_quantity)},
        )
    balance.quantity_on_hand -= qty
    balance.save(update_fields=["quantity_on_hand", "quantity_reserved", "updated_at"])
    if locked_reservation:
        locked_reservation.issued_quantity += qty
        locked_reservation.status = (
            Reservation.Status.FULFILLED
            if locked_reservation.remaining_quantity == 0
            else Reservation.Status.PARTIALLY_ISSUED
        )
        locked_reservation.save(update_fields=["issued_quantity", "status", "updated_at"])
    return _record_transaction(
        organization=organization,
        actor=actor,
        part=part,
        bin=bin,
        transaction_type=StockTransaction.Type.ISSUE,
        quantity=stored_qty,
        operation_id=op_id,
        unit_cost=cost,
        total_cost=qty * cost,
        work_order=work_order,
        reservation=locked_reservation,
        reason=reason,
        source=source,
        audit_context=(
            {
                "unit_cost_override": True,
                "default_unit_cost": str(part.default_unit_cost),
                "unit_cost": str(cost),
                "override_reason": reason,
            }
            if cost_override
            else None
        ),
    )


def _returned_quantity(original: StockTransaction) -> Decimal:
    direct = StockTransaction.objects.filter(
        original_transaction=original,
        transaction_type__in=[StockTransaction.Type.RETURN, StockTransaction.Type.REVERSAL],
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    reversed_returns = StockTransaction.objects.filter(
        original_transaction__original_transaction=original,
        original_transaction__transaction_type=StockTransaction.Type.RETURN,
        transaction_type=StockTransaction.Type.REVERSAL,
    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    return direct + reversed_returns


@transaction.atomic
def return_stock(
    *,
    organization: Organization,
    actor: User,
    original: StockTransaction,
    quantity: object,
    operation_id: object,
    reason: str,
    source: str = "web",
) -> StockTransaction:
    qty = parse_quantity(quantity)
    if qty < 0:
        raise DomainError("Return quantity must be positive", code="invalid_quantity")
    if not reason.strip():
        raise DomainError("A reason is required for a return", code="reason_required")
    op_id = parse_operation_id(operation_id)
    _same_org(organization, actor=actor, original=original)
    locked_original = (
        StockTransaction.objects.select_for_update()
        .select_related("part", "bin__warehouse")
        .get(pk=original.pk)
    )
    if locked_original.transaction_type != StockTransaction.Type.ISSUE:
        raise DomainError("Returns must reference an original issue", code="invalid_original")
    _authorize_work_order_stock(actor, locked_original.work_order)
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=StockTransaction.Type.RETURN,
        part=locked_original.part,
        bin=locked_original.bin,
        quantity=qty,
        unit_cost=locked_original.unit_cost,
        reason=reason,
        work_order=locked_original.work_order,
        original_transaction=locked_original,
    )
    if existing:
        return existing
    if _returned_quantity(locked_original) + qty > abs(locked_original.quantity):
        raise DomainError(
            "Return exceeds the unreturned issue quantity", code="return_exceeds_issue", status=409
        )
    balance = _locked_balance(organization, locked_original.part, locked_original.bin)
    balance.quantity_on_hand += qty
    balance.save(update_fields=["quantity_on_hand", "updated_at"])
    return _record_transaction(
        organization=organization,
        actor=actor,
        part=locked_original.part,
        bin=locked_original.bin,
        transaction_type=StockTransaction.Type.RETURN,
        quantity=qty,
        operation_id=op_id,
        unit_cost=locked_original.unit_cost,
        total_cost=-(qty * locked_original.unit_cost),
        work_order=locked_original.work_order,
        original_transaction=locked_original,
        reason=reason,
        source=source,
    )


@transaction.atomic
def reverse_transaction(
    *,
    organization: Organization,
    actor: User,
    original: StockTransaction,
    operation_id: object,
    reason: str,
) -> StockTransaction:
    if not reason.strip():
        raise DomainError("A reason is required for a reversal", code="reason_required")
    op_id = parse_operation_id(operation_id)
    _same_org(organization, actor=actor, original=original)
    locked_original = (
        StockTransaction.objects.select_for_update()
        .select_related("part", "bin__warehouse")
        .get(pk=original.pk)
    )
    receipt_line = getattr(locked_original, "receipt_line", None)
    if receipt_line is not None and getattr(receipt_line, "reversal", None) is None:
        raise DomainError(
            "Receipt stock must be reversed through the receipt reversal workflow",
            code="receipt_reversal_required",
            status=409,
        )
    if locked_original.transaction_type == StockTransaction.Type.REVERSAL:
        raise DomainError("A reversal cannot itself be reversed", code="invalid_original")
    _authorize_work_order_stock(actor, locked_original.work_order)
    delta = -locked_original.quantity
    locked_reservation = None
    if (
        locked_original.transaction_type == StockTransaction.Type.ISSUE
        and locked_original.reservation_id is not None
    ):
        locked_reservation = Reservation.objects.select_for_update().get(
            pk=locked_original.reservation_id
        )
    existing = _existing_transaction(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        transaction_type=StockTransaction.Type.REVERSAL,
        part=locked_original.part,
        bin=locked_original.bin,
        quantity=delta,
        unit_cost=locked_original.unit_cost,
        reason=reason,
        work_order=locked_original.work_order,
        reservation=locked_reservation,
        original_transaction=locked_original,
        reference_type=locked_original.reference_type,
        reference_id=locked_original.reference_id,
    )
    if existing:
        return existing
    if StockTransaction.objects.filter(
        organization=organization,
        transaction_type=StockTransaction.Type.REVERSAL,
        original_transaction=locked_original,
    ).exists():
        raise DomainError(
            "Stock transaction is already reversed", code="already_reversed", status=409
        )
    has_returns = StockTransaction.objects.filter(
        organization=organization,
        transaction_type=StockTransaction.Type.RETURN,
        original_transaction=locked_original,
    ).exists()
    if locked_original.transaction_type == StockTransaction.Type.ISSUE and has_returns:
        raise DomainError(
            "An issue with recorded returns cannot be reversed as a whole",
            code="transaction_partially_compensated",
            status=409,
        )
    reservation_audit_context: dict[str, object] | None = None
    if locked_reservation is not None:
        issue_quantity = abs(locked_original.quantity)
        if locked_reservation.issued_quantity < issue_quantity:
            raise DomainError(
                "Reservation projection cannot safely reverse this issue",
                code="reservation_projection_conflict",
                status=409,
            )
        previous_status = locked_reservation.status
        previous_issued = locked_reservation.issued_quantity
        previous_released = locked_reservation.released_quantity
        locked_reservation.issued_quantity -= issue_quantity
        restore_reservation = previous_status in {
            Reservation.Status.ACTIVE,
            Reservation.Status.PARTIALLY_ISSUED,
            Reservation.Status.FULFILLED,
        }
        if restore_reservation:
            locked_reservation.status = (
                Reservation.Status.ACTIVE
                if locked_reservation.issued_quantity == 0
                else Reservation.Status.PARTIALLY_ISSUED
            )
        elif previous_status in {Reservation.Status.RELEASED, Reservation.Status.EXPIRED}:
            locked_reservation.released_quantity += issue_quantity
        else:
            raise DomainError(
                "Reservation state cannot safely reverse this issue",
                code="reservation_projection_conflict",
                status=409,
            )
        reservation_audit_context = {
            "reservation_id": str(locked_reservation.pk),
            "reservation_status_before": previous_status,
            "reservation_status_after": locked_reservation.status,
            "reservation_issued_before": str(previous_issued),
            "reservation_issued_after": str(locked_reservation.issued_quantity),
            "reservation_released_before": str(previous_released),
            "reservation_released_after": str(locked_reservation.released_quantity),
            "reservation_restored": restore_reservation,
        }
    balance = _locked_balance(organization, locked_original.part, locked_original.bin)
    if delta < 0 and balance.available_quantity < abs(delta):
        raise DomainError(
            "Reversal would consume unavailable stock",
            code="insufficient_available_stock",
            status=409,
        )
    balance.quantity_on_hand += delta
    if locked_reservation is not None and restore_reservation:
        balance.quantity_reserved += abs(locked_original.quantity)
    balance.save(update_fields=["quantity_on_hand", "quantity_reserved", "updated_at"])
    if locked_reservation is not None:
        locked_reservation.save(
            update_fields=["issued_quantity", "released_quantity", "status", "updated_at"]
        )
    return _record_transaction(
        organization=organization,
        actor=actor,
        part=locked_original.part,
        bin=locked_original.bin,
        transaction_type=StockTransaction.Type.REVERSAL,
        quantity=delta,
        operation_id=op_id,
        unit_cost=locked_original.unit_cost,
        total_cost=-locked_original.total_cost,
        work_order=locked_original.work_order,
        reservation=locked_reservation,
        original_transaction=locked_original,
        reference_type=locked_original.reference_type,
        reference_id=locked_original.reference_id,
        reason=reason,
        audit_context=reservation_audit_context,
    )


def _post_locked_inventory_count(
    *,
    organization: Organization,
    actor: User,
    count: InventoryCount,
    previous_state: str,
    approval_operation_id: uuid.UUID | None = None,
) -> InventoryCount:
    lines = list(count.lines.select_related("part", "bin__warehouse").order_by("bin_id", "part_id"))
    for line in lines:
        balance = _locked_balance(organization, line.part, line.bin)
        if balance.quantity_on_hand != line.expected_quantity:
            raise DomainError(
                "Stock changed after the count was captured; recount before posting",
                code="count_balance_stale",
                status=409,
                details={
                    "line_id": str(line.pk),
                    "part_id": str(line.part_id),
                    "bin_id": str(line.bin_id),
                    "expected_quantity": str(line.expected_quantity),
                    "current_quantity": str(balance.quantity_on_hand),
                },
            )
        if line.counted_quantity < balance.quantity_reserved:
            raise DomainError(
                "Reserved stock changed after the count was captured; recount before posting",
                code="count_balance_stale",
                status=409,
                details={
                    "line_id": str(line.pk),
                    "part_id": str(line.part_id),
                    "bin_id": str(line.bin_id),
                    "counted_quantity": str(line.counted_quantity),
                    "current_reserved_quantity": str(balance.quantity_reserved),
                },
            )
        if not line.variance:
            continue
        balance.quantity_on_hand = line.counted_quantity
        balance.save(update_fields=["quantity_on_hand", "updated_at"])
        line.adjustment_transaction = _record_transaction(
            organization=organization,
            actor=actor,
            part=line.part,
            bin=line.bin,
            transaction_type=StockTransaction.Type.COUNT_ADJUSTMENT,
            quantity=line.variance,
            operation_id=uuid.uuid5(count.operation_id, f"{line.part_id}:{line.bin_id}"),
            unit_cost=line.unit_cost_snapshot,
            total_cost=line.variance * line.unit_cost_snapshot,
            reference_type="inventory_count",
            reference_id=str(count.pk),
            reason=count.reason,
        )
        line.save(update_fields=["adjustment_transaction", "updated_at"])

    now = timezone.now()
    count.status = InventoryCount.Status.POSTED
    count.posted_by = actor
    count.posted_at = now
    update_fields = ["status", "posted_by", "posted_at", "updated_at"]
    if previous_state == InventoryCount.Status.PENDING_APPROVAL:
        count.approved_by = actor
        count.approved_at = now
        update_fields.extend(["approved_by", "approved_at"])
    count.save(update_fields=update_fields)
    context = {
        "line_count": len(lines),
        "reason": count.reason,
        "total_variance_value": str(count.total_variance_value),
        "approval_threshold": str(count.approval_threshold),
        "approval_required": count.approval_required,
    }
    if approval_operation_id:
        context["approval_operation_id"] = str(approval_operation_id)
    audit(
        organization=organization,
        actor=actor,
        action="inventory_count.posted",
        resource=count,
        previous_state=previous_state,
        new_state=InventoryCount.Status.POSTED,
        context=context,
        correlation_id=str(count.operation_id),
    )
    emit(
        organization=organization,
        event_type="inventory_count.posted",
        resource=count,
        payload={
            "line_count": len(lines),
            "approval_required": count.approval_required,
        },
    )
    return count


def _existing_inventory_count(
    *,
    organization: Organization,
    actor: User,
    operation_id: uuid.UUID,
    warehouse: Warehouse,
    reason: str,
    lines: list[tuple[Part, Bin, Decimal]],
) -> InventoryCount | None:
    existing = InventoryCount.objects.filter(
        organization=organization,
        operation_id=operation_id,
    ).first()
    if existing is None:
        return None
    requested_lines = sorted((str(part.pk), str(bin.pk), quantity) for part, bin, quantity in lines)
    stored_lines = sorted(
        (str(part_id), str(bin_id), quantity)
        for part_id, bin_id, quantity in existing.lines.values_list(
            "part_id", "bin_id", "counted_quantity"
        )
    )
    if (
        existing.created_by_id != actor.pk
        or existing.warehouse_id != warehouse.pk
        or existing.reason != reason
        or stored_lines != requested_lines
    ):
        raise DomainError(
            "This operation_id was already used for different inventory count input",
            code="idempotency_conflict",
            status=409,
        )
    return existing


@transaction.atomic
def post_inventory_count(
    *,
    organization: Organization,
    actor: User,
    warehouse: Warehouse,
    lines: list[dict[str, Any]],
    operation_id: object,
    reason: str,
    can_adjust: bool | None = None,
) -> InventoryCount:
    _validate_reason(reason)
    if not reason.strip():
        raise DomainError("A reason is required for an inventory count", code="reason_required")
    if not lines:
        raise DomainError("At least one count line is required", code="lines_required")
    op_id = parse_operation_id(operation_id)
    _same_org(organization, actor=actor, warehouse=warehouse)
    parsed: list[tuple[Part, Bin, Decimal]] = []
    seen: set[tuple[object, object]] = set()
    for data in lines:
        part, bin = data.get("part"), data.get("bin")
        if not isinstance(part, Part) or not isinstance(bin, Bin):
            raise DomainError("Every count line requires a valid part and bin", code="invalid_line")
        qty = parse_quantity(data.get("counted_quantity"), "counted_quantity", allow_zero=True)
        if qty < 0:
            raise DomainError(
                "Counted quantity cannot be negative", code="invalid_counted_quantity"
            )
        _same_org(organization, part=part, bin=bin, bin_warehouse=bin.warehouse)
        key = (part.pk, bin.pk)
        if key in seen:
            raise DomainError("Duplicate part/bin count line", code="duplicate_count_line")
        seen.add(key)
        parsed.append((part, bin, qty))

    # The operation-id namespace is organization-wide, so serialize count creation
    # across warehouses before checking the unique key and its full input.
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    warehouse = Warehouse.objects.select_for_update().get(
        organization=organization, pk=warehouse.pk
    )
    existing = _existing_inventory_count(
        organization=organization,
        actor=actor,
        operation_id=op_id,
        warehouse=warehouse,
        reason=reason,
        lines=parsed,
    )
    if existing:
        return existing
    for part, bin, _qty in parsed:
        _validate_location(organization, part, bin)
        if bin.warehouse_id != warehouse.pk:
            raise DomainError(
                "Count bin is outside the selected warehouse", code="warehouse_mismatch"
            )
    count = InventoryCount.objects.create(
        organization=organization,
        warehouse=warehouse,
        operation_id=op_id,
        reason=reason[:500],
        created_by=actor,
    )
    total_variance_value = Decimal("0")
    for part, bin, counted in sorted(parsed, key=lambda row: (str(row[1].pk), str(row[0].pk))):
        balance = _locked_balance(organization, part, bin)
        if counted < balance.quantity_reserved:
            raise DomainError(
                "Count is below currently reserved stock",
                code="count_below_reserved",
                status=409,
                details={"reserved_quantity": str(balance.quantity_reserved)},
            )
        expected = balance.quantity_on_hand
        variance = counted - expected
        total_variance_value += abs(variance * part.default_unit_cost).quantize(Decimal("0.0001"))
        InventoryCountLine.objects.create(
            organization=organization,
            inventory_count=count,
            part=part,
            bin=bin,
            expected_quantity=expected,
            counted_quantity=counted,
            variance=variance,
            unit_cost_snapshot=part.default_unit_cost,
        )
    count.total_variance_value = total_variance_value
    if can_adjust is False:
        # A field counter can submit the physical count, but an authorized financial
        # approver must post every nonzero valuation adjustment.
        count.approval_required = total_variance_value > 0
    else:
        count.approval_required = (
            not (has_permission(actor, "inventory.adjust") if can_adjust is None else can_adjust)
            and total_variance_value > 0
            and total_variance_value >= count.approval_threshold
        )
    if not count.approval_required:
        count.save(update_fields=["total_variance_value", "approval_required", "updated_at"])
        return _post_locked_inventory_count(
            organization=organization,
            actor=actor,
            count=count,
            previous_state=InventoryCount.Status.DRAFT,
        )

    count.status = InventoryCount.Status.PENDING_APPROVAL
    count.save(
        update_fields=[
            "status",
            "total_variance_value",
            "approval_required",
            "updated_at",
        ]
    )
    audit(
        organization=organization,
        actor=actor,
        action="inventory_count.pending_approval",
        resource=count,
        previous_state=InventoryCount.Status.DRAFT,
        new_state=InventoryCount.Status.PENDING_APPROVAL,
        context={
            "line_count": len(parsed),
            "reason": reason,
            "total_variance_value": str(total_variance_value),
            "approval_threshold": str(count.approval_threshold),
        },
        correlation_id=str(op_id),
    )
    emit(
        organization=organization,
        event_type="inventory_count.pending_approval",
        resource=count,
        payload={
            "line_count": len(parsed),
            "total_variance_value": str(total_variance_value),
        },
    )
    return count


@transaction.atomic
def approve_inventory_count(
    *,
    organization: Organization,
    actor: User,
    count: InventoryCount,
    operation_id: object,
) -> InventoryCount:
    if not has_permission(actor, "inventory.adjust"):
        raise DomainError(
            "Permission required: inventory.adjust",
            code="permission_denied",
            status=403,
        )
    _same_org(organization, actor=actor, count=count)
    approval_operation_id = parse_operation_id(operation_id)
    count = (
        InventoryCount.objects.select_for_update()
        .select_related("created_by", "warehouse")
        .get(organization=organization, pk=count.pk)
    )
    if count.status != InventoryCount.Status.PENDING_APPROVAL:
        raise DomainError(
            "Only a count pending approval can be approved",
            code="count_not_pending_approval",
            status=409,
        )
    if count.created_by_id == actor.pk:
        raise DomainError(
            "The count submitter cannot approve a variance requiring approval",
            code="separation_of_duties",
            status=403,
        )
    return _post_locked_inventory_count(
        organization=organization,
        actor=actor,
        count=count,
        previous_state=InventoryCount.Status.PENDING_APPROVAL,
        approval_operation_id=approval_operation_id,
    )


def reconcile_inventory(organization: Organization) -> list[dict[str, str]]:
    """Return projection mismatches without repairing or hiding ledger drift."""
    ledger: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {
        (row["part_id"], row["bin_id"]): row["quantity"] or Decimal("0")
        for row in StockTransaction.objects.filter(organization=organization)
        .values("part_id", "bin_id")
        .annotate(quantity=Sum("quantity"))
    }
    reserved: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    for row in Reservation.objects.filter(
        organization=organization,
        status__in=[Reservation.Status.ACTIVE, Reservation.Status.PARTIALLY_ISSUED],
    ).values("part_id", "bin_id", "requested_quantity", "issued_quantity", "released_quantity"):
        key = (row["part_id"], row["bin_id"])
        reserved[key] = reserved.get(key, Decimal("0")) + (
            row["requested_quantity"] - row["issued_quantity"] - row["released_quantity"]
        )
    balances: dict[tuple[uuid.UUID, uuid.UUID], StockBalance] = {
        (row.part_id, row.bin_id): row
        for row in StockBalance.objects.filter(organization=organization)
    }
    mismatches = []
    for key in set(ledger) | set(reserved) | set(balances):
        balance = balances.get(key)
        projected_on_hand = balance.quantity_on_hand if balance else Decimal("0")
        projected_reserved = balance.quantity_reserved if balance else Decimal("0")
        ledger_on_hand = ledger.get(key, Decimal("0"))
        ledger_reserved = reserved.get(key, Decimal("0"))
        if projected_on_hand != ledger_on_hand or projected_reserved != ledger_reserved:
            mismatches.append(
                {
                    "part_id": str(key[0]),
                    "bin_id": str(key[1]),
                    "projected_on_hand": str(projected_on_hand),
                    "ledger_on_hand": str(ledger_on_hand),
                    "projected_reserved": str(projected_reserved),
                    "reservation_total": str(ledger_reserved),
                }
            )
    return mismatches


def sync_inventory_operation(
    user: User, operation: dict[str, Any], auth: object | None = None
) -> dict[str, object]:
    payload = operation.get("payload") or {}
    if not isinstance(payload, dict):
        raise DomainError("Offline operation payload must be an object", code="invalid_payload")
    operation_id = operation.get("operation_id")
    operation_type = operation.get("type")
    organization = user.organization
    if organization is None:
        raise DomainError("User has no organization", code="organization_required", status=403)
    if operation_type == "stock.issue":
        if not has_permission(user, "inventory.issue", auth):
            raise DomainError("Stock issue is not allowed", code="permission_denied", status=403)
        from maintenance.models import WorkOrder

        part = Part.objects.filter(
            organization=organization,
            pk=parse_resource_id(payload.get("part_id"), "part_id"),
        ).first()
        bin = Bin.objects.filter(
            organization=organization,
            pk=parse_resource_id(payload.get("bin_id"), "bin_id"),
        ).first()
        work_order = WorkOrder.objects.filter(
            organization=organization,
            pk=parse_resource_id(payload.get("work_order_id"), "work_order_id"),
        ).first()
        if not part or not bin or not work_order:
            raise DomainError("Part, bin, or work order was not found", code="resource_not_found")
        reservation = None
        if payload.get("reservation_id"):
            reservation = Reservation.objects.filter(
                organization=organization,
                pk=parse_resource_id(payload["reservation_id"], "reservation_id"),
            ).first()
            if not reservation:
                raise DomainError("Reservation was not found", code="resource_not_found")
        row = issue_stock(
            organization=organization,
            actor=user,
            part=part,
            bin=bin,
            work_order=work_order,
            quantity=payload.get("quantity"),
            operation_id=operation_id,
            reservation=reservation,
            unit_cost=payload.get("unit_cost"),
            reason=str(payload.get("reason", "")),
            source="offline",
            auth=auth,
        )
        return {"transaction": row.to_dict(include_financial=can_view_financials(user, auth))}
    if operation_type == "stock.return":
        if not has_permission(user, "inventory.return", auth):
            raise DomainError("Stock return is not allowed", code="permission_denied", status=403)
        original = StockTransaction.objects.filter(
            organization=organization,
            pk=parse_resource_id(payload.get("original_transaction_id"), "original_transaction_id"),
        ).first()
        if not original:
            raise DomainError("Original issue was not found", code="resource_not_found")
        row = return_stock(
            organization=organization,
            actor=user,
            original=original,
            quantity=payload.get("quantity"),
            operation_id=operation_id,
            reason=str(payload.get("reason", "")),
            source="offline",
        )
        return {"transaction": row.to_dict(include_financial=can_view_financials(user, auth))}
    raise DomainError("Unsupported inventory operation", code="unsupported_offline_operation")
