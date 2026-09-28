from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.exceptions import DomainError
from core.models import ApiToken, AuditEvent, Location, Organization, User
from core.permissions import has_permission
from core.services import idempotent
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    Asset,
    AssetStatusEvent,
    AssetType,
    Component,
    ComponentInstallation,
    Meter,
    MeterReading,
    normalize_external_identity,
)
from .services import (
    change_asset_status,
    correct_meter_reading,
    create_asset,
    create_meter,
    install_component,
    record_meter_reading,
    remove_component,
    update_asset,
    upsert_external_asset,
)


def _allowed(request: Any, *permissions: str) -> bool:
    return any(has_permission(request.user, permission, request.auth) for permission in permissions)


def _require(request: Any, *permissions: str) -> None:
    if not _allowed(request, *permissions):
        raise DomainError("Asset access is not allowed", code="permission_denied", status=403)


def _require_external_token(request: Any) -> None:
    if not isinstance(request.auth, ApiToken):
        raise DomainError(
            "A scoped API token is required for external asset writes",
            code="api_token_required",
            status=403,
        )
    _require(request, "assets.sync")


def _external_identity(source_system: object, external_id: object) -> tuple[str, str]:
    try:
        return normalize_external_identity(source_system, external_id)
    except ValidationError as exc:
        details = getattr(exc, "message_dict", None) or getattr(exc, "messages", None)
        raise DomainError(
            "External asset identity is invalid",
            code="invalid_external_identity",
            details=details,
        ) from exc


def _external_asset_fields(request: Any, existing: Asset | None) -> dict[str, Any]:
    data = request.data
    allowed = {
        "asset_type_id",
        "asset_type",
        "asset_type_category",
        "unit_number",
        "vin",
        "serial_number",
        "year",
        "make",
        "model",
        "source_details",
    }
    if unknown := sorted(set(data) - allowed):
        raise DomainError(
            "Unsupported external asset fields", code="unsupported_fields", details=unknown
        )
    if data.get("asset_type_id") and data.get("asset_type"):
        raise DomainError("Use asset_type_id or asset_type, not both", code="ambiguous_asset_type")
    organization = request.user.organization
    fields: dict[str, Any] = {}
    if "asset_type_id" in data:
        if not data.get("asset_type_id"):
            raise DomainError("asset_type_id cannot be empty", code="asset_type_required")
        fields["asset_type"] = get_object_or_404(
            AssetType, pk=data["asset_type_id"], organization=organization
        )
    elif "asset_type" in data:
        name = str(data.get("asset_type") or "").strip()
        if not name:
            raise DomainError("asset_type cannot be empty", code="asset_type_required")
        category = str(data.get("asset_type_category") or "vehicle").strip() or "vehicle"
        fields["asset_type"], _ = AssetType.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={"category": category[:40]},
        )
    elif existing is None:
        raise DomainError("asset_type_id or asset_type is required", code="asset_type_required")
    elif "asset_type_category" in data:
        raise DomainError("asset_type_category requires asset_type", code="asset_type_required")

    if "unit_number" in data:
        unit_number = str(data.get("unit_number") or "").strip()
        if not unit_number:
            raise DomainError("unit_number cannot be empty", code="unit_number_required")
        fields["unit_number"] = unit_number
    elif existing is None:
        raise DomainError("unit_number is required", code="unit_number_required")
    for name in ("vin", "serial_number", "make", "model"):
        if name in data:
            fields[name] = "" if data[name] is None else str(data[name]).strip()
    if "year" in data:
        try:
            fields["year"] = int(data["year"]) if data["year"] not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise DomainError("year must be a number", code="invalid_year") from exc
    if "source_details" in data:
        details = data["source_details"]
        if not isinstance(details, dict):
            raise DomainError("source_details must be an object", code="invalid_source_details")
        if len(details) > 32 or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", key)
            for key in details
        ):
            raise DomainError(
                "source_details keys are invalid or exceed the 32-key limit",
                code="invalid_source_details",
            )
        if len(json.dumps(details, sort_keys=True, separators=(",", ":")).encode()) > 8192:
            raise DomainError(
                "source_details exceeds the 8 KiB limit", code="invalid_source_details"
            )
        fields["source_details"] = details
    return fields


