from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from core.exceptions import DomainError
from core.permissions import (
    can_manage_financials,
    can_view_financials,
    has_permission,
    redact_financial_fields,
)
from core.services import idempotent
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from inventory.models import Bin, Part
from inventory.services import parse_cost, parse_quantity, parse_resource_id
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .models import PurchaseOrder, PurchaseOrderLine, PurchaseRequest, Receipt, Vendor
from .services import (
    create_purchase_order,
    create_purchase_request,
    create_vendor,
    post_receipt,
    transition_purchase_order,
    transition_purchase_request,
)
from .services import (
    reverse_receipt as reverse_receipt_service,
)


def _require_any(request: Request, *permissions: str) -> None:
    if not any(
        has_permission(request.user, permission, request.auth) for permission in permissions
    ):
        raise DomainError("Permission denied", code="permission_denied", status=403)


def _require_financial_manage(request: Request) -> None:
    if not can_manage_financials(request.user, request.auth):
        raise DomainError(
            "Permission required: financial.manage",
            code="financial_permission_denied",
            status=403,
        )


def _financial_replay_transform(request: Request) -> Callable[[object], object] | None:
    """Redact a prior idempotent result for a now-less-privileged credential."""

    if can_view_financials(request.user, request.auth):
        return None
    return redact_financial_fields


def _date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DomainError(f"{field} must use YYYY-MM-DD", code="invalid_date") from exc


def _cost(value: object) -> Decimal:
    cost = parse_cost(value)
    if cost >= Decimal("10000000000"):
        raise DomainError("unit_cost is too large", code="invalid_unit_cost")
    return cost


def _operation_id(request: Request) -> uuid.UUID:
    value = request.headers.get("Idempotency-Key") or request.data.get("operation_id")
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError("Idempotency-Key must be a UUID", code="invalid_idempotency_key") from exc


