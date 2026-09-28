from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from assets.models import Asset, Component, MeterReading
from core.exceptions import DomainError
from core.models import ApiToken, User
from core.permissions import (
    can_manage_financials,
    can_view_financials,
    has_permission,
    redact_financial_fields,
)
from core.services import audit, idempotent
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Prefetch, Q, QuerySet, Value, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .models import (
    Defect,
    ExternalEmployeeProjection,
    Inspection,
    InspectionTemplate,
    LaborEntry,
    MaintenanceAlert,
    MaintenancePlan,
    MaintenanceRequest,
    ServicePackage,
    WorkOrder,
    WorkOrderAssignment,
    WorkOrderTask,
    normalize_external_employee_identity,
)
from .services import (
    UNSET,
    _component_on_asset,
    _recalculate_plans,
    _reject_financial_payload,
    can_access_all_maintenance_assets,
    can_view_all_work_orders,
    create_defect,
    create_inspection,
    create_labor_entry,
    create_maintenance_plan,
    create_request,
    create_service_package,
    create_work_order,
    filter_work_orders_for_assignee,
    is_active_work_order_assignee,
    replace_work_order_assignments,
    sync_legacy_work_order_lead,
    transition_defect,
    transition_request,
    transition_work_order,
    update_work_order_task,
    upsert_external_employee_projection,
    void_inspection_record,
)


def _require_any(request: Request, *permissions: str) -> None:
    if not any(
        has_permission(request.user, permission, request.auth) for permission in permissions
    ):
        raise DomainError("This action is not allowed", code="permission_denied", status=403)


def _financial_replay_transform(request: Request) -> Callable[[object], object] | None:
    """Redact a prior idempotent result for a now-less-privileged credential."""

    if can_view_financials(request.user, request.auth):
        return None
    return redact_financial_fields


