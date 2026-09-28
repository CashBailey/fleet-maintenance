from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.exceptions import DomainError
from core.models import Location
from core.permissions import (
    can_manage_financials,
    can_view_financials,
    has_permission,
    redact_financial_fields,
)
from core.services import audit, idempotent
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

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
    can_access_all_inventory_work_orders,
    issue_stock,
    parse_resource_id,
    post_inventory_count,
    reconcile_inventory,
    release_reservation,
    reserve_stock,
    return_stock,
    reverse_transaction,
)


def _require(request: Any, permission: str) -> None:
    if not has_permission(request.user, permission, request.auth):
        raise DomainError(
            f"Permission required: {permission}", code="permission_denied", status=403
        )


def _operation_id(request: Any) -> object:
    return request.headers.get("Idempotency-Key") or request.data.get("operation_id")


def _resource_id(data: Any, field: str) -> object:
    return parse_resource_id(data.get(field), field)


def _scope_work_order_rows(rows: Any, user: Any) -> Any:
    if can_access_all_inventory_work_orders(user):
        return rows
    from maintenance.models import WorkOrder
    from maintenance.services import filter_work_orders_for_assignee

    return rows.filter(
        work_order__in=filter_work_orders_for_assignee(
            WorkOrder.objects.filter(organization=user.organization), user
        )
    )


def _part_json(part: Part, *, include_financial: bool = False) -> dict[str, object]:
    return {
        **part.to_dict(include_financial=include_financial),
        "cross_references": [
            {"id": str(row.pk), "kind": row.kind, "value": row.value}
            for row in part.cross_references.all()
        ],
    }


def _financial_replay_transform(request: Any) -> Callable[[object], object] | None:
    """Redact a prior idempotent result for a now-less-privileged credential."""

    if can_view_financials(request.user, request.auth):
        return None
    return redact_financial_fields


@api_view(["GET", "POST"])
def parts_collection(request: Any) -> Response:
    organization = request.user.organization
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = Part.objects.filter(organization=organization).prefetch_related("cross_references")
        identifier = str(request.query_params.get("identifier", "")).strip()
        query = str(request.query_params.get("q", "")).strip()
        if identifier:
            normalized = identifier.upper()
            rows = rows.filter(
                Q(number__iexact=identifier)
                | Q(manufacturer_number__iexact=identifier)
                | Q(barcode__iexact=identifier)
                | Q(cross_references__value_normalized=normalized)
                | Q(vendor_parts__vendor_part_number__iexact=identifier)
            ).distinct()
        elif query:
            normalized = query.upper()
            rows = rows.filter(
                Q(number__icontains=query)
                | Q(name__icontains=query)
                | Q(manufacturer_number__icontains=query)
                | Q(barcode__iexact=query)
                | Q(cross_references__value_normalized__icontains=normalized)
                | Q(vendor_parts__vendor_part_number__icontains=query)
            ).distinct()
        if request.query_params.get("active") in {"true", "1"}:
            rows = rows.filter(active=True)
        return Response(
            {"parts": [_part_json(row, include_financial=include_financial) for row in rows[:500]]}
        )

    if not (
        has_permission(request.user, "inventory.transact", request.auth)
        or can_manage_financials(request.user, request.auth)
    ):
        raise DomainError("Part creation is not allowed", code="permission_denied", status=403)
    if "default_unit_cost" in request.data and not can_manage_financials(
        request.user, request.auth
    ):
        raise DomainError(
            "Permission required to set a part cost: financial.manage",
            code="financial_permission_denied",
            status=403,
        )

    def handle() -> Response:
        number = str(request.data.get("number", "")).strip().upper()
        name = str(request.data.get("name", "")).strip()
        if not number or not name:
            raise DomainError("number and name are required", code="required_fields")
        if Part.objects.filter(organization=organization, number=number).exists():
            raise DomainError("Part number already exists", code="duplicate_part", status=409)
        cross_references = request.data.get("cross_references", [])
        if not isinstance(cross_references, list):
            raise DomainError("cross_references must be a list", code="invalid_cross_references")
        from .services import parse_cost

        try:
            part = Part.objects.create(
                organization=organization,
                number=number,
                name=name,
                description=str(request.data.get("description", "")).strip(),
                manufacturer=str(request.data.get("manufacturer", "")).strip(),
                manufacturer_number=str(request.data.get("manufacturer_number", "")).strip(),
                unit_of_measure=str(request.data.get("unit_of_measure", "each")).strip() or "each",
                barcode=str(request.data.get("barcode", "")).strip(),
                default_unit_cost=parse_cost(request.data.get("default_unit_cost", 0)),
            )
            for item in cross_references:
                data = {"value": item} if isinstance(item, str) else item
                if not isinstance(data, dict) or not str(data.get("value", "")).strip():
                    raise DomainError(
                        "Invalid part cross-reference", code="invalid_cross_reference"
                    )
                kind = str(data.get("kind", PartCrossReference.Kind.ALTERNATE)).upper()
                if kind not in PartCrossReference.Kind.values:
                    raise DomainError(
                        "Invalid cross-reference kind", code="invalid_cross_reference"
                    )
                PartCrossReference.objects.create(
                    organization=organization,
                    part=part,
                    kind=kind,
                    value=str(data["value"]),
                )
        except (IntegrityError, ValidationError) as exc:
            raise DomainError(
                "Part number, barcode, or alternate number already exists",
                code="duplicate_part_reference",
                status=409,
            ) from exc
        audit(organization=organization, actor=request.user, action="part.created", resource=part)
        return Response({"part": _part_json(part, include_financial=include_financial)}, status=201)

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET"])
def part_history(request: Any, part_id: object) -> Response:
    _require(request, "inventory.view")
    organization = request.user.organization
    part = get_object_or_404(
        Part.objects.prefetch_related("cross_references"), pk=part_id, organization=organization
    )
    transactions = StockTransaction.objects.filter(
        organization=organization, part=part
    ).select_related("part", "bin__warehouse", "actor")
    reservations = Reservation.objects.filter(organization=organization, part=part).select_related(
        "part", "bin__warehouse", "work_order"
    )
    transactions = _scope_work_order_rows(transactions, request.user)
    reservations = _scope_work_order_rows(reservations, request.user)
    balances = StockBalance.objects.filter(organization=organization, part=part).select_related(
        "part", "bin__warehouse"
    )
    return Response(
        {
            "part": _part_json(
                part, include_financial=can_view_financials(request.user, request.auth)
            ),
            "balances": [row.to_dict() for row in balances],
            "reservations": [row.to_dict() for row in reservations],
            "transactions": [
                row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                for row in transactions[:500]
            ],
        }
    )