def _asset_queryset(request: Any):
    rows = Asset.objects.filter(organization=request.user.organization).select_related(
        "asset_type", "home_location", "assigned_driver"
    )
    if has_permission(request.user, "assets.view", request.auth):
        return rows
    if has_permission(request.user, "assets.assigned", request.auth):
        return rows.filter(assigned_driver=request.user)
    raise DomainError("Asset access is not allowed", code="permission_denied", status=403)


def _datetime(value: object) -> datetime:
    parsed = parse_datetime(str(value)) if value else None
    if not parsed or parsed.tzinfo is None:
        raise DomainError(
            "observed_at must be an ISO 8601 timestamp with timezone", code="invalid_observed_at"
        )
    return parsed


def _asset_maintenance_timeline(asset: Asset) -> list[dict[str, object]]:
    from maintenance.models import Defect, MaintenanceRequest, WorkOrder, WorkOrderTask

    organization = asset.organization
    status_events = AssetStatusEvent.objects.filter(organization=organization, asset=asset)
    defects = list(Defect.objects.filter(organization=organization, asset=asset))
    requests = list(MaintenanceRequest.objects.filter(organization=organization, asset=asset))
    work_orders = list(
        WorkOrder.objects.filter(organization=organization, asset=asset).select_related("request")
    )
    tasks = WorkOrderTask.objects.filter(
        organization=organization,
        work_order__organization=organization,
        work_order__in=work_orders,
    ).select_related("work_order", "work_order__request")
    entries: list[tuple[datetime, dict[str, object]]] = []

    def add(
        *,
        entry_type: str,
        record_id: object,
        label: str,
        status: str,
        occurred_at: datetime,
        number: str | None = None,
        reason: str = "",
        source_type: str | None = None,
        source_id: object | None = None,
        links: Mapping[str, object | None] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        entries.append(
            (
                occurred_at,
                {
                    "type": entry_type,
                    "id": str(record_id),
                    "number": number,
                    "label": label,
                    "status": status,
                    "reason": reason,
                    "occurred_at": occurred_at,
                    "source": (
                        {"type": source_type, "id": str(source_id) if source_id else None}
                        if source_type
                        else None
                    ),
                    "links": {
                        key: str(value) for key, value in (links or {}).items() if value is not None
                    },
                    "context": dict(context or {}),
                },
            )
        )

    for row in ComponentInstallation.objects.filter(
        organization=organization, asset=asset
    ).select_related("component"):
        label = f"{row.component.get_kind_display()} {row.component.serial_number}"
        add(
            entry_type="component_installed",
            record_id=row.pk,
            number=asset.unit_number,
            label=f"{label} installed",
            status="installed",
            occurred_at=row.installed_at,
            source_type=row.source,
            source_id=row.installed_by_id,
            context={"installed_meters": row.installed_meters},
            links={
                "asset_id": asset.pk,
                "component_id": row.component_id,
                "component_installation_id": row.pk,
                "work_order_id": row.installed_work_order_id,
            },
        )
        if row.removed_at is not None:
            add(
                entry_type="component_removed",
                record_id=row.pk,
                number=asset.unit_number,
                label=f"{label} removed",
                status="removed",
                reason=row.removal_reason,
                occurred_at=row.removed_at,
                source_type=row.source,
                source_id=row.removed_by_id,
                context={"removed_meters": row.removed_meters},
                links={
                    "asset_id": asset.pk,
                    "component_id": row.component_id,
                    "component_installation_id": row.pk,
                    "work_order_id": row.removed_work_order_id,
                },
            )

    for status_event in status_events:
        add(
            entry_type="asset_status",
            record_id=status_event.pk,
            number=asset.unit_number,
            label=f"Asset status changed to {status_event.get_new_status_display()}",
            status=status_event.new_status,
            reason=status_event.reason,
            occurred_at=status_event.occurred_at,
            source_type=status_event.source,
            source_id=status_event.actor_id,
            context=status_event.context,
            links={
                "asset_id": asset.pk,
                "asset_status_event_id": status_event.pk,
            },
        )

    for defect in defects:
        source_type = "inspection_finding" if defect.inspection_finding_id else None
        source_id = defect.inspection_finding_id
        if not source_id and defect.inspection_response_id:
            source_type, source_id = "inspection_response", defect.inspection_response_id
        add(
            entry_type="defect",
            record_id=defect.pk,
            label=f"{defect.category}: {defect.description}"[:300],
            status=defect.status,
            reason=defect.disposition_reason,
            occurred_at=defect.created_at,
            source_type=source_type,
            source_id=source_id,
            links={"asset_id": asset.pk, "defect_id": defect.pk},
        )

    for maintenance_request in requests:
        source_type = "defect" if maintenance_request.defect_id else None
        source_id = maintenance_request.defect_id
        if not source_id and maintenance_request.alert_id:
            source_type, source_id = "maintenance_alert", maintenance_request.alert_id
        add(
            entry_type="maintenance_request",
            record_id=maintenance_request.pk,
            label=maintenance_request.summary,
            status=maintenance_request.status,
            reason=maintenance_request.decision_reason,
            occurred_at=maintenance_request.created_at,
            source_type=source_type,
            source_id=source_id,
            links={
                "asset_id": asset.pk,
                "defect_id": maintenance_request.defect_id,
                "maintenance_alert_id": maintenance_request.alert_id,
                "maintenance_request_id": maintenance_request.pk,
            },
        )

    for work_order in work_orders:
        request = work_order.request
        source_type = "maintenance_request" if work_order.request_id else None
        source_id = work_order.request_id
        if not source_id and work_order.maintenance_plan_id:
            source_type, source_id = "maintenance_plan", work_order.maintenance_plan_id
        links = {
            "asset_id": asset.pk,
            "defect_id": request.defect_id if request else None,
            "maintenance_request_id": work_order.request_id,
            "maintenance_plan_id": work_order.maintenance_plan_id,
            "work_order_id": work_order.pk,
        }
        add(
            entry_type="work_order",
            record_id=work_order.pk,
            number=work_order.number,
            label=work_order.summary,
            status=work_order.status,
            reason=work_order.blocked_reason or work_order.reopen_reason,
            occurred_at=work_order.created_at,
            source_type=source_type,
            source_id=source_id,
            links=links,
        )
        if work_order.completed_at:
            add(
                entry_type="work_order_completion",
                record_id=work_order.pk,
                number=work_order.number,
                label=work_order.completion_summary or f"{work_order.number} completed",
                status="Completed",
                reason=work_order.completion_summary,
                occurred_at=work_order.completed_at,
                source_type="work_order",
                source_id=work_order.pk,
                links=links,
            )
        if work_order.closed_at:
            add(
                entry_type="work_order_closure",
                record_id=work_order.pk,
                number=work_order.number,
                label=f"{work_order.number} verified and closed",
                status="Closed",
                occurred_at=work_order.closed_at,
                source_type="work_order",
                source_id=work_order.pk,
                links=links,
            )

    for task in tasks:
        work_order = task.work_order
        request = work_order.request
        add(
            entry_type="work_order_task",
            record_id=task.pk,
            number=work_order.number,
            label=task.title,
            status=task.status,
            reason=task.notes,
            occurred_at=task.completed_at or task.created_at,
            source_type="work_order",
            source_id=work_order.pk,
            links={
                "asset_id": asset.pk,
                "defect_id": request.defect_id if request else None,
                "maintenance_request_id": work_order.request_id,
                "work_order_id": work_order.pk,
                "work_order_task_id": task.pk,
            },
        )

    return [entry for _, entry in sorted(entries, key=lambda item: (item[0], item[1]["type"]))]


def _foreign_keys(
    org: object, data: dict[str, Any]
) -> tuple[AssetType | None, Location | None, User | None]:
    asset_type = None
    if data.get("asset_type_id"):
        asset_type = get_object_or_404(AssetType, pk=data["asset_type_id"], organization=org)
    location = None
    if data.get("home_location_id"):
        location = get_object_or_404(Location, pk=data["home_location_id"], organization=org)
    driver = None
    if data.get("assigned_driver_id"):
        driver = get_object_or_404(
            User, pk=data["assigned_driver_id"], organization=org, is_active=True
        )
    return asset_type, location, driver


@api_view(["GET", "POST"])
def assets_collection(request: Any) -> Response:
    org = request.user.organization
    if request.method == "GET":
        rows = _asset_queryset(request)
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        if request.query_params.get("q"):
            query = request.query_params["q"].strip()
            rows = rows.filter(
                Q(unit_number__icontains=query)
                | Q(vin__icontains=query)
                | Q(serial_number__icontains=query)
                | Q(make__icontains=query)
                | Q(model__icontains=query)
            )
        if request.query_params.get("include_archived") != "true":
            rows = rows.filter(archived_at__isnull=True)
        return Response(
            {
                "assets": [row.to_dict() for row in rows[:500]],
                "asset_types": [
                    row.to_dict() for row in AssetType.objects.filter(organization=org)
                ],
            }
        )

    _require(request, "assets.manage")

    def handler() -> Response:
        data = request.data
        asset_type, location, driver = _foreign_keys(org, data)
        if asset_type is None:
            type_name = str(data.get("asset_type", "")).strip()
            if not type_name:
                raise DomainError(
                    "asset_type_id or asset_type is required", code="asset_type_required"
                )
            asset_type, _ = AssetType.objects.get_or_create(
                organization=org,
                name=type_name,
                defaults={"category": str(data.get("asset_type_category", "vehicle"))[:40]},
            )
        unit_number = str(data.get("unit_number", "")).strip()
        if not unit_number:
            raise DomainError("unit_number is required", code="unit_number_required")
        specs = data.get("specs", {})
        if not isinstance(specs, dict):
            raise DomainError("specs must be an object", code="invalid_specs")
        year = data.get("year") or None
        if year is not None:
            try:
                year = int(year)
            except (TypeError, ValueError) as exc:
                raise DomainError("year must be a number", code="invalid_year") from exc
        asset = create_asset(
            organization=org,
            actor=request.user,
            asset_type=asset_type,
            home_location=location,
            assigned_driver=driver,
            unit_number=unit_number,
            vin=str(data.get("vin", "")),
            serial_number=str(data.get("serial_number", "")),
            year=year,
            make=str(data.get("make", "")).strip(),
            model=str(data.get("model", "")).strip(),
            ownership=str(data.get("ownership", "")).strip(),
            specs=specs,
            status=str(data.get("status", Asset.Status.AVAILABLE)),
            status_reason=str(data.get("status_reason", "Initial asset status")),
        )
        return Response({"asset": asset.to_dict()}, status=201)

    return idempotent(request, handler)


@api_view(["GET", "PUT"])
def external_asset_detail(request: Any, source_system: str, external_id: str) -> Response:
    source, identifier = _external_identity(source_system, external_id)
    organization = request.user.organization
    rows = Asset.objects.filter(
        organization=organization,
        source_system=source,
        external_id=identifier,
    ).select_related("asset_type", "home_location", "assigned_driver")
    if request.method == "GET":
        _require(request, "assets.view")
        asset = get_object_or_404(rows)
        return Response(
            {
                "asset": asset.to_dict(),
                "deep_link": f"/assets/{asset.pk}",
                "schedule_link": f"/schedule?asset_id={asset.pk}",
            }
        )

    _require_external_token(request)

    def handler() -> Response:
        existing = rows.first()
        asset, created = upsert_external_asset(
            organization=organization,
            actor=request.user,
            source_system=source,
            external_id=identifier,
            fields=_external_asset_fields(request, existing),
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        return Response(
            {
                "asset": asset.to_dict(),
                "created": created,
                "deep_link": f"/assets/{asset.pk}",
                "schedule_link": f"/schedule?asset_id={asset.pk}",
            },
            status=201 if created else 200,
        )

    return idempotent(request, handler)


@api_view(["POST"])
def external_asset_meter_readings(request: Any, source_system: str, external_id: str) -> Response:
    _require_external_token(request)
    source, identifier = _external_identity(source_system, external_id)
    asset = get_object_or_404(
        Asset.objects.select_related("organization"),
        organization=request.user.organization,
        source_system=source,
        external_id=identifier,
    )

    def handler() -> Response:
        data = request.data
        allowed = {
            "meter_id",
            "kind",
            "name",
            "unit",
            "value",
            "observed_at",
            "external_id",
            "reason",
        }
        if unknown := sorted(set(data) - allowed):
            raise DomainError(
                "Unsupported external meter fields", code="unsupported_fields", details=unknown
            )
        reading_external_id = str(data.get("external_id", "")).strip()
        if not reading_external_id:
            raise DomainError(
                "external_id is required for an external meter reading",
                code="external_id_required",
            )
        if "value" not in data:
            raise DomainError("value is required", code="meter_value_required")
        # ponytail: an organization-wide row lock is enough for the current fleet size;
        # use a dedicated external-ID lock table if ingest throughput becomes material.
        Organization.objects.select_for_update().get(pk=asset.organization_id)
        existing_reading = (
            MeterReading.objects.select_related("meter")
            .filter(
                organization=asset.organization,
                source=source,
                external_id=reading_external_id,
            )
            .first()
        )
        if existing_reading and existing_reading.meter.asset_id != asset.pk:
            raise DomainError(
                "This external reading ID already belongs to another asset",
                code="duplicate_meter_reading_conflict",
                status=409,
            )
        meter = existing_reading.meter if existing_reading else None
        if data.get("meter_id"):
            requested_meter = (
                meter
                if meter and str(meter.pk) == str(data["meter_id"])
                else get_object_or_404(
                    Meter,
                    pk=data["meter_id"],
                    asset=asset,
                    organization=asset.organization,
                    active=True,
                )
            )
            if meter and requested_meter.pk != meter.pk:
                raise DomainError(
                    "This external reading ID already belongs to another meter",
                    code="duplicate_meter_reading_conflict",
                    status=409,
                )
            meter = requested_meter
        if meter is not None:
            if (
                (data.get("kind") and str(data["kind"]).strip() != meter.kind)
                or (data.get("unit") and str(data["unit"]).strip() != meter.unit)
                or (data.get("name") and str(data["name"]).strip() != meter.name)
            ):
                raise DomainError(
                    "The supplied meter definition does not match meter_id",
                    code="meter_definition_conflict",
                    status=409,
                )
        if meter is None:
            kind = str(data.get("kind", "")).strip()
            if kind not in Meter.Kind.values:
                raise DomainError("A valid meter kind is required", code="invalid_meter_kind")
            name = str(data.get("name") or dict(Meter.Kind.choices)[kind]).strip()
            unit = str(data.get("unit", "")).strip()
            meter = Meter.objects.filter(
                organization=asset.organization, asset=asset, name=name
            ).first()
            if meter and (not meter.active or meter.kind != kind or meter.unit != unit):
                raise DomainError(
                    "The existing meter name has a different definition",
                    code="meter_definition_conflict",
                    status=409,
                )
            if meter is None:
                meter = create_meter(
                    organization=asset.organization,
                    asset=asset,
                    actor=request.user,
                    name=name,
                    kind=kind,
                    unit=unit,
                )
        maximum_rate: object | None = None
        if meter.kind == Meter.Kind.ODOMETER:
            maximum_rate = settings.METER_MAX_MILES_PER_HOUR
            if meter.unit == "km":
                maximum_rate = Decimal(str(maximum_rate)) * Decimal("1.609344")
        elif meter.kind == Meter.Kind.ENGINE_HOURS:
            maximum_rate = settings.METER_MAX_ENGINE_HOURS_PER_HOUR
        reading = record_meter_reading(
            meter=meter,
            value=data["value"],
            observed_at=_datetime(data.get("observed_at")),
            source=source,
            actor=request.user,
            external_id=reading_external_id,
            provenance={"entered_via": "external_asset_api", "source_system": source},
            reason=str(data.get("reason", "")),
            maximum_rate_per_hour=maximum_rate,
            future_tolerance_seconds=settings.METER_FUTURE_TOLERANCE_SECONDS,
        )
        return Response({"meter": meter.to_dict(), "reading": reading.to_dict()}, status=201)

    return idempotent(request, handler)


@api_view(["GET", "PATCH"])
def asset_detail(request: Any, asset_id: object) -> Response:
    asset = get_object_or_404(_asset_queryset(request), pk=asset_id)
    if request.method == "GET":
        return Response({"asset": asset.to_dict()})
    _require(request, "assets.manage")

    def handler() -> Response:
        data = request.data
        allowed = {
            "unit_number",
            "vin",
            "serial_number",
            "year",
            "make",
            "model",
            "ownership",
            "specs",
            "asset_type_id",
            "home_location_id",
            "assigned_driver_id",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise DomainError(
                "Unsupported asset fields", code="unsupported_fields", details=unknown
            )
        source_owned = {
            "unit_number",
            "vin",
            "serial_number",
            "year",
            "make",
            "model",
            "asset_type_id",
        }
        if asset.external_id and (owned_fields := sorted(set(data) & source_owned)):
            raise DomainError(
                "Externally linked master fields must be changed through the external asset API",
                code="external_asset_field_owned",
                status=409,
                details={"fields": owned_fields, "source_system": asset.source_system},
            )
        fields: dict[str, Any] = {}
        for name in ("unit_number", "vin", "serial_number", "make", "model", "ownership"):
            if name in data:
                fields[name] = str(data[name]).strip()
        if "year" in data:
            try:
                fields["year"] = int(data["year"]) if data["year"] not in (None, "") else None
            except (TypeError, ValueError) as exc:
                raise DomainError("year must be a number", code="invalid_year") from exc
        if "specs" in data:
            if not isinstance(data["specs"], dict):
                raise DomainError("specs must be an object", code="invalid_specs")
            specs = dict(data["specs"])
            if asset.external_id:
                current_integrations = asset.specs.get("integrations", {})
                if not isinstance(current_integrations, dict):
                    raise DomainError(
                        "Existing integration details cannot be changed safely",
                        code="invalid_local_specs",
                        status=409,
                    )
                source_details = current_integrations.get(asset.source_system, {})
                if not isinstance(source_details, dict):
                    raise DomainError(
                        "Existing source details cannot be changed safely",
                        code="invalid_local_specs",
                        status=409,
                    )
                requested_integrations = specs.get("integrations", {})
                if not isinstance(requested_integrations, dict):
                    raise DomainError("specs.integrations must be an object", code="invalid_specs")
                requested_source = requested_integrations.get(asset.source_system, source_details)
                if requested_source != source_details:
                    raise DomainError(
                        "Externally linked source details must be changed through "
                        "the external asset API",
                        code="external_asset_field_owned",
                        status=409,
                        details={"field": f"specs.integrations.{asset.source_system}"},
                    )
                if (
                    asset.source_system in current_integrations
                    or asset.source_system in requested_integrations
                ):
                    specs["integrations"] = {
                        **requested_integrations,
                        asset.source_system: source_details,
                    }
            fields["specs"] = specs
        asset_type, location, driver = _foreign_keys(asset.organization, data)
        if "asset_type_id" in data:
            if asset_type is None:
                raise DomainError("asset_type_id cannot be empty", code="asset_type_required")
            fields["asset_type"] = asset_type
        if "home_location_id" in data:
            fields["home_location"] = location
        if "assigned_driver_id" in data:
            fields["assigned_driver"] = driver
        if not fields:
            return Response({"asset": asset.to_dict()})
        update_asset(asset=asset, actor=request.user, fields=fields)
        return Response({"asset": asset.to_dict()})

    return idempotent(request, handler)


@api_view(["GET"])
def asset_history(request: Any, asset_id: object) -> Response:
    asset = get_object_or_404(_asset_queryset(request), pk=asset_id)
    status_events = asset.status_events.select_related("actor")
    meters = asset.meters.prefetch_related("readings")
    audit_events: list[dict[str, object]] = []
    if has_permission(request.user, "audit.view", request.auth):
        rows = AuditEvent.objects.filter(
            organization=asset.organization, resource_type="Asset", resource_id=str(asset.pk)
        )[:500]
        audit_events = [
            {
                "id": str(row.pk),
                "action": row.action,
                "previous_state": row.previous_state,
                "new_state": row.new_state,
                "context": row.context,
                "occurred_at": row.occurred_at,
            }
            for row in rows
        ]
    return Response(
        {
            "asset": asset.to_dict(include_meters=False),
            "status_events": [event.to_dict() for event in status_events],
            "meters": [meter.to_dict(include_readings=True) for meter in meters],
            "audit_events": audit_events,
            "component_installations": [
                row.to_dict()
                for row in _installation_rows(organization=asset.organization, asset=asset)
            ],
            "timeline": _asset_maintenance_timeline(asset),
        }
    )


@api_view(["GET", "POST"])
def meter_readings(request: Any, asset_id: object) -> Response:
    asset = get_object_or_404(_asset_queryset(request), pk=asset_id)
    if request.method == "GET":
        return Response(
            {"meters": [meter.to_dict(include_readings=True) for meter in asset.meters.all()]}
        )
    _require(request, "assets.manage", "assets.status", "maintenance.manage", "maintenance.execute")

    def handler() -> Response:
        data = request.data
        if "source" in data:
            raise DomainError(
                "Meter source is assigned by the endpoint", code="meter_source_not_allowed"
            )
        meter = None
        if data.get("meter_id"):
            meter = get_object_or_404(
                Meter, pk=data["meter_id"], asset=asset, organization=asset.organization
            )
        if meter is None:
            kind = str(data.get("kind", "")).strip()
            if kind not in Meter.Kind.values:
                raise DomainError("A valid meter kind is required", code="invalid_meter_kind")
            meter = create_meter(
                organization=asset.organization,
                asset=asset,
                actor=request.user,
                name=str(data.get("name") or dict(Meter.Kind.choices)[kind]).strip(),
                kind=kind,
                unit=str(data.get("unit", "")).strip(),
            )
        if "value" not in data:
            return Response({"meter": meter.to_dict()}, status=201)
        reading = record_meter_reading(
            meter=meter,
            value=data["value"],
            observed_at=_datetime(data.get("observed_at")),
            source="manual",
            actor=request.user,
            external_id=str(data.get("external_id", "")),
            provenance={"entered_via": "asset_api"},
            reason=str(data.get("reason", "")),
        )
        return Response({"meter": meter.to_dict(), "reading": reading.to_dict()}, status=201)

    return idempotent(request, handler)


@api_view(["POST"])
def correct_meter(request: Any, reading_id: object) -> Response:
    _require(request, "assets.manage", "assets.status")
    reading = get_object_or_404(
        MeterReading.objects.select_related("meter", "meter__asset"),
        pk=reading_id,
        organization=request.user.organization,
    )

    def handler() -> Response:
        if "value" not in request.data:
            raise DomainError("value is required", code="meter_value_required")
        corrected = correct_meter_reading(
            reading=reading,
            value=request.data["value"],
            reason=str(request.data.get("reason", "")),
            actor=request.user,
        )
        return Response({"reading": corrected.to_dict(), "meter": corrected.meter.to_dict()})

    return idempotent(request, handler)


@api_view(["POST"])
def change_availability(request: Any, asset_id: object) -> Response:
    asset = get_object_or_404(
        Asset.objects.select_related("organization"),
        pk=asset_id,
        organization=request.user.organization,
    )
    new_status = str(request.data.get("status", ""))
    if new_status == Asset.Status.RETIRED:
        _require(request, "assets.manage")
    else:
        _require(request, "assets.status")

    def handler() -> Response:
        final_meter_readings: list[MeterReading] = []
        if new_status == Asset.Status.RETIRED:
            raw_reading_ids = request.data.get("final_meter_reading_ids", [])
            if not isinstance(raw_reading_ids, list):
                raise DomainError(
                    "final_meter_reading_ids must be a list",
                    code="invalid_final_meter_readings",
                )
            try:
                reading_ids = [uuid.UUID(str(value)) for value in raw_reading_ids]
            except (AttributeError, TypeError, ValueError) as exc:
                raise DomainError(
                    "final_meter_reading_ids must contain UUIDs",
                    code="invalid_final_meter_readings",
                ) from exc
            if len(reading_ids) != len(set(reading_ids)):
                raise DomainError(
                    "Each final meter reading may be supplied only once",
                    code="invalid_final_meter_readings",
                )
            final_meter_readings = list(
                MeterReading.objects.filter(
                    organization=request.user.organization,
                    pk__in=reading_ids,
                )
            )
            if len(final_meter_readings) != len(reading_ids):
                raise DomainError(
                    "A final meter reading is invalid or outside this organization",
                    code="invalid_final_meter_readings",
                )
        event = change_asset_status(
            asset=asset,
            new_status=new_status,
            reason=str(request.data.get("reason", "")),
            actor=request.user,
            classification=str(request.data.get("classification", "company")),
            override_reason=str(request.data.get("override_reason", "")),
            disposition=str(request.data.get("disposition", "")),
            final_meter_readings=final_meter_readings,
        )
        return Response({"asset": asset.to_dict(), "status_event": event.to_dict()})

    return idempotent(request, handler)


def _component_queryset(request: Any):
    if not has_permission(request.user, "assets.view", request.auth):
        raise DomainError("Component access is not allowed", code="permission_denied", status=403)
    return Component.objects.filter(organization=request.user.organization)


def _installation_rows(**filters: Any):
    return ComponentInstallation.objects.filter(**filters).select_related(
        "component",
        "asset",
        "installed_by",
        "removed_by",
        "installed_work_order",
        "removed_work_order",
    )


def _require_component_write(request: Any) -> None:
    """The service layer draws the technician/manager line; this is the coarse gate."""
    if not _allowed(request, "assets.manage", "maintenance.manage", "maintenance.execute"):
        raise DomainError("Not allowed to change components", code="permission_denied", status=403)


@api_view(["GET", "POST"])
def asset_components(request: Any, asset_id: object) -> Response:
    asset = get_object_or_404(_asset_queryset(request), pk=asset_id)
    if request.method == "GET":
        rows = _installation_rows(organization=asset.organization, asset=asset)
        return Response({"installations": [row.to_dict() for row in rows]})

    _require_component_write(request)
    allowed = {
        "component_id",
        "kind",
        "serial_number",
        "manufacturer",
        "model",
        "installed_at",
        "work_order_id",
    }
    if unsupported := sorted(set(request.data) - allowed):
        raise DomainError(
            "Unsupported fields", code="unsupported_fields", details={"fields": unsupported}
        )

    def handler() -> Response:
        from maintenance.models import WorkOrder

        component = None
        if request.data.get("component_id"):
            component = get_object_or_404(
                Component.objects.filter(organization=asset.organization),
                pk=request.data["component_id"],
            )
        work_order = None
        if request.data.get("work_order_id"):
            work_order = get_object_or_404(
                WorkOrder.objects.filter(organization=asset.organization),
                pk=request.data["work_order_id"],
            )
        installation = install_component(
            asset=asset,
            actor=request.user,
            component=component,
            kind=str(request.data.get("kind") or ""),
            serial_number=str(request.data.get("serial_number") or ""),
            manufacturer=str(request.data.get("manufacturer") or ""),
            model=str(request.data.get("model") or ""),
            installed_at=(
                _datetime(request.data["installed_at"])
                if request.data.get("installed_at")
                else None
            ),
            work_order=work_order,
        )
        return Response(
            {
                "installation": installation.to_dict(),
                "component": installation.component.to_dict(),
            },
            status=201,
        )

    return idempotent(request, handler)


@api_view(["GET"])
def component_detail(request: Any, component_id: object) -> Response:
    from maintenance.models import WorkOrder, WorkOrderTask

    component = get_object_or_404(_component_queryset(request), pk=component_id)
    installations = list(_installation_rows(component=component))
    tasks = (
        WorkOrderTask.objects.filter(organization=component.organization, component=component)
        .select_related("work_order", "work_order__asset")
        .order_by("-created_at")
    )
    work_order_ids = {
        row.installed_work_order_id for row in installations if row.installed_work_order_id
    } | {row.removed_work_order_id for row in installations if row.removed_work_order_id}
    work_orders = WorkOrder.objects.filter(
        organization=component.organization, pk__in=work_order_ids
    ).order_by("-created_at")
    return Response(
        {
            "component": component.to_dict(),
            "installations": [row.to_dict() for row in installations],
            "tasks": [
                {
                    "task_id": str(task.pk),
                    "work_order_id": str(task.work_order_id),
                    "work_order_number": task.work_order.number,
                    "title": task.title,
                    "status": task.status,
                    "completed_at": task.completed_at,
                    "asset_id": str(task.work_order.asset_id),
                    "unit_number": task.work_order.asset.unit_number,
                }
                for task in tasks
            ],
            "work_orders": [
                {
                    "id": str(row.pk),
                    "number": row.number,
                    "summary": row.summary,
                    "status": row.status,
                }
                for row in work_orders
            ],
        }
    )


@api_view(["POST"])
def remove_installed_component(request: Any, component_id: object) -> Response:
    component = get_object_or_404(_component_queryset(request), pk=component_id)
    _require_component_write(request)
    if unsupported := sorted(set(request.data) - {"reason", "removed_at", "work_order_id"}):
        raise DomainError(
            "Unsupported fields", code="unsupported_fields", details={"fields": unsupported}
        )

    def handler() -> Response:
        from maintenance.models import WorkOrder

        work_order = None
        if request.data.get("work_order_id"):
            work_order = get_object_or_404(
                WorkOrder.objects.filter(organization=component.organization),
                pk=request.data["work_order_id"],
            )
        installation = remove_component(
            component=component,
            actor=request.user,
            reason=str(request.data.get("reason") or ""),
            removed_at=(
                _datetime(request.data["removed_at"]) if request.data.get("removed_at") else None
            ),
            work_order=work_order,
        )
        return Response({"installation": installation.to_dict()})

    return idempotent(request, handler)