def _body_list(value: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DomainError(f"{field} must be a list", code="invalid_payload")
    return value


def _parse_datetime(value: object, *, required: bool = False) -> datetime | None:
    if value in (None, ""):
        if required:
            raise DomainError("A date and time is required", code="datetime_required")
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise DomainError("Date and time must be ISO 8601", code="invalid_datetime")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _require_external_personnel_token(request: Request) -> None:
    if not isinstance(request.auth, ApiToken):
        raise DomainError(
            "A scoped API token is required for external personnel writes",
            code="api_token_required",
            status=403,
        )
    _require_any(request, "personnel.sync")


def _external_employee_payload(data: Any) -> dict[str, object]:
    if not isinstance(data, dict):
        raise DomainError("Personnel payload must be an object", code="invalid_payload")
    allowed = {
        "display_name",
        "active",
        "source_version",
        "source_updated_at",
        "external_user_id",
        "job_title",
        "department",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise DomainError(
            "Unsupported external employee fields", code="unsupported_fields", details=unknown
        )
    required = {"display_name", "active", "source_version", "source_updated_at"}
    missing = sorted(name for name in required if name not in data)
    if missing:
        raise DomainError(
            "Required personnel fields are missing", code="required_fields", details=missing
        )
    if not isinstance(data["active"], bool):
        raise DomainError("active must be true or false", code="invalid_active")
    values: dict[str, object] = {"active": data["active"]}
    for name, maximum in {
        "display_name": 200,
        "source_version": 160,
        "external_user_id": 160,
        "job_title": 160,
        "department": 160,
    }.items():
        raw = data.get(name, "")
        if not isinstance(raw, str):
            raise DomainError(f"{name} must be text", code="invalid_personnel_field")
        value = raw.strip()
        if len(value) > maximum or (name in {"display_name", "source_version"} and not value):
            raise DomainError(f"{name} is invalid", code="invalid_personnel_field")
        values[name] = value
    observed = parse_datetime(str(data["source_updated_at"]))
    if observed is None or timezone.is_naive(observed):
        raise DomainError(
            "source_updated_at must be an ISO-8601 timestamp with a timezone",
            code="invalid_source_updated_at",
        )
    values["source_updated_at"] = observed
    return values


def _base_version(data: Any) -> int:
    raw = data.get("base_version")
    if isinstance(raw, bool):
        raise DomainError("base_version must be a positive integer", code="invalid_base_version")
    try:
        version = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise DomainError("base_version is required", code="base_version_required") from exc
    if version < 1:
        raise DomainError("base_version must be a positive integer", code="invalid_base_version")
    return version


def _assignment_targets(request: Request) -> list[dict[str, object]]:
    raw = request.data.get("assignees")
    if not isinstance(raw, list):
        raise DomainError("assignees must be a list", code="invalid_assignees")
    targets: list[dict[str, object]] = []
    allowed = {"user_id", "source_system", "external_employee_id", "role"}
    for item in raw:
        if not isinstance(item, dict):
            raise DomainError("Each assignee is invalid", code="invalid_assignee")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise DomainError(
                "Each assignee contains unsupported fields",
                code="invalid_assignee",
                details=unknown,
            )
        has_user = item.get("user_id") not in (None, "")
        has_external = item.get("source_system") not in (None, "") or item.get(
            "external_employee_id"
        ) not in (None, "")
        if has_user == has_external:
            raise DomainError(
                "Each assignee needs either user_id or external identity", code="invalid_assignee"
            )
        role = str(item.get("role", "technician")).strip().lower()
        if has_user:
            user = get_object_or_404(
                User,
                pk=item["user_id"],
                organization=request.user.organization,
                is_active=True,
            )
            targets.append({"local_user": user, "external_employee": None, "role": role})
            continue
        try:
            source, external_id = normalize_external_employee_identity(
                item.get("source_system"), item.get("external_employee_id")
            )
        except ValidationError as exc:
            raise DomainError(
                "External employee identity is invalid", code="invalid_assignee"
            ) from exc
        employee = ExternalEmployeeProjection.objects.filter(
            organization=request.user.organization,
            source_system=source,
            external_employee_id=external_id,
        ).first()
        if employee is None:
            raise DomainError("External employee was not found", code="not_found", status=404)
        if not employee.active:
            raise DomainError("An inactive person cannot be assigned", code="inactive_assignee")
        targets.append({"local_user": None, "external_employee": employee, "role": role})
    return targets


@api_view(["GET"])
def external_employees(request: Request) -> Response:
    _require_any(request, "maintenance.manage")
    rows = ExternalEmployeeProjection.objects.filter(
        organization=request.user.organization, active=True
    )
    query = str(request.query_params.get("q", "")).strip()
    if query:
        rows = rows.filter(
            Q(display_name__icontains=query)
            | Q(job_title__icontains=query)
            | Q(department__icontains=query)
        )
    return Response({"employees": [row.to_dict() for row in rows.order_by("display_name")[:200]]})


@api_view(["PUT"])
def external_employee_detail(
    request: Request, source_system: str, external_employee_id: str
) -> Response:
    _require_external_personnel_token(request)

    def upsert() -> Response:
        values = _external_employee_payload(request.data)
        source_updated_at = values["source_updated_at"]
        assert isinstance(source_updated_at, datetime)
        employee, created, changed = upsert_external_employee_projection(
            organization=request.user.organization,
            actor=request.user,
            source_system=source_system,
            external_employee_id=external_employee_id,
            display_name=str(values["display_name"]),
            active=bool(values["active"]),
            source_version=str(values["source_version"]),
            source_updated_at=source_updated_at,
            external_user_id=str(values["external_user_id"]),
            job_title=str(values["job_title"]),
            department=str(values["department"]),
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        return Response(
            {"employee": employee.to_dict(), "created": created, "changed": changed},
            status=201 if created else 200,
        )

    return idempotent(request, upsert)


def _asset(request: Request, asset_id: Any) -> Asset:
    try:
        asset = Asset.objects.filter(pk=asset_id, organization=request.user.organization).first()
    except (ValidationError, ValueError):
        asset = None
    if not asset:
        raise DomainError("Asset not found", code="not_found", status=404)
    return asset


def _driver_asset_allowed(request: Request, asset: Asset) -> None:
    if (
        "driver" in request.user.role_slugs
        and not can_access_all_maintenance_assets(request.user, request.auth)
        and asset.assigned_driver_id != request.user.pk
    ):
        raise DomainError(
            "Asset is not assigned to this driver", code="permission_denied", status=403
        )


def _can_execute(request: Request, work_order: WorkOrder) -> bool:
    return has_permission(request.user, "maintenance.manage", request.auth) or (
        has_permission(request.user, "maintenance.execute", request.auth)
        and is_active_work_order_assignee(work_order, request.user)
    )


def _labor_projection(
    work_order: WorkOrder, *, include_financial: bool = False
) -> dict[str, object]:
    entries = list(work_order.labor_entries.select_related("technician"))
    superseded_by = {
        entry.corrects_id: str(entry.pk) for entry in entries if entry.corrects_id is not None
    }
    current = [entry for entry in entries if entry.pk not in superseded_by]
    payload: dict[str, object] = {
        "labor_entries": [
            {
                **entry.to_dict(include_financial=include_financial),
                "is_current": entry.pk not in superseded_by,
                "superseded_by_id": superseded_by.get(entry.pk),
            }
            for entry in entries
        ],
        "current_labor_minutes": sum(entry.minutes for entry in current),
    }
    if include_financial:
        payload["current_labor_cost"] = str(sum((entry.cost for entry in current), Decimal("0")))
    return payload


@api_view(["GET", "POST"])
def service_packages(request: Request) -> Response:
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(
            request, "pm.manage", "maintenance.manage", "maintenance.execute", "work_orders.view"
        )
        rows = ServicePackage.objects.filter(organization=request.user.organization)
        if request.query_params.get("active") in {"1", "true"}:
            rows = rows.filter(active=True)
        return Response(
            {"service_packages": [row.to_dict(include_financial=include_financial) for row in rows]}
        )
    _require_any(request, "pm.manage")
    expected_parts = _body_list(request.data.get("expected_parts", []), "expected_parts")

    def create() -> Response:
        row = create_service_package(
            organization=request.user.organization,
            actor=request.user,
            name=str(request.data.get("name", "")),
            description=str(request.data.get("description", "")),
            tasks=_body_list(request.data.get("tasks", []), "tasks"),
            expected_parts=expected_parts,
            expected_labor_minutes=int(request.data.get("expected_labor_minutes", 0)),
        )
        return Response(
            {"service_package": row.to_dict(include_financial=include_financial)}, status=201
        )

    return idempotent(
        request,
        create,
        required=False,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def maintenance_plans(request: Request) -> Response:
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(
            request, "pm.manage", "maintenance.manage", "maintenance.execute", "assets.view"
        )
        rows = MaintenancePlan.objects.filter(
            organization=request.user.organization
        ).select_related("asset", "service_package")
        if request.query_params.get("asset_id"):
            rows = rows.filter(asset_id=request.query_params["asset_id"])
        return Response(
            {"plans": [row.to_dict(include_financial=include_financial) for row in rows]}
        )
    _require_any(request, "pm.manage")

    def create() -> Response:
        asset = _asset(request, request.data.get("asset_id"))
        package = get_object_or_404(
            ServicePackage,
            pk=request.data.get("service_package_id"),
            organization=request.user.organization,
        )
        plan = create_maintenance_plan(
            organization=request.user.organization,
            actor=request.user,
            asset=asset,
            package=package,
            name=str(request.data.get("name", "")),
            triggers=_body_list(request.data.get("triggers", []), "triggers"),
        )
        return Response({"plan": plan.to_dict(include_financial=include_financial)}, status=201)

    return idempotent(
        request,
        create,
        required=False,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["POST"])
def recalculate_plans(request: Request) -> Response:
    _require_any(request, "pm.manage", "maintenance.manage", "integrations.manage")
    rows = MaintenancePlan.objects.filter(
        organization=request.user.organization,
        active=True,
        asset__archived_at__isnull=True,
    ).exclude(asset__status=Asset.Status.RETIRED)
    rows = rows.select_related("asset", "service_package")
    if request.data.get("plan_id"):
        rows = rows.filter(pk=request.data["plan_id"])
    recalculated = _recalculate_plans(rows)
    return Response(
        {
            "plans": [
                plan.to_dict(include_financial=can_view_financials(request.user, request.auth))
                for plan in rows
            ],
            "recalculated": recalculated,
        }
    )


@api_view(["POST"])
def plan_to_work_order(request: Request, plan_id: object) -> Response:
    _require_any(request, "pm.manage")
    plan = get_object_or_404(
        MaintenancePlan.objects.select_related("asset", "service_package"),
        pk=plan_id,
        organization=request.user.organization,
    )

    def create() -> Response:
        assigned = None
        if request.data.get("assigned_to_id"):
            assigned = get_object_or_404(
                User,
                pk=request.data["assigned_to_id"],
                organization=request.user.organization,
                is_active=True,
            )
        work = create_work_order(
            organization=request.user.organization,
            actor=request.user,
            asset=plan.asset,
            plan=plan,
            package=plan.service_package,
            assigned_to=assigned,
            summary=str(request.data.get("summary") or f"Scheduled {plan.name}"),
            priority=str(request.data.get("priority", "normal")),
            requires_qc=bool(request.data.get("requires_qc", False)),
            target_date=parse_date(str(request.data["target_date"]))
            if request.data.get("target_date")
            else None,
        )
        return Response(
            {
                "work_order": work.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        create,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def inspection_templates(request: Request) -> Response:
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(request, "inspections.create", "maintenance.manage", "maintenance.execute")
        rows = InspectionTemplate.objects.filter(organization=request.user.organization)
        if request.query_params.get("active", "true") in {"1", "true"}:
            rows = rows.filter(active=True)
        return Response(
            {
                "inspection_templates": [
                    row.to_dict(include_financial=include_financial) for row in rows
                ]
            }
        )
    _require_any(request, "pm.manage", "maintenance.manage")
    name = str(request.data.get("name", "")).strip()
    questions = _body_list(request.data.get("questions", []), "questions")
    _reject_financial_payload(questions, "questions")
    if not name or not questions:
        raise DomainError("Template name and questions are required", code="invalid_template")
    ids = [str(question.get("id", "")) for question in questions]
    if any(not question_id for question_id in ids) or len(ids) != len(set(ids)):
        raise DomainError("Question IDs must be present and unique", code="invalid_questions")

    def create() -> Response:
        with transaction.atomic():
            previous = (
                InspectionTemplate.objects.select_for_update()
                .filter(organization=request.user.organization, name=name)
                .order_by("-version")
                .first()
            )
            row = InspectionTemplate.objects.create(
                organization=request.user.organization,
                created_by=request.user,
                name=name,
                description=str(request.data.get("description", "")).strip(),
                questions=questions,
                retention_months=int(request.data.get("retention_months", 14)),
                version=previous.version + 1 if previous else 1,
                supersedes=previous,
            )
            if previous:
                InspectionTemplate.objects.filter(pk=previous.pk).update(active=False)
            audit(
                organization=request.user.organization,
                actor=request.user,
                action="inspection_template.version_created",
                resource=row,
                context={
                    "version": row.version,
                    "supersedes_id": str(previous.pk) if previous else None,
                },
            )
        return Response(
            {"inspection_template": row.to_dict(include_financial=include_financial)}, status=201
        )

    return idempotent(
        request,
        create,
        required=False,
        replay_response_transform=_financial_replay_transform(request),
    )


def _inspection_queryset(request: Request) -> QuerySet[Inspection]:
    rows = (
        Inspection.objects.filter(organization=request.user.organization)
        .select_related("asset", "template", "performed_by")
        .prefetch_related(
            "responses__finding",
            "findings__response",
            "findings__reported_by",
            "findings__defect",
        )
    )
    if "driver" in request.user.role_slugs and not can_access_all_maintenance_assets(
        request.user, request.auth
    ):
        rows = rows.filter(performed_by=request.user)
    return rows


@api_view(["GET", "POST"])
def inspections(request: Request) -> Response:
    include_financial = can_view_financials(request.user, request.auth)
    if request.method == "GET":
        _require_any(
            request,
            "inspections.view_own",
            "inspections.create",
            "maintenance.manage",
            "maintenance.execute",
        )
        rows = _inspection_queryset(request)
        if request.query_params.get("asset_id"):
            rows = rows.filter(asset_id=request.query_params["asset_id"])
        return Response(
            {
                "inspections": [
                    row.to_dict(include_financial=include_financial) for row in rows[:200]
                ]
            }
        )
    _require_any(request, "inspections.create", "maintenance.manage")

    def create() -> Response:
        asset = _asset(request, request.data.get("asset_id"))
        _driver_asset_allowed(request, asset)
        template = get_object_or_404(
            InspectionTemplate,
            pk=request.data.get("template_id"),
            organization=request.user.organization,
            active=True,
        )
        replaces = None
        if request.data.get("replaces_id"):
            replaces = get_object_or_404(
                Inspection,
                pk=request.data["replaces_id"],
                organization=request.user.organization,
                status="Voided",
            )
        row = create_inspection(
            organization=request.user.organization,
            actor=request.user,
            asset=asset,
            template=template,
            responses=_body_list(request.data.get("responses", []), "responses"),
            acknowledgment=str(request.data.get("acknowledgment", "")),
            submit=bool(request.data.get("submit", True)),
            replaces=replaces,
        )
        return Response(
            {"inspection": row.to_dict(include_financial=include_financial)}, status=201
        )

    return idempotent(
        request,
        create,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET"])
def inspection_detail(request: Request, inspection_id: object) -> Response:
    _require_any(
        request,
        "inspections.view_own",
        "inspections.create",
        "maintenance.manage",
        "maintenance.execute",
    )
    row = get_object_or_404(_inspection_queryset(request), pk=inspection_id)
    return Response(
        {
            "inspection": row.to_dict(
                include_financial=can_view_financials(request.user, request.auth)
            )
        }
    )


@api_view(["POST"])
def void_inspection(request: Request, inspection_id: object) -> Response:
    _require_any(request, "maintenance.manage")
    row = get_object_or_404(Inspection, pk=inspection_id, organization=request.user.organization)

    def void() -> Response:
        return Response(
            {
                "inspection": void_inspection_record(
                    inspection=row, actor=request.user, reason=str(request.data.get("reason", ""))
                ).to_dict(include_financial=can_view_financials(request.user, request.auth))
            }
        )

    return idempotent(
        request,
        void,
        replay_response_transform=_financial_replay_transform(request),
    )


def _defect_queryset(request: Request) -> QuerySet[Defect]:
    rows = Defect.objects.filter(organization=request.user.organization).select_related(
        "asset", "reported_by"
    )
    if "driver" in request.user.role_slugs and not can_access_all_maintenance_assets(
        request.user, request.auth
    ):
        rows = rows.filter(reported_by=request.user)
    return rows


@api_view(["GET", "POST"])
def defects(request: Request) -> Response:
    if request.method == "GET":
        _require_any(
            request,
            "defects.view_own",
            "defects.create",
            "maintenance.manage",
            "maintenance.execute",
        )
        rows = _defect_queryset(request)
        if request.query_params.get("asset_id"):
            rows = rows.filter(asset_id=request.query_params["asset_id"])
        return Response({"defects": [row.to_dict() for row in rows[:200]]})
    _require_any(request, "defects.create")

    def create() -> Response:
        asset = _asset(request, request.data.get("asset_id"))
        _driver_asset_allowed(request, asset)
        row = create_defect(
            organization=request.user.organization,
            actor=request.user,
            asset=asset,
            category=str(request.data.get("category", "")),
            description=str(request.data.get("description", "")),
            severity=str(request.data.get("severity", "medium")),
            safety_related=bool(request.data.get("safety_related", False)),
        )
        return Response({"defect": row.to_dict()}, status=201)

    return idempotent(request, create)


@api_view(["POST"])
def defect_transition(request: Request, defect_id: object) -> Response:
    _require_any(request, "maintenance.manage", "maintenance.execute")
    row = get_object_or_404(Defect, pk=defect_id, organization=request.user.organization)
    target_status = str(request.data.get("status", ""))
    can_manage = has_permission(request.user, "maintenance.manage", request.auth)
    if not can_manage:
        if target_status != "Corrected":
            raise DomainError(
                "Technicians may only mark an assigned defect as corrected",
                code="permission_denied",
                status=403,
            )

    def transition() -> Response:
        if not can_manage:
            assigned_work_exists = (
                filter_work_orders_for_assignee(
                    WorkOrder.objects.select_for_update().filter(
                        organization=request.user.organization,
                        request__defect=row,
                    ),
                    request.user,
                )
                .exclude(status__in=["Closed", "Cancelled"])
                .exists()
            )
            if not assigned_work_exists:
                raise DomainError(
                    "This defect is not linked to work assigned to this technician",
                    code="permission_denied",
                    status=403,
                )
        reason = str(request.data.get("reason", ""))
        evidence_attachment_ids: list[str] = []
        if target_status == "Corrected":
            repair_details = str(request.data.get("repair_details") or reason).strip()
            if not repair_details:
                raise DomainError(
                    "Repair details are required before a defect can be marked corrected",
                    code="repair_details_required",
                )
            raw_evidence_ids = request.data.get("evidence_attachment_ids", [])
            if not isinstance(raw_evidence_ids, list):
                raise DomainError(
                    "evidence_attachment_ids must be a list",
                    code="invalid_repair_evidence",
                )
            try:
                import uuid as uuid_module

                evidence_attachment_ids = list(
                    dict.fromkeys(str(uuid_module.UUID(str(value))) for value in raw_evidence_ids)
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise DomainError(
                    "Repair evidence contains an invalid attachment ID",
                    code="invalid_repair_evidence",
                ) from exc
            if evidence_attachment_ids:
                from core.models import Attachment

                linked_ids = {
                    str(value)
                    for value in Attachment.objects.filter(
                        pk__in=evidence_attachment_ids,
                        organization=request.user.organization,
                        resource_type__iexact="Defect",
                        resource_id=str(row.pk),
                    ).values_list("pk", flat=True)
                }
                if linked_ids != set(evidence_attachment_ids):
                    raise DomainError(
                        "Repair evidence must be attached to this defect",
                        code="invalid_repair_evidence",
                    )
            reason = repair_details
        updated = transition_defect(
            defect=row,
            actor=request.user,
            new_status=target_status,
            reason=reason,
            auth=request.auth,
        )
        response: dict[str, object] = {"defect": updated.to_dict()}
        if target_status == "Corrected":
            repair_evidence = {
                "repair_details": reason,
                "attachment_ids": evidence_attachment_ids,
            }
            audit(
                organization=updated.organization,
                actor=request.user,
                action="defect.repair_documented",
                resource=updated,
                context=repair_evidence,
                correlation_id=str(
                    request.headers.get("Idempotency-Key") or request.data.get("operation_id", "")
                ),
            )
            response["repair_evidence"] = repair_evidence
        return Response(response)

    return idempotent(request, transition)


@api_view(["POST"])
def defect_to_request(request: Request, defect_id: object) -> Response:
    _require_any(request, "maintenance.manage")
    defect = get_object_or_404(
        Defect.objects.select_related("asset"), pk=defect_id, organization=request.user.organization
    )

    def create() -> Response:
        row = create_request(
            organization=request.user.organization,
            actor=request.user,
            asset=defect.asset,
            defect=defect,
            summary=str(request.data.get("summary") or defect.description[:240]),
            description=str(request.data.get("description", defect.description)),
            priority=str(
                request.data.get("priority") or ("safety" if defect.safety_related else "normal")
            ),
        )
        defect.refresh_from_db(fields=["status"])
        if defect.status == "Open":
            transition_defect(defect=defect, actor=request.user, new_status="Acknowledged")
        return Response({"maintenance_request": row.to_dict()}, status=201)

    return idempotent(request, create)


@api_view(["GET", "POST"])
def requests_collection(request: Request) -> Response:
    if request.method == "GET":
        _require_any(request, "maintenance.manage", "maintenance.execute", "work_orders.view")
        rows = MaintenanceRequest.objects.filter(
            organization=request.user.organization
        ).select_related("asset")
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        return Response({"maintenance_requests": [row.to_dict() for row in rows[:200]]})
    _require_any(request, "maintenance.manage", "maintenance.execute")

    def create() -> Response:
        asset = _asset(request, request.data.get("asset_id"))
        row = create_request(
            organization=request.user.organization,
            actor=request.user,
            asset=asset,
            summary=str(request.data.get("summary", "")),
            description=str(request.data.get("description", "")),
            priority=str(request.data.get("priority", "normal")),
        )
        return Response({"maintenance_request": row.to_dict()}, status=201)

    return idempotent(request, create)


@api_view(["POST"])
def request_transition(request: Request, request_id: object) -> Response:
    _require_any(request, "maintenance.manage")
    row = get_object_or_404(
        MaintenanceRequest, pk=request_id, organization=request.user.organization
    )

    def transition() -> Response:
        updated = transition_request(
            request=row,
            actor=request.user,
            new_status=str(request.data.get("status", "")),
            reason=str(request.data.get("reason", "")),
            auth=request.auth,
        )
        return Response({"maintenance_request": updated.to_dict()})

    return idempotent(request, transition)


@api_view(["POST"])
def request_to_work_order(request: Request, request_id: object) -> Response:
    _require_any(request, "maintenance.manage")
    row = get_object_or_404(
        MaintenanceRequest.objects.select_related("asset"),
        pk=request_id,
        organization=request.user.organization,
    )

    def create() -> Response:
        assigned = None
        if request.data.get("assigned_to_id"):
            assigned = get_object_or_404(
                User,
                pk=request.data["assigned_to_id"],
                organization=request.user.organization,
                is_active=True,
            )
        package = None
        if request.data.get("service_package_id"):
            package = get_object_or_404(
                ServicePackage,
                pk=request.data["service_package_id"],
                organization=request.user.organization,
            )
        work = create_work_order(
            organization=request.user.organization,
            actor=request.user,
            asset=row.asset,
            request=row,
            package=package,
            assigned_to=assigned,
            summary=str(request.data.get("summary") or row.summary),
            complaint=str(request.data.get("complaint") or row.description),
            priority=str(request.data.get("priority") or row.priority),
            requires_qc=bool(request.data.get("requires_qc", row.priority == "safety")),
            target_date=parse_date(str(request.data["target_date"]))
            if request.data.get("target_date")
            else None,
        )
        return Response({"work_order": work.to_dict()}, status=201)

    return idempotent(request, create)


def _work_queryset(request: Request) -> QuerySet[WorkOrder]:
    rows = (
        WorkOrder.objects.filter(organization=request.user.organization)
        .select_related("asset", "assigned_to", "maintenance_plan", "service_package")
        .prefetch_related(
            "tasks",
            Prefetch(
                "assignment_events",
                queryset=WorkOrderAssignment.objects.select_related(
                    "assigned_by", "external_employee", "local_user"
                ).order_by("sequence"),
            ),
        )
    )
    if not can_view_all_work_orders(request.user, request.auth) or request.query_params.get(
        "mine"
    ) in {"1", "true"}:
        rows = filter_work_orders_for_assignee(rows, request.user)
        rows = rows.order_by(
            Case(
                When(priority="safety", then=Value(0)),
                When(priority="high", then=Value(1)),
                When(priority="normal", then=Value(2)),
                When(priority="low", then=Value(3)),
                default=Value(4),
                output_field=IntegerField(),
            ),
            Case(
                When(target_date__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            "target_date",
            "created_at",
            "id",
        )
    return rows


@api_view(["GET", "POST"])
def work_orders(request: Request) -> Response:
    if request.method == "GET":
        _require_any(request, "work_orders.view", "maintenance.execute", "maintenance.manage")
        rows = _work_queryset(request)
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        if request.query_params.get("asset_id"):
            rows = rows.filter(asset_id=request.query_params["asset_id"])
        return Response(
            {
                "work_orders": [
                    row.to_dict(include_financial=can_view_financials(request.user, request.auth))
                    for row in rows[:200]
                ]
            }
        )
    _require_any(request, "maintenance.manage")

    def create() -> Response:
        asset = _asset(request, request.data.get("asset_id"))
        assigned = None
        if request.data.get("assigned_to_id"):
            assigned = get_object_or_404(
                User,
                pk=request.data["assigned_to_id"],
                organization=request.user.organization,
                is_active=True,
            )
        package = None
        if request.data.get("service_package_id"):
            package = get_object_or_404(
                ServicePackage,
                pk=request.data["service_package_id"],
                organization=request.user.organization,
            )
        work = create_work_order(
            organization=request.user.organization,
            actor=request.user,
            asset=asset,
            package=package,
            assigned_to=assigned,
            summary=str(request.data.get("summary", "")),
            complaint=str(request.data.get("complaint", "")),
            priority=str(request.data.get("priority", "normal")),
            requires_qc=bool(request.data.get("requires_qc", False)),
            target_date=parse_date(str(request.data["target_date"]))
            if request.data.get("target_date")
            else None,
        )
        return Response(
            {
                "work_order": work.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        create,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "PATCH"])
def work_order_detail(request: Request, work_order_id: object) -> Response:
    _require_any(request, "work_orders.view", "maintenance.execute", "maintenance.manage")
    row = get_object_or_404(_work_queryset(request), pk=work_order_id)
    if request.method == "GET":
        from inventory.models import StockTransaction

        include_financial = can_view_financials(request.user, request.auth)
        payload = row.to_dict(include_close_snapshots=True, include_financial=include_financial)
        payload.update(_labor_projection(row, include_financial=include_financial))
        stock_transactions = list(
            StockTransaction.objects.filter(
                organization=request.user.organization, work_order=row
            ).select_related("part", "bin", "bin__warehouse", "actor")
        )
        payload["stock_transactions"] = [
            entry.to_dict(include_financial=include_financial) for entry in stock_transactions
        ]
        if include_financial:
            payload["part_cost"] = str(
                sum((entry.total_cost for entry in stock_transactions), Decimal("0"))
            )
        return Response({"work_order": payload})
    if not _can_execute(request, row):
        raise DomainError(
            "This work order is not assigned to this user", code="permission_denied", status=403
        )
    base_version = _base_version(request.data)
    manager_fields = {
        "summary",
        "complaint",
        "priority",
        "requires_qc",
        "target_date",
        "assigned_to_id",
    }
    technician_fields = {"diagnosis", "completion_summary"}
    allowed = technician_fields | (
        manager_fields
        if has_permission(request.user, "maintenance.manage", request.auth)
        else set()
    )
    changed: dict[str, object] = {}
    for field in allowed:
        if field not in request.data:
            continue
        value: object = request.data[field]
        if field == "target_date":
            value = parse_date(str(value)) if value else None
        elif field == "assigned_to_id":
            value = (
                get_object_or_404(
                    User, pk=value, organization=request.user.organization, is_active=True
                )
                if value
                else None
            )
            field = "assigned_to"
        elif field == "priority" and value not in dict(WorkOrder.PRIORITIES):
            raise DomainError("Invalid priority", code="invalid_priority")
        changed[field] = value

    def update() -> Response:
        with transaction.atomic():
            locked = (
                WorkOrder.objects.select_for_update(of=("self",))
                .select_related("asset", "assigned_to", "maintenance_plan", "service_package")
                .prefetch_related("tasks")
                .get(pk=row.pk, organization=request.user.organization)
            )
            if not _can_execute(request, locked):
                raise DomainError(
                    "This work order is not assigned to this user",
                    code="permission_denied",
                    status=403,
                )
            if locked.status in {"Completed", "Closed", "Cancelled"}:
                raise DomainError(
                    "This work order is not editable", code="work_order_not_editable", status=409
                )
            if locked.version != base_version:
                raise DomainError(
                    "This work order changed on the server",
                    code="sync_conflict",
                    status=409,
                    details={
                        "base_version": base_version,
                        "current_version": locked.version,
                        "work_order": locked.to_dict(),
                    },
                )
            if not changed:
                return Response({"work_order": locked.to_dict()})
            previous = {field: str(getattr(locked, field)) for field in changed}
            for field, value in changed.items():
                setattr(locked, field, value)
            locked.version += 1
            locked.save(update_fields=[*changed, "version", "updated_at"])
            assignment_event_ids: list[str] = []
            if "assigned_to" in changed:
                removed, added = sync_legacy_work_order_lead(
                    work_order=locked,
                    actor=request.user,
                    assigned_to=locked.assigned_to,
                    reason="Legacy primary assignment update",
                )
                assignment_event_ids = [str(event.pk) for event in [*removed, *added]]
            audit(
                organization=locked.organization,
                actor=request.user,
                action="work_order.updated",
                resource=locked,
                context={
                    "previous": previous,
                    "fields": sorted(changed),
                    "assignment_event_ids": assignment_event_ids,
                },
            )
        return Response({"work_order": locked.to_dict()})

    return idempotent(request, update)


@api_view(["POST"])
def work_order_assignments(request: Request, work_order_id: object) -> Response:
    _require_any(request, "maintenance.manage")
    row = get_object_or_404(
        WorkOrder.objects.filter(organization=request.user.organization), pk=work_order_id
    )

    def assign() -> Response:
        base_version = _base_version(request.data)
        targets = _assignment_targets(request)
        reason_value = request.data.get("reason", "")
        if not isinstance(reason_value, str):
            raise DomainError("Assignment reason must be text", code="invalid_reason")
        reason = reason_value.strip()
        updated = replace_work_order_assignments(
            work_order=row,
            actor=request.user,
            targets=targets,
            base_version=base_version,
            reason=reason,
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        return Response({"work_order": updated.to_dict()})

    return idempotent(request, assign)


@api_view(["POST"])
def work_order_transition(request: Request, work_order_id: object) -> Response:
    _require_any(request, "maintenance.execute", "maintenance.manage")
    row = get_object_or_404(_work_queryset(request), pk=work_order_id)
    if not _can_execute(request, row):
        raise DomainError(
            "This work order is not assigned to this user", code="permission_denied", status=403
        )
    target = str(request.data.get("status", ""))
    if not has_permission(request.user, "maintenance.manage", request.auth) and target in {
        "Ready",
        "Closed",
        "Reopened",
        "Cancelled",
    }:
        raise DomainError(
            "A supervisor must perform this transition", code="permission_denied", status=403
        )

    def transition() -> Response:
        meter_reading = None
        if request.data.get("completion_meter_id"):
            meter_reading = get_object_or_404(
                MeterReading,
                pk=request.data["completion_meter_id"],
                organization=request.user.organization,
            )
        updated = transition_work_order(
            work_order=row,
            actor=request.user,
            new_status=target,
            reason=str(request.data.get("reason", "")),
            completion_summary=str(request.data.get("completion_summary", "")),
            completion_meter=meter_reading,
        )
        return Response({"work_order": updated.to_dict()})

    return idempotent(request, transition)


@api_view(["GET", "POST"])
def work_order_tasks(request: Request, work_order_id: object) -> Response:
    _require_any(request, "maintenance.execute", "maintenance.manage", "work_orders.view")
    work = get_object_or_404(_work_queryset(request), pk=work_order_id)
    if request.method == "GET":
        return Response(
            {
                "tasks": [
                    task.to_dict(include_financial=can_view_financials(request.user, request.auth))
                    for task in work.tasks.all()
                ]
            }
        )
    if not has_permission(request.user, "maintenance.manage", request.auth):
        raise DomainError("Only a supervisor may add tasks", code="permission_denied", status=403)
    title = str(request.data.get("title", "")).strip()
    if not title:
        raise DomainError("Task title is required", code="title_required")
    raw_sequence = request.data.get("sequence")
    if isinstance(raw_sequence, bool):
        raise DomainError("sequence must be a non-negative integer", code="invalid_sequence")
    try:
        sequence = int(str(raw_sequence)) if raw_sequence not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "sequence must be a non-negative integer", code="invalid_sequence"
        ) from exc
    if sequence is not None and not 0 <= sequence <= 32767:
        raise DomainError("sequence must be between 0 and 32767", code="invalid_sequence")

    def create() -> Response:
        with transaction.atomic():
            locked = WorkOrder.objects.select_for_update().get(
                pk=work.pk, organization=request.user.organization
            )
            if locked.status in {"Completed", "Closed", "Cancelled"}:
                raise DomainError(
                    "This work order is not editable", code="work_order_not_editable", status=409
                )
            task_sequence = sequence
            if task_sequence is None:
                last_sequence = (
                    locked.tasks.order_by("-sequence").values_list("sequence", flat=True).first()
                    or 0
                )
                if last_sequence >= 32767:
                    raise DomainError(
                        "No task sequence remains available",
                        code="task_sequence_exhausted",
                        status=409,
                    )
                task_sequence = last_sequence + 1
            elif locked.tasks.filter(sequence=task_sequence).exists():
                raise DomainError(
                    "A task already uses this sequence",
                    code="duplicate_task_sequence",
                    status=409,
                )
            component = None
            if request.data.get("component_id"):
                component = get_object_or_404(
                    Component.objects.filter(organization=request.user.organization),
                    pk=request.data["component_id"],
                )
                component = _component_on_asset(component, locked.asset)
            task = WorkOrderTask.objects.create(
                organization=request.user.organization,
                work_order=locked,
                title=title,
                instructions=str(request.data.get("instructions", "")),
                required=bool(request.data.get("required", True)),
                sequence=task_sequence,
                component=component,
            )
            audit(
                organization=locked.organization,
                actor=request.user,
                action="work_order_task.created",
                resource=task,
                context={"work_order_id": str(locked.pk)},
            )
        return Response(
            {
                "task": task.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(request, create)


@api_view(["GET", "PATCH", "POST"])
def work_order_task(request: Request, work_order_id: object, task_id: object) -> Response:
    _require_any(request, "maintenance.execute", "maintenance.manage", "work_orders.view")
    work = get_object_or_404(_work_queryset(request), pk=work_order_id)
    task = get_object_or_404(
        WorkOrderTask.objects.select_related("work_order"),
        pk=task_id,
        work_order=work,
        organization=request.user.organization,
    )
    if request.method == "GET":
        return Response(
            {
                "task": task.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            }
        )
    if not _can_execute(request, task.work_order):
        raise DomainError(
            "This work order is not assigned to this user", code="permission_denied", status=403
        )

    def update() -> Response:
        component: object = UNSET
        if "component_id" in request.data:
            raw = request.data["component_id"]
            component = (
                get_object_or_404(
                    Component.objects.filter(organization=request.user.organization), pk=raw
                )
                if raw
                else None
            )
        updated = update_work_order_task(
            task=task,
            actor=request.user,
            status=str(request.data.get("status", task.status)),
            notes=str(request.data.get("notes", task.notes)),
            measurement=request.data.get("measurement", task.measurement),
            base_version=request.data.get("base_version"),
            component=component,
        )
        return Response(
            {
                "task": updated.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                ),
                "work_order_version": updated.work_order.version,
            }
        )

    return idempotent(
        request,
        update,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET", "POST"])
def labor_entries(request: Request, work_order_id: object) -> Response:
    _require_any(request, "maintenance.execute", "maintenance.manage", "work_orders.view")
    work = get_object_or_404(_work_queryset(request), pk=work_order_id)
    if request.method == "GET":
        return Response(
            _labor_projection(
                work,
                include_financial=can_view_financials(request.user, request.auth),
            )
        )
    if not _can_execute(request, work):
        raise DomainError(
            "This work order is not assigned to this user", code="permission_denied", status=403
        )
    if "hourly_rate" in request.data and not can_manage_financials(request.user, request.auth):
        raise DomainError(
            "Permission required to set labor cost: financial.manage",
            code="financial_permission_denied",
            status=403,
        )
    if request.data.get("corrects_id") and not can_manage_financials(request.user, request.auth):
        raise DomainError(
            "Permission required to correct labor cost: financial.manage",
            code="financial_permission_denied",
            status=403,
        )

    def create() -> Response:
        corrects = None
        if request.data.get("corrects_id"):
            if not has_permission(request.user, "maintenance.manage", request.auth):
                raise DomainError(
                    "Only a supervisor may correct labor",
                    code="permission_denied",
                    status=403,
                )
            corrects = get_object_or_404(
                LaborEntry.objects.select_related("technician"),
                pk=request.data["corrects_id"],
                organization=request.user.organization,
            )
        technician = corrects.technician if corrects else request.user
        if request.data.get("technician_id") and has_permission(
            request.user, "maintenance.manage", request.auth
        ):
            technician = get_object_or_404(
                User,
                pk=request.data["technician_id"],
                organization=request.user.organization,
                is_active=True,
            )
        entry = create_labor_entry(
            work_order=work,
            actor=request.user,
            technician=technician,
            minutes=int(request.data.get("minutes", 0)),
            hourly_rate=Decimal(str(request.data.get("hourly_rate", 0))),
            note=str(request.data.get("note", "")),
            started_at=_parse_datetime(request.data.get("started_at")),
            ended_at=_parse_datetime(request.data.get("ended_at")),
            corrects=corrects,
        )
        return Response(
            {
                "labor_entry": entry.to_dict(
                    include_financial=can_view_financials(request.user, request.auth)
                )
            },
            status=201,
        )

    return idempotent(
        request,
        create,
        replay_response_transform=_financial_replay_transform(request),
    )


@api_view(["GET"])
def maintenance_alerts(request: Request) -> Response:
    _require_any(request, "maintenance.manage", "integrations.manage", "reports.integration")
    rows = MaintenanceAlert.objects.filter(organization=request.user.organization).select_related(
        "asset"
    )
    if request.query_params.get("status"):
        rows = rows.filter(status=request.query_params["status"])
    return Response({"maintenance_alerts": [row.to_dict() for row in rows[:200]]})


@api_view(["POST"])
def maintenance_alert_transition(request: Request, alert_id: Any) -> Response:
    _require_any(request, "maintenance.manage")
    target = str(request.data.get("status", ""))
    transitions = {
        "New": {"NeedsReview", "Acknowledged", "Dismissed"},
        "NeedsReview": {"Acknowledged", "Suppressed", "Dismissed"},
        "Acknowledged": {"Converted", "Suppressed", "Resolved", "Dismissed"},
        "Suppressed": {"NeedsReview", "Resolved"},
        "Converted": {"Resolved"},
        "Resolved": set(),
        "Dismissed": {"NeedsReview"},
    }

    def transition() -> Response:
        with transaction.atomic():
            row = (
                MaintenanceAlert.objects.select_for_update()
                .select_related("asset")
                .get(pk=alert_id, organization=request.user.organization)
            )
            previous = row.status
            if target not in transitions.get(previous, set()):
                raise DomainError(
                    f"Alert cannot transition from {previous} to {target}",
                    code="invalid_alert_transition",
                    status=409,
                )
            reason = str(request.data.get("reason", "")).strip()
            if target in {"Suppressed", "Dismissed"} and not reason:
                raise DomainError("A reason is required", code="reason_required")
            if target == "Converted":
                maintenance_request = create_request(
                    organization=row.organization,
                    actor=request.user,
                    asset=row.asset,
                    alert=row,
                    summary=str(request.data.get("summary") or row.title),
                    description=str(request.data.get("description") or row.description),
                    priority=str(request.data.get("priority", "normal")),
                )
            else:
                maintenance_request = None
            row.status, row.disposition_reason = target, reason
            row.save(update_fields=["status", "disposition_reason", "updated_at"])
            audit(
                organization=row.organization,
                actor=request.user,
                action="maintenance_alert.transitioned",
                resource=row,
                previous_state=previous,
                new_state=target,
                context={"reason": reason},
            )
        return Response(
            {
                "maintenance_alert": row.to_dict(),
                "maintenance_request": maintenance_request.to_dict()
                if maintenance_request
                else None,
            }
        )

    return idempotent(request, transition)