@api_view(["GET", "POST"])
def warehouses(request: Any) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = Warehouse.objects.filter(organization=organization).select_related("location")
        return Response({"warehouses": [row.to_dict() for row in rows]})
    _require(request, "inventory.transact")
    location = get_object_or_404(
        Location, organization=organization, pk=_resource_id(request.data, "location_id")
    )
    code = str(request.data.get("code", "")).strip().upper()
    name = str(request.data.get("name", "")).strip()
    if not code or not name:
        raise DomainError("code and name are required", code="required_fields")

    def handle() -> Response:
        try:
            row = Warehouse.objects.create(
                organization=organization, location=location, code=code, name=name
            )
        except IntegrityError as exc:
            raise DomainError(
                "Warehouse code already exists", code="duplicate_warehouse", status=409
            ) from exc
        audit(
            organization=organization,
            actor=request.user,
            action="warehouse.created",
            resource=row,
        )
        return Response({"warehouse": row.to_dict()}, status=201)

    return idempotent(request, handle)


@api_view(["GET", "POST"])
def bins(request: Any) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = Bin.objects.filter(organization=organization).select_related("warehouse")
        warehouse_id = request.query_params.get("warehouse_id")
        if warehouse_id:
            rows = rows.filter(warehouse_id=parse_resource_id(warehouse_id, "warehouse_id"))
        return Response({"bins": [row.to_dict() for row in rows]})
    _require(request, "inventory.transact")
    warehouse = get_object_or_404(
        Warehouse, organization=organization, pk=_resource_id(request.data, "warehouse_id")
    )
    code = str(request.data.get("code", "")).strip().upper()
    if not code:
        raise DomainError("code is required", code="required_fields")

    def handle() -> Response:
        try:
            row = Bin.objects.create(
                organization=organization,
                warehouse=warehouse,
                code=code,
                name=str(request.data.get("name", "")).strip(),
            )
        except IntegrityError as exc:
            raise DomainError(
                "Bin code already exists in this warehouse",
                code="duplicate_bin",
                status=409,
            ) from exc
        audit(organization=organization, actor=request.user, action="bin.created", resource=row)
        return Response({"bin": row.to_dict()}, status=201)

    return idempotent(request, handle)


@api_view(["GET"])
def bin_history(request: Any, bin_id: object) -> Response:
    _require(request, "inventory.view")
    organization = request.user.organization
    stock_bin = get_object_or_404(
        Bin.objects.select_related("warehouse"), organization=organization, pk=bin_id
    )
    rows = (
        StockTransaction.objects.filter(organization=organization, bin=stock_bin)
        .select_related("part", "bin__warehouse", "actor")
        .order_by("-created_at", "-id")
    )
    rows = _scope_work_order_rows(rows, request.user)
    return Response(
        {
            "bin": stock_bin.to_dict(),
            "transactions": [
                row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                for row in rows[:500]
            ],
        }
    )