@api_view(["GET", "POST"])
def vendors(request: Request) -> Response:
    organization = request.user.organization
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(request, "vendors.view", "vendors.manage", "purchasing.policy")
        rows = (
            Vendor.objects.filter(organization=organization)
            .prefetch_related("parts__part")
            .order_by("name")
        )
        return Response(
            {"vendors": [vendor.to_dict(include_financial=include_financial) for vendor in rows]}
        )
    _require_any(request, "vendors.manage")
    if "payment_terms" in request.data:
        _require_financial_manage(request)

    def handler() -> Response:
        try:
            vendor = create_vendor(
                organization=organization,
                actor=request.user,
                code=str(request.data.get("code", "")),
                name=str(request.data.get("name", "")),
                contact_name=str(request.data.get("contact_name", "")),
                email=str(request.data.get("email", "")),
                phone=str(request.data.get("phone", "")),
                address=str(request.data.get("address", "")),
                payment_terms=str(request.data.get("payment_terms", "")),
            )
        except IntegrityError as exc:
            raise DomainError(
                "Vendor code already exists", code="duplicate_vendor", status=409
            ) from exc
        return Response({"vendor": vendor.to_dict(include_financial=include_financial)}, status=201)

    return idempotent(
        request,
        handler,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def purchase_requests(request: Request) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        _require_any(request, "purchasing.request", "purchasing.policy")
        rows = PurchaseRequest.objects.filter(organization=organization).select_related(
            "part", "requested_by", "approved_by"
        )
        return Response({"purchase_requests": [row.to_dict() for row in rows]})
    _require_any(request, "purchasing.request")
    part = get_object_or_404(
        Part,
        pk=parse_resource_id(request.data.get("part_id"), "part_id"),
        organization=organization,
    )

    def handler() -> Response:
        row = create_purchase_request(
            organization=organization,
            actor=request.user,
            part=part,
            quantity=parse_quantity(request.data.get("quantity")),
            reason=str(request.data.get("reason", "")),
            needed_by=_date(request.data.get("needed_by"), "needed_by"),
        )
        return Response({"purchase_request": row.to_dict()}, status=201)

    return idempotent(request, handler)


@api_view(["POST"])
def purchase_request_transition(request: Request, purchase_request_id: object) -> Response:
    organization = request.user.organization
    target = str(request.data.get("target", request.data.get("status", "")))
    if target in {PurchaseRequest.Status.APPROVED, PurchaseRequest.Status.REJECTED}:
        _require_any(request, "purchasing.approve")
    else:
        _require_any(request, "purchasing.request")
    purchase_request = get_object_or_404(
        PurchaseRequest,
        pk=purchase_request_id,
        organization=organization,
    )
    if (
        target == PurchaseRequest.Status.CANCELLED
        and purchase_request.requested_by_id != request.user.pk
        and not has_permission(request.user, "purchasing.manage", request.auth)
    ):
        raise DomainError(
            "Only the requester or purchasing manager can cancel this request",
            code="permission_denied",
            status=403,
        )

    def handler() -> Response:
        result = transition_purchase_request(
            organization=organization,
            actor=request.user,
            purchase_request=purchase_request,
            target=target,
            reason=str(request.data.get("reason", "")),
        )
        return Response({"purchase_request": result.to_dict()})

    return idempotent(request, handler)


@api_view(["GET", "POST"])
def purchase_orders(request: Request) -> Response:
    organization = request.user.organization
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(request, "purchasing.manage", "purchasing.receive", "purchasing.policy")
        rows = (
            PurchaseOrder.objects.filter(organization=organization)
            .select_related("vendor", "created_by", "submitted_by", "approved_by")
            .prefetch_related("lines__part", "lines__receipt_lines__receipt")
        )
        return Response(
            {"purchase_orders": [row.to_dict(include_financial=include_financial) for row in rows]}
        )
    _require_any(request, "purchasing.manage")
    _require_financial_manage(request)
    vendor = get_object_or_404(
        Vendor,
        pk=parse_resource_id(request.data.get("vendor_id"), "vendor_id"),
        organization=organization,
    )
    payload_lines = request.data.get("lines")
    if not isinstance(payload_lines, list):
        raise DomainError("lines must be a list", code="invalid_lines")
    lines: list[dict[str, object]] = []
    for item in payload_lines:
        if not isinstance(item, dict):
            raise DomainError("Each order line must be an object", code="invalid_lines")
        part = get_object_or_404(
            Part,
            pk=parse_resource_id(item.get("part_id"), "part_id"),
            organization=organization,
        )
        purchase_request = None
        if item.get("purchase_request_id"):
            purchase_request = get_object_or_404(
                PurchaseRequest,
                pk=parse_resource_id(item["purchase_request_id"], "purchase_request_id"),
                organization=organization,
            )
        quantity = parse_quantity(
            item.get("quantity_ordered", item.get("quantity")), "quantity_ordered"
        )
        unit_cost = _cost(item.get("unit_cost"))
        if abs(quantity * unit_cost) >= Decimal("100000000000000"):
            raise DomainError("line total is too large", code="invalid_line_total")
        lines.append(
            {
                "part": part,
                "purchase_request": purchase_request,
                "quantity_ordered": quantity,
                "unit_cost": unit_cost,
                "description": str(item.get("description", "")),
                "vendor_part_number": str(item.get("vendor_part_number", "")),
            }
        )

    def handler() -> Response:
        purchase_order = create_purchase_order(
            organization=organization,
            actor=request.user,
            vendor=vendor,
            lines=lines,
            number=str(request.data.get("number", "")),
            expected_at=_date(request.data.get("expected_at"), "expected_at"),
            emergency=request.data.get("emergency") is True,
            notes=str(request.data.get("notes", "")),
        )
        return Response(
            {"purchase_order": purchase_order.to_dict(include_financial=include_financial)},
            status=201,
        )

    return idempotent(
        request,
        handler,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["POST"])
def purchase_order_transition(request: Request, purchase_order_id: object) -> Response:
    organization = request.user.organization
    target = str(request.data.get("target", request.data.get("status", "")))
    if target == PurchaseOrder.Status.APPROVED:
        _require_any(request, "purchasing.approve")
    else:
        _require_any(request, "purchasing.manage")
    _require_financial_manage(request)
    purchase_order = get_object_or_404(
        PurchaseOrder, pk=purchase_order_id, organization=organization
    )

    def handler() -> Response:
        result = transition_purchase_order(
            organization=organization,
            actor=request.user,
            purchase_order=purchase_order,
            target=target,
            reason=str(request.data.get("reason", "")),
        )
        return Response(
            {
                "purchase_order": result.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            }
        )

    return idempotent(
        request,
        handler,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def receipts(request: Request) -> Response:
    organization = request.user.organization
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(request, "purchasing.manage", "purchasing.receive", "purchasing.policy")
        rows = (
            Receipt.objects.filter(organization=organization)
            .select_related("purchase_order", "received_by", "reversal_of", "reversed_by")
            .prefetch_related("lines__part", "lines__bin")
        )
        return Response(
            {"receipts": [row.to_dict(include_financial=include_financial) for row in rows]}
        )
    _require_any(request, "purchasing.receive")
    purchase_order = get_object_or_404(
        PurchaseOrder,
        pk=parse_resource_id(request.data.get("purchase_order_id"), "purchase_order_id"),
        organization=organization,
    )
    payload_lines = request.data.get("lines")
    if not isinstance(payload_lines, list):
        raise DomainError("lines must be a list", code="invalid_lines")
    lines: list[dict[str, object]] = []
    for item in payload_lines:
        if not isinstance(item, dict):
            raise DomainError("Each receipt line must be an object", code="invalid_lines")
        order_line = get_object_or_404(
            PurchaseOrderLine,
            pk=parse_resource_id(item.get("purchase_order_line_id"), "purchase_order_line_id"),
            organization=organization,
            purchase_order=purchase_order,
        )
        stock_bin = get_object_or_404(
            Bin,
            pk=parse_resource_id(item.get("bin_id"), "bin_id"),
            organization=organization,
        )
        lines.append(
            {
                "purchase_order_line": order_line,
                "bin": stock_bin,
                "quantity": parse_quantity(item.get("quantity")),
            }
        )

    def handler() -> Response:
        receipt = post_receipt(
            organization=organization,
            actor=request.user,
            purchase_order=purchase_order,
            lines=lines,
            operation_id=_operation_id(request),
            packing_slip=str(request.data.get("packing_slip", "")),
        )
        return Response(
            {"receipt": receipt.to_dict(include_financial=include_financial)}, status=201
        )

    return idempotent(
        request,
        handler,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["POST"])
def reverse_receipt(request: Request, receipt_id: object) -> Response:
    _require_any(request, "purchasing.receive")
    organization = request.user.organization
    receipt = get_object_or_404(Receipt, pk=receipt_id, organization=organization)

    def handler() -> Response:
        reversal = reverse_receipt_service(
            organization=organization,
            actor=request.user,
            receipt=receipt,
            operation_id=_operation_id(request),
            reason=str(request.data.get("reason", "")),
        )
        return Response(
            {
                "receipt": reversal.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        handler,
        replay_response_transform=_financial_replay_transform(request),
    )