@api_view(["GET"])
def stock(request: Any) -> Response:
    _require(request, "inventory.view")
    organization = request.user.organization
    rows = StockBalance.objects.filter(organization=organization).select_related(
        "part", "bin__warehouse"
    )
    if request.query_params.get("part_id"):
        rows = rows.filter(part_id=parse_resource_id(request.query_params["part_id"], "part_id"))
    if request.query_params.get("bin_id"):
        rows = rows.filter(bin_id=parse_resource_id(request.query_params["bin_id"], "bin_id"))
    mismatches = reconcile_inventory(organization)
    return Response(
        {
            "stock": [row.to_dict() for row in rows],
            "reconciled": not mismatches,
            "reconciliation_errors": mismatches,
        }
    )


@api_view(["GET", "POST"])
def reservations(request: Any) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = Reservation.objects.filter(organization=organization).select_related(
            "part", "bin__warehouse", "work_order"
        )
        rows = _scope_work_order_rows(rows, request.user)
        if request.query_params.get("work_order_id"):
            rows = rows.filter(
                work_order_id=parse_resource_id(
                    request.query_params["work_order_id"], "work_order_id"
                )
            )
        return Response({"reservations": [row.to_dict() for row in rows[:500]]})
    _require(request, "inventory.issue")

    def handle() -> Response:
        if request.data.get("action") == "release":
            row = get_object_or_404(
                Reservation,
                organization=organization,
                pk=_resource_id(request.data, "reservation_id"),
            )
            row = release_reservation(
                organization=organization,
                actor=request.user,
                reservation=row,
                reason=str(request.data.get("reason", "")),
            )
            return Response({"reservation": row.to_dict()})
        from maintenance.models import WorkOrder

        part = get_object_or_404(
            Part, organization=organization, pk=_resource_id(request.data, "part_id")
        )
        bin = get_object_or_404(
            Bin, organization=organization, pk=_resource_id(request.data, "bin_id")
        )
        work_order = get_object_or_404(
            WorkOrder,
            organization=organization,
            pk=_resource_id(request.data, "work_order_id"),
        )
        row = reserve_stock(
            organization=organization,
            actor=request.user,
            part=part,
            bin=bin,
            work_order=work_order,
            quantity=request.data.get("quantity"),
            operation_id=_operation_id(request),
            reason=str(request.data.get("reason", "")),
        )
        return Response({"reservation": row.to_dict()}, status=201)

    return idempotent(request, handle)


@api_view(["POST"])
def approve_count(request: Any, count_id: object) -> Response:
    _require(request, "inventory.adjust")
    if not can_manage_financials(request.user, request.auth):
        raise DomainError(
            "Permission required to approve inventory value: financial.manage",
            code="financial_permission_denied",
            status=403,
        )
    organization = request.user.organization
    count = get_object_or_404(
        InventoryCount,
        organization=organization,
        pk=count_id,
    )

    def handle() -> Response:
        row = approve_inventory_count(
            organization=organization,
            actor=request.user,
            count=count,
            operation_id=_operation_id(request),
        )
        return Response(
            {
                "count": row.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            }
        )

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )


def _transaction_rows(request: Any, transaction_type: str) -> Response:
    _require(request, "inventory.view")
    rows = StockTransaction.objects.filter(
        organization=request.user.organization, transaction_type=transaction_type
    ).select_related("part", "bin__warehouse", "actor")
    rows = _scope_work_order_rows(rows, request.user)
    return Response(
        {
            "transactions": [
                row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                for row in rows[:500]
            ]
        }
    )


@api_view(["GET", "POST"])
def issues(request: Any) -> Response:
    if request.method == "GET":
        return _transaction_rows(request, StockTransaction.Type.ISSUE)
    _require(request, "inventory.issue")
    organization = request.user.organization

    def handle() -> Response:
        from maintenance.models import WorkOrder

        reservation = None
        if request.data.get("reservation_id"):
            reservation = get_object_or_404(
                Reservation,
                organization=organization,
                pk=parse_resource_id(request.data["reservation_id"], "reservation_id"),
            )
        part = (
            reservation.part
            if reservation
            else get_object_or_404(
                Part, organization=organization, pk=_resource_id(request.data, "part_id")
            )
        )
        bin = (
            reservation.bin
            if reservation
            else get_object_or_404(
                Bin, organization=organization, pk=_resource_id(request.data, "bin_id")
            )
        )
        work_order = (
            reservation.work_order
            if reservation
            else get_object_or_404(
                WorkOrder,
                organization=organization,
                pk=_resource_id(request.data, "work_order_id"),
            )
        )
        row = issue_stock(
            organization=organization,
            actor=request.user,
            part=part,
            bin=bin,
            work_order=work_order,
            quantity=request.data.get("quantity"),
            operation_id=_operation_id(request),
            reservation=reservation,
            unit_cost=request.data.get("unit_cost"),
            reason=str(request.data.get("reason", "")),
            auth=request.auth,
        )
        return Response(
            {
                "transaction": row.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def returns(request: Any) -> Response:
    if request.method == "GET":
        return _transaction_rows(request, StockTransaction.Type.RETURN)
    _require(request, "inventory.return")
    organization = request.user.organization

    def handle() -> Response:
        original = get_object_or_404(
            StockTransaction,
            organization=organization,
            pk=_resource_id(request.data, "original_transaction_id"),
        )
        row = return_stock(
            organization=organization,
            actor=request.user,
            original=original,
            quantity=request.data.get("quantity"),
            operation_id=_operation_id(request),
            reason=str(request.data.get("reason", "")),
        )
        return Response(
            {
                "transaction": row.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def adjustments(request: Any) -> Response:
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = StockTransaction.objects.filter(
            organization=request.user.organization,
            transaction_type__in=[
                StockTransaction.Type.ADJUSTMENT,
                StockTransaction.Type.COUNT_ADJUSTMENT,
                StockTransaction.Type.REVERSAL,
            ],
        ).select_related("part", "bin__warehouse", "actor")
        rows = _scope_work_order_rows(rows, request.user)
        return Response(
            {
                "transactions": [
                    row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                    for row in rows[:500]
                ]
            }
        )
    _require(request, "inventory.adjust")
    if not can_manage_financials(request.user, request.auth):
        raise DomainError(
            "Permission required to adjust inventory value: financial.manage",
            code="financial_permission_denied",
            status=403,
        )
    organization = request.user.organization

    def handle() -> Response:
        reason = str(request.data.get("reason", ""))
        if request.data.get("original_transaction_id"):
            original = get_object_or_404(
                StockTransaction,
                organization=organization,
                pk=parse_resource_id(
                    request.data["original_transaction_id"], "original_transaction_id"
                ),
            )
            row = reverse_transaction(
                organization=organization,
                actor=request.user,
                original=original,
                operation_id=_operation_id(request),
                reason=reason,
            )
        else:
            part = get_object_or_404(
                Part, organization=organization, pk=_resource_id(request.data, "part_id")
            )
            bin = get_object_or_404(
                Bin, organization=organization, pk=_resource_id(request.data, "bin_id")
            )
            row = adjust_stock(
                organization=organization,
                actor=request.user,
                part=part,
                bin=bin,
                quantity=request.data.get("quantity"),
                operation_id=_operation_id(request),
                reason=reason,
                unit_cost=request.data.get("unit_cost", part.default_unit_cost),
            )
        return Response(
            {
                "transaction": row.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def counts(request: Any) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        _require(request, "inventory.view")
        rows = (
            InventoryCount.objects.filter(organization=organization)
            .select_related("warehouse")
            .prefetch_related("lines__part", "lines__bin__warehouse")
        )
        return Response(
            {
                "counts": [
                    row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                    for row in rows[:200]
                ]
            }
        )
    if not (
        has_permission(request.user, "inventory.count", request.auth)
        or has_permission(request.user, "inventory.adjust", request.auth)
    ):
        raise DomainError(
            "Permission required: inventory.count or inventory.adjust",
            code="permission_denied",
            status=403,
        )

    def handle() -> Response:
        warehouse = get_object_or_404(
            Warehouse,
            organization=organization,
            pk=_resource_id(request.data, "warehouse_id"),
        )
        raw_lines = request.data.get("lines")
        if not isinstance(raw_lines, list):
            raise DomainError("lines must be a list", code="invalid_lines")
        lines = []
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise DomainError("Every count line must be an object", code="invalid_line")
            lines.append(
                {
                    "part": get_object_or_404(
                        Part, organization=organization, pk=_resource_id(raw, "part_id")
                    ),
                    "bin": get_object_or_404(
                        Bin, organization=organization, pk=_resource_id(raw, "bin_id")
                    ),
                    "counted_quantity": raw.get("counted_quantity"),
                }
            )
        row = post_inventory_count(
            organization=organization,
            actor=request.user,
            warehouse=warehouse,
            lines=lines,
            operation_id=_operation_id(request),
            reason=str(request.data.get("reason", "")),
            can_adjust=(
                has_permission(request.user, "inventory.adjust", request.auth)
                and can_manage_financials(request.user, request.auth)
            ),
        )
        return Response(
            {
                "count": row.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        handle,
        replay_response_transform=_financial_replay_transform(request),
    )
