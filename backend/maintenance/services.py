from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict, cast

from assets.models import Asset, Meter, MeterReading
from core.exceptions import DomainError
from core.models import Attachment, Comment, IdempotencyRecord, Organization, User
from core.permissions import can_view_financials, has_permission, redact_financial_fields
from core.services import audit, emit
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from .models import (
    Defect,
    ExternalEmployeeProjection,
    Inspection,
    InspectionFinding,
    InspectionResponse,
    InspectionTemplate,
    LaborEntry,
    MaintenanceAlert,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderAssignment,
    WorkOrderCloseSnapshot,
    WorkOrderTask,
    normalize_external_employee_identity,
)


class AssignmentTarget(TypedDict):
    local_user: User | None
    external_employee: ExternalEmployeeProjection | None
    role: str


def _validation_error(exc: ValidationError) -> DomainError:
    return DomainError(
        "Input validation failed",
        code="validation_error",
        details=getattr(exc, "message_dict", None) or getattr(exc, "messages", None),
    )


def _decimal(value: object, label: str = "value") -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainError(f"{label} must be a number", code="invalid_number") from exc
    if not parsed.is_finite():
        raise DomainError(f"{label} must be finite", code="invalid_number")
    return parsed


def _same_org(organization: Organization, *objects: Any) -> None:
    for obj in objects:
        if obj is not None and getattr(obj, "organization_id", None) != organization.pk:
            raise DomainError(
                "Referenced record is outside this organization", code="invalid_reference"
            )


def _reject_financial_payload(value: object, field: str) -> None:
    """Keep free-form maintenance JSON from becoming an ungoverned cost store."""

    if redact_financial_fields(value) != value:
        raise DomainError(
            f"{field} cannot include monetary fields",
            code="financial_payload_not_allowed",
        )


def _lock_serviceable_asset(organization: Organization, asset: Asset) -> Asset:
    locked = Asset.objects.select_for_update().get(pk=asset.pk, organization=organization)
    if locked.status == Asset.Status.RETIRED or locked.archived_at is not None:
        raise DomainError(
            "Retired or archived assets cannot receive new maintenance work",
            code="asset_retired",
            status=409,
        )
    return locked


_EXPECTED_PART_FIELDS = frozenset({"part_id", "part_number", "quantity", "required"})


def _normalize_expected_parts(expected_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep PM package parts operational; pricing belongs to inventory and purchasing facts."""

    normalized: list[dict[str, Any]] = []
    for item in expected_parts:
        if not isinstance(item, dict):
            raise DomainError("Every expected part must be an object", code="invalid_expected_part")
        unknown_fields = set(item) - _EXPECTED_PART_FIELDS
        if unknown_fields:
            raise DomainError(
                "Expected parts may contain only part_id, part_number, quantity, and required",
                code="invalid_expected_part",
                details={"fields": sorted(unknown_fields)},
            )
        part_id = str(item.get("part_id", "")).strip()
        part_number = str(item.get("part_number", "")).strip().upper()
        if not part_id and not part_number:
            raise DomainError(
                "Every expected part needs part_id or part_number", code="invalid_expected_part"
            )
        quantity = _decimal(item.get("quantity", "1"), "expected part quantity")
        if quantity <= 0:
            raise DomainError(
                "Expected part quantity must be positive", code="invalid_expected_part"
            )
        normalized_item: dict[str, Any] = {
            "quantity": format(quantity, "f"),
            "required": bool(item.get("required", True)),
        }
        if part_id:
            normalized_item["part_id"] = part_id
        if part_number:
            normalized_item["part_number"] = part_number
        normalized.append(normalized_item)
    return normalized


def _snapshot_package(package: ServicePackage | None) -> dict[str, Any]:
    if not package:
        return {}
    return {
        "id": str(package.pk),
        "name": package.name,
        "version": package.version,
        "description": package.description,
        "tasks": package.tasks,
        "expected_parts": package.expected_parts,
        "expected_labor_minutes": package.expected_labor_minutes,
    }


def can_access_all_maintenance_assets(user: User, auth: object | None = None) -> bool:
    return any(
        has_permission(user, permission, auth)
        for permission in ("assets.view", "maintenance.manage")
    )


def can_view_all_work_orders(user: User, auth: object | None = None) -> bool:
    if has_permission(user, "maintenance.manage", auth):
        return True
    return has_permission(user, "work_orders.view", auth) and bool(
        user.role_slugs & {"parts_clerk", "purchasing_manager"}
    )


def is_active_work_order_assignee(work_order: WorkOrder, user: User) -> bool:
    """Whether a local Fleetline user is on the currently assigned work-order team."""

    latest = (
        WorkOrderAssignment.objects.filter(work_order=work_order, local_user=user)
        .order_by("-sequence")
        .values_list("action", flat=True)
        .first()
    )
    if latest is not None:
        return latest == WorkOrderAssignment.Action.ASSIGNED
    # Compatibility for pre-migration or independently restored records.
    return work_order.assigned_to_id == user.pk


def filter_work_orders_for_assignee(rows: QuerySet[WorkOrder], user: User) -> QuerySet[WorkOrder]:
    """Return rows where ``user`` is active in the append-only team projection.

    The legacy lead field remains a fallback for a restore predating assignment
    events; current records take their membership from the most recent event.
    """

    latest_action = (
        WorkOrderAssignment.objects.filter(work_order_id=OuterRef("pk"), local_user=user)
        .order_by("-sequence")
        .values("action")[:1]
    )
    return rows.annotate(_assignment_action=Subquery(latest_action)).filter(
        Q(_assignment_action=WorkOrderAssignment.Action.ASSIGNED)
        | Q(_assignment_action__isnull=True, assigned_to=user)
    )


def upsert_external_employee_projection(
    *,
    organization: Organization,
    actor: User,
    source_system: str,
    external_employee_id: str,
    display_name: str,
    active: bool,
    source_version: str,
    source_updated_at: datetime,
    external_user_id: str = "",
    job_title: str = "",
    department: str = "",
    correlation_id: str = "",
) -> tuple[ExternalEmployeeProjection, bool, bool]:
    """Idempotently project source-owned personnel without creating a local account."""

    source, external_id = normalize_external_employee_identity(source_system, external_employee_id)
    if timezone.is_naive(source_updated_at):
        raise DomainError(
            "source_updated_at must include a timezone", code="invalid_source_updated_at"
        )
    values = {
        "external_user_id": external_user_id.strip(),
        "display_name": display_name.strip(),
        "job_title": job_title.strip(),
        "department": department.strip(),
        "active": active,
        "source_version": source_version.strip(),
        "source_updated_at": source_updated_at,
    }
    if not values["display_name"] or not values["source_version"]:
        raise DomainError("display_name and source_version are required", code="required_fields")
    with transaction.atomic():
        row = (
            ExternalEmployeeProjection.objects.select_for_update()
            .filter(
                organization=organization,
                source_system=source,
                external_employee_id=external_id,
            )
            .first()
        )
        if row is None:
            row = ExternalEmployeeProjection.objects.create(
                organization=organization,
                source_system=source,
                external_employee_id=external_id,
                **values,
            )
            audit(
                organization=organization,
                actor=actor,
                action="personnel.external_upserted",
                resource=row,
                new_state="active" if row.active else "inactive",
                context={
                    "source_system": source,
                    "external_employee_id": external_id,
                    "source_version": row.source_version,
                    "fields": sorted(values),
                    "created": True,
                },
                correlation_id=correlation_id,
                source=source,
            )
            return row, True, True
        if source_updated_at < row.source_updated_at:
            raise DomainError(
                "This personnel update is older than the stored source record",
                code="stale_external_employee",
                status=409,
            )
        changed = {name: value for name, value in values.items() if getattr(row, name) != value}
        if source_updated_at == row.source_updated_at and changed:
            raise DomainError(
                "This source timestamp conflicts with the stored personnel record",
                code="external_employee_version_conflict",
                status=409,
            )
        if not changed:
            return row, False, False
        previous_active = row.active
        for name, value in changed.items():
            setattr(row, name, value)
        row.save(update_fields=[*changed, "updated_at"])
        audit(
            organization=organization,
            actor=actor,
            action="personnel.external_upserted",
            resource=row,
            previous_state="active" if previous_active else "inactive",
            new_state="active" if row.active else "inactive",
            context={
                "source_system": source,
                "external_employee_id": external_id,
                "source_version": row.source_version,
                "fields": sorted(changed),
                "created": False,
            },
            correlation_id=correlation_id,
            source=source,
        )
        return row, False, True


def _assignment_target_key(target: Mapping[str, object]) -> tuple[str, str]:
    local_user = target.get("local_user")
    if isinstance(local_user, User):
        return ("user", str(local_user.pk))
    external_employee = target.get("external_employee")
    if isinstance(external_employee, ExternalEmployeeProjection):
        return ("external_employee", str(external_employee.pk))
    raise DomainError("Each assignment needs a valid person", code="invalid_assignee")


def _local_assignment_display_name(user: User) -> str:
    return (user.get_full_name().strip() or user.username)[:200]


def _assignment_identity_snapshot_from_target(target: AssignmentTarget) -> dict[str, str]:
    """Capture only the identity needed to retain an immutable assignment fact.

    An external projection is a current, source-owned personnel view. Keep its
    display identity, opaque source identifier, and source version on the event
    rather than copying login IDs, titles, departments, or other profile data.
    """

    local_user = target["local_user"]
    if local_user is not None:
        return {
            "subject_display_name": _local_assignment_display_name(local_user),
            "subject_source_system": "",
            "subject_external_employee_id": "",
            "subject_source_version": "",
        }
    external_employee = target["external_employee"]
    if external_employee is None:
        raise DomainError("Each assignment needs a valid person", code="invalid_assignee")
    return {
        "subject_display_name": external_employee.display_name,
        "subject_source_system": external_employee.source_system,
        "subject_external_employee_id": external_employee.external_employee_id,
        "subject_source_version": external_employee.source_version,
    }


def _assignment_identity_snapshot_from_event(event: WorkOrderAssignment) -> dict[str, str]:
    """Carry the original assignment identity into its compensating removal event."""

    return {
        "subject_display_name": event.subject_display_name,
        "subject_source_system": event.subject_source_system,
        "subject_external_employee_id": event.subject_external_employee_id,
        "subject_source_version": event.subject_source_version,
    }


def _assignment_audit_snapshot(
    events: list[WorkOrderAssignment],
) -> list[dict[str, str | None]]:
    """Return JSON-safe assignment identity evidence for the audit stream.

    Assignment event snapshots, rather than mutable personnel projections, are
    used so the audit stream cannot be rewritten by a later GatorHub rename.
    """

    snapshot: list[dict[str, str | None]] = []
    for event in events:
        snapshot.append(
            {
                "assignment_event_id": str(event.pk),
                "role": event.role,
                "subject_type": "user" if event.local_user_id else "external_employee",
                "user_id": str(event.local_user_id) if event.local_user_id else None,
                "display_name": event.subject_display_name,
                "source_system": (
                    event.subject_source_system if event.external_employee_id else None
                ),
                "external_employee_id": (
                    event.subject_external_employee_id if event.external_employee_id else None
                ),
                "source_version": (
                    event.subject_source_version if event.external_employee_id else None
                ),
            }
        )
    return snapshot


def _validate_assignment_targets(
    organization: Organization, targets: Sequence[Mapping[str, object]]
) -> list[AssignmentTarget]:
    if not targets:
        return []
    normalized: dict[tuple[str, str], AssignmentTarget] = {}
    lead_count = 0
    for target in targets:
        role = str(target.get("role", WorkOrderAssignment.Role.TECHNICIAN)).strip().lower()
        if role not in WorkOrderAssignment.Role.values:
            raise DomainError("Invalid assignment role", code="invalid_assignment_role")
        local_user = target.get("local_user")
        external_employee = target.get("external_employee")
        if bool(local_user) == bool(external_employee):
            raise DomainError(
                "Each assignment must identify exactly one person", code="invalid_assignee"
            )
        person = local_user or external_employee
        if not isinstance(person, (User, ExternalEmployeeProjection)):
            raise DomainError("Each assignment needs a valid person", code="invalid_assignee")
        if person.organization_id != organization.pk:
            raise DomainError(
                "Referenced person is outside this organization", code="invalid_reference"
            )
        active = (
            person.active if isinstance(person, ExternalEmployeeProjection) else person.is_active
        )
        if not active:
            raise DomainError("An inactive person cannot be assigned", code="inactive_assignee")
        if role == WorkOrderAssignment.Role.LEAD:
            lead_count += 1
        normalized_target: AssignmentTarget = {
            "local_user": local_user if isinstance(local_user, User) else None,
            "external_employee": (
                external_employee
                if isinstance(external_employee, ExternalEmployeeProjection)
                else None
            ),
            "role": role,
        }
        key = _assignment_target_key(normalized_target)
        if key in normalized:
            raise DomainError("A person may only be assigned once", code="duplicate_assignee")
        normalized[key] = normalized_target
    if lead_count > 1:
        raise DomainError("A work order may have only one lead", code="multiple_assignment_leads")
    return list(normalized.values())


def _append_assignment_changes(
    *,
    work_order: WorkOrder,
    actor: User | None,
    targets: list[AssignmentTarget],
    reason: str,
) -> tuple[list[WorkOrderAssignment], list[WorkOrderAssignment]]:
    """Append deltas only; callers already hold the work-order row lock."""

    desired = {_assignment_target_key(target): target for target in targets}
    current = {event.subject_key: event for event in work_order.active_assignments()}
    sequence = (
        work_order.assignment_events.order_by("-sequence")
        .values_list("sequence", flat=True)
        .first()
        or 0
    )
    removed = [
        event
        for key, event in current.items()
        if key not in desired or event.role != desired[key]["role"]
    ]
    added = [
        target
        for key, target in desired.items()
        if key not in current or current[key].role != target["role"]
    ]
    unassignments: list[WorkOrderAssignment] = []
    assignments: list[WorkOrderAssignment] = []
    for event in removed:
        sequence += 1
        unassignments.append(
            WorkOrderAssignment.objects.create(
                organization=work_order.organization,
                work_order=work_order,
                local_user=event.local_user,
                external_employee=event.external_employee,
                role=event.role,
                action=WorkOrderAssignment.Action.UNASSIGNED,
                sequence=sequence,
                assigned_by=actor,
                reason=reason,
                **_assignment_identity_snapshot_from_event(event),
            )
        )
    for target in added:
        sequence += 1
        assignments.append(
            WorkOrderAssignment.objects.create(
                organization=work_order.organization,
                work_order=work_order,
                local_user=target["local_user"],
                external_employee=target["external_employee"],
                role=str(target["role"]),
                action=WorkOrderAssignment.Action.ASSIGNED,
                sequence=sequence,
                assigned_by=actor,
                reason=reason,
                **_assignment_identity_snapshot_from_target(target),
            )
        )
    return unassignments, assignments


def replace_work_order_assignments(
    *,
    work_order: WorkOrder,
    actor: User,
    targets: list[dict[str, object]],
    base_version: int,
    reason: str = "",
    correlation_id: str = "",
) -> WorkOrder:
    """Atomically replace the active team while retaining immutable assignment evidence."""

    if len(reason) > 500:
        raise DomainError("Assignment reason is too long", code="invalid_reason")
    normalized = _validate_assignment_targets(work_order.organization, targets)
    with transaction.atomic():
        locked = WorkOrder.objects.select_for_update().get(
            pk=work_order.pk, organization=work_order.organization
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
                details={"base_version": base_version, "current_version": locked.version},
            )
        before = _assignment_audit_snapshot(locked.active_assignments())
        removed, added = _append_assignment_changes(
            work_order=locked,
            actor=actor,
            targets=normalized,
            reason=reason,
        )
        if not removed and not added:
            return locked
        lead = next(
            (
                target["local_user"]
                for target in normalized
                if target["role"] == WorkOrderAssignment.Role.LEAD and target["local_user"]
            ),
            None,
        )
        locked.assigned_to = lead if isinstance(lead, User) else None
        locked.version += 1
        locked.save(update_fields=["assigned_to", "version", "updated_at"])
        after = _assignment_audit_snapshot(locked.active_assignments())
        audit(
            organization=locked.organization,
            actor=actor,
            action="work_order.assignments_changed",
            resource=locked,
            context={
                "before": before,
                "after": after,
                "reason": reason,
                "assignment_event_ids": [str(event.pk) for event in [*removed, *added]],
            },
            correlation_id=correlation_id,
        )
        return locked


def sync_legacy_work_order_lead(
    *,
    work_order: WorkOrder,
    actor: User | None,
    assigned_to: User | None,
    reason: str,
) -> tuple[list[WorkOrderAssignment], list[WorkOrderAssignment]]:
    """Append legacy single-assignee changes without removing teammate assignments."""

    current = work_order.active_assignments()
    targets: list[AssignmentTarget] = [
        {
            "local_user": event.local_user,
            "external_employee": event.external_employee,
            "role": event.role,
        }
        for event in current
        if event.role != WorkOrderAssignment.Role.LEAD
        and not (assigned_to is not None and event.local_user_id == assigned_to.pk)
    ]
    if assigned_to is not None:
        targets.append({"local_user": assigned_to, "external_employee": None, "role": "lead"})
    return _append_assignment_changes(
        work_order=work_order,
        actor=actor,
        targets=_validate_assignment_targets(work_order.organization, targets),
        reason=reason,
    )


def create_service_package(
    *,
    organization: Organization,
    actor: User,
    name: str,
    tasks: list[dict[str, Any]],
    description: str = "",
    expected_parts: list[dict[str, Any]] | None = None,
    expected_labor_minutes: int = 0,
) -> ServicePackage:
    name = name.strip()
    if not name:
        raise DomainError("Package name is required", code="name_required")
    if not isinstance(tasks, list) or not tasks:
        raise DomainError("At least one service task is required", code="tasks_required")
    normalized = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict) or not str(task.get("title", "")).strip():
            raise DomainError("Every service task needs a title", code="invalid_task")
        normalized.append(
            {
                "title": str(task["title"]).strip()[:240],
                "instructions": str(task.get("instructions", "")).strip(),
                "required": bool(task.get("required", True)),
                "sequence": int(task.get("sequence", index + 1)),
            }
        )
    normalized_parts = _normalize_expected_parts(expected_parts or [])
    with transaction.atomic():
        previous = (
            ServicePackage.objects.select_for_update()
            .filter(organization=organization, name=name)
            .order_by("-version")
            .first()
        )
        package = ServicePackage.objects.create(
            organization=organization,
            created_by=actor,
            name=name,
            version=(previous.version + 1 if previous else 1),
            description=description.strip(),
            tasks=normalized,
            expected_parts=normalized_parts,
            expected_labor_minutes=max(0, int(expected_labor_minutes)),
            supersedes=previous,
        )
        if previous and previous.active:
            ServicePackage.objects.filter(pk=previous.pk).update(active=False)
        plans_updated = 0
        if previous:
            plans_updated = MaintenancePlan.objects.filter(
                organization=organization,
                service_package=previous,
                active=True,
            ).update(service_package=package)
        audit(
            organization=organization,
            actor=actor,
            action="service_package.version_created",
            resource=package,
            context={
                "version": package.version,
                "supersedes_id": str(previous.pk) if previous else None,
                "active_plans_updated": plans_updated,
            },
        )
    return package


def create_maintenance_plan(
    *,
    organization: Organization,
    actor: User,
    asset: Asset,
    package: ServicePackage,
    name: str,
    triggers: list[dict[str, Any]],
) -> MaintenancePlan:
    _same_org(organization, asset, package)
    if not name.strip() or not triggers:
        raise DomainError("Plan name and at least one trigger are required", code="invalid_plan")
    try:
        with transaction.atomic():
            asset = _lock_serviceable_asset(organization, asset)
            package = ServicePackage.objects.select_for_update().get(
                pk=package.pk,
                organization=organization,
            )
            if not package.active:
                raise DomainError(
                    "An inactive service-package version cannot be assigned to new work",
                    code="inactive_service_package",
                    status=409,
                )
            plan = MaintenancePlan.objects.create(
                organization=organization, asset=asset, service_package=package, name=name.strip()
            )
            seen_kinds: set[str] = set()
            for data in triggers:
                kind = str(data.get("kind", ""))
                if kind not in dict(MaintenanceTrigger.KINDS) or kind in seen_kinds:
                    raise DomainError(
                        "Each trigger kind must be valid and unique", code="invalid_trigger"
                    )
                seen_kinds.add(kind)
                meter = None
                if kind != "date":
                    meter = Meter.objects.filter(
                        pk=str(data.get("meter_id")),
                        organization=organization,
                        asset=asset,
                        active=True,
                    ).first()
                    expected_meter_kind = "odometer" if kind == "mileage" else kind
                    if not meter or meter.kind != expected_meter_kind:
                        raise DomainError(
                            "Trigger meter is missing or has the wrong kind", code="invalid_meter"
                        )
                raw_completed_value = data.get("last_completed_value")
                trigger = MaintenanceTrigger(
                    organization=organization,
                    plan=plan,
                    kind=kind,
                    interval=_decimal(data.get("interval"), "interval"),
                    grace=_decimal(data.get("grace", 0), "grace"),
                    due_soon_threshold=_decimal(
                        data.get("due_soon_threshold", 0), "due soon threshold"
                    ),
                    meter=meter,
                    last_completed_at=(
                        data.get("last_completed_at")
                        if kind == "date" and data.get("last_completed_at") not in (None, "")
                        else None
                    ),
                    last_completed_value=(
                        _decimal(raw_completed_value, "last completed value")
                        if kind != "date" and raw_completed_value not in (None, "")
                        else None
                    ),
                    reset_rule=str(data.get("reset_rule", "completion")),
                )
                trigger.full_clean()
                trigger.save()
            calculate_plan_due(plan)
            audit(
                organization=organization,
                actor=actor,
                action="maintenance_plan.created",
                resource=plan,
                context={
                    "asset_id": str(asset.pk),
                    "trigger_kinds": sorted(seen_kinds),
                },
            )
            emit(
                organization=organization,
                event_type="maintenance.plan_projection_changed",
                resource=plan,
                payload=_pm_projection_payload(plan),
            )
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    except IntegrityError as exc:
        raise DomainError(
            "A plan with this name already exists for the asset", code="duplicate_plan", status=409
        ) from exc
    return plan


_DUE_RANK = {"Unknown": 0, "Current": 1, "DueSoon": 2, "Due": 3, "Overdue": 4}


def calculate_plan_due(plan: MaintenancePlan, *, as_of: datetime | None = None) -> MaintenancePlan:
    """Evaluate all triggers; the most urgent one wins (whichever comes first)."""
    as_of = as_of or timezone.now()
    reasons: list[dict[str, Any]] = []
    status = "Unknown"
    for trigger in plan.triggers.select_related("meter").all():
        if trigger.kind == "date":
            if trigger.last_completed_at is None:
                trigger_status = "Due"
                reasons.append(
                    {
                        "trigger_id": str(trigger.pk),
                        "kind": trigger.kind,
                        "status": trigger_status,
                        "message": "Last completion date is unknown; initial service is due",
                    }
                )
            else:
                due_at = trigger.last_completed_at + timedelta(days=float(trigger.interval))
                overdue_at = due_at + timedelta(days=float(trigger.grace))
                due_soon_at = due_at - timedelta(days=float(trigger.due_soon_threshold))
                trigger_status = (
                    "Overdue"
                    if as_of >= overdue_at
                    else "Due"
                    if as_of >= due_at
                    else "DueSoon"
                    if as_of >= due_soon_at
                    else "Current"
                )
                reasons.append(
                    {
                        "trigger_id": str(trigger.pk),
                        "kind": trigger.kind,
                        "status": trigger_status,
                        "due_at": due_at.isoformat(),
                        "grace_until": overdue_at.isoformat(),
                    }
                )
        else:
            reading = trigger.meter.current_reading if trigger.meter else None
            if trigger.last_completed_value is None:
                trigger_status = "Due"
                reason = {
                    "trigger_id": str(trigger.pk),
                    "kind": trigger.kind,
                    "status": trigger_status,
                    "message": "Last completion baseline is unknown; initial service is due",
                }
                if reading is not None:
                    reason.update(
                        {
                            "current_value": str(reading.value),
                            "unit": reading.meter.unit,
                            "reading_id": str(reading.pk),
                        }
                    )
                reasons.append(reason)
            elif reading is None:
                trigger_status = "Unknown"
                reasons.append(
                    {
                        "trigger_id": str(trigger.pk),
                        "kind": trigger.kind,
                        "status": trigger_status,
                        "message": "No accepted meter reading is available",
                    }
                )
            else:
                base_value = trigger.last_completed_value
                due_value = base_value + trigger.interval
                overdue_value = due_value + trigger.grace
                trigger_status = (
                    "Overdue"
                    if reading.value >= overdue_value
                    else "Due"
                    if reading.value >= due_value
                    else "DueSoon"
                    if reading.value >= due_value - trigger.due_soon_threshold
                    else "Current"
                )
                reasons.append(
                    {
                        "trigger_id": str(trigger.pk),
                        "kind": trigger.kind,
                        "status": trigger_status,
                        "current_value": str(reading.value),
                        "due_value": str(due_value),
                        "grace_until": str(overdue_value),
                        "unit": reading.meter.unit,
                        "reading_id": str(reading.pk),
                    }
                )
        if _DUE_RANK[trigger_status] > _DUE_RANK[status]:
            status = trigger_status
    if not reasons:
        status = "Unknown"
    MaintenancePlan.objects.filter(pk=plan.pk).update(
        due_status=status, due_reasons=reasons, last_calculated_at=as_of, updated_at=as_of
    )
    plan.due_status, plan.due_reasons, plan.last_calculated_at = status, reasons, as_of
    return plan


def _recalculate_plans(plans: Any, *, as_of: datetime | None = None) -> int:
    count = 0
    plans = plans.filter(asset__archived_at__isnull=True).exclude(
        asset__status=Asset.Status.RETIRED
    )
    plan_ids = sorted(plans.values_list("pk", flat=True), key=str)
    for plan_id in plan_ids:
        with transaction.atomic():
            plan = (
                MaintenancePlan.objects.select_for_update()
                .select_related("asset", "service_package")
                .filter(pk=plan_id)
                .first()
            )
            if plan is None:
                continue
            previous = plan.due_status
            calculate_plan_due(plan, as_of=as_of)
            if plan.due_status != previous:
                emit(
                    organization=plan.organization,
                    event_type="maintenance.plan_projection_changed",
                    resource=plan,
                    payload=_pm_projection_payload(plan),
                )
            if plan.due_status in {"DueSoon", "Due", "Overdue"} and plan.due_status != previous:
                emit(
                    organization=plan.organization,
                    event_type="maintenance.due",
                    resource=plan,
                    payload={
                        "asset_id": str(plan.asset_id),
                        "status": plan.due_status,
                        "summary": (
                            f"{plan.asset.unit_number}: {plan.name} is "
                            f"{plan.get_due_status_display().lower()}"
                        ),
                    },
                )
        count += 1
    return count


def recalculate_asset_plans(organization: Organization, asset_id: Any) -> int:
    """Worker entrypoint used after an accepted meter reading."""
    return _recalculate_plans(
        MaintenancePlan.objects.filter(
            organization=organization, asset_id=asset_id, active=True
        ).select_related("asset", "service_package")
    )


def recalculate_date_plans(*, as_of: datetime | None = None) -> int:
    """Worker entrypoint that keeps time-based PM status current without meter events."""
    return _recalculate_plans(
        MaintenancePlan.objects.filter(active=True, triggers__kind="date")
        .select_related("asset", "service_package")
        .distinct(),
        as_of=as_of,
    )


def create_inspection(
    *,
    organization: Organization,
    actor: User,
    asset: Asset,
    template: InspectionTemplate,
    responses: list[dict[str, Any]],
    acknowledgment: str = "",
    submit: bool = True,
    replaces: Inspection | None = None,
    source: str = "web",
) -> Inspection:
    _same_org(organization, asset, template, replaces)
    _reject_financial_payload(responses, "Inspection responses")
    if replaces and (replaces.status != "Voided" or replaces.asset_id != asset.pk):
        raise DomainError(
            "A replacement must reference a voided inspection for the same asset",
            code="invalid_inspection_replacement",
            status=409,
        )
    questions = template.questions
    if not isinstance(questions, list):
        raise DomainError("Inspection template is invalid", code="invalid_template")
    question_map = {str(q.get("id")): q for q in questions if isinstance(q, dict) and q.get("id")}
    response_map = {str(r.get("question_id")): r for r in responses if isinstance(r, dict)}
    missing = [
        qid
        for qid, question in question_map.items()
        if question.get("required") and qid not in response_map
    ]
    if submit and missing:
        raise DomainError(
            "Required inspection responses are missing",
            code="responses_required",
            details={"question_ids": missing},
        )
    now = timezone.now()
    with transaction.atomic():
        inspection = Inspection.objects.create(
            organization=organization,
            asset=asset,
            template=template,
            template_snapshot={
                "id": str(template.pk),
                "name": template.name,
                "version": template.version,
                "questions": questions,
                "retention_months": template.retention_months,
            },
            performed_by=actor,
            status="Draft",
            started_at=now,
            acknowledgment=acknowledgment.strip(),
            replaces=replaces,
        )
        abnormal: list[tuple[InspectionResponse, dict[str, Any]]] = []
        for qid, data in response_map.items():
            question = question_map.get(qid)
            if question is None:
                raise DomainError(
                    "Response references an unknown question",
                    code="invalid_question",
                    details={"question_id": qid},
                )
            result = str(data.get("result", ""))
            if result not in dict(InspectionResponse.RESULTS):
                raise DomainError("Inspection response result is invalid", code="invalid_response")
            row = InspectionResponse.objects.create(
                organization=organization,
                inspection=inspection,
                question_id=qid,
                question=str(question.get("label") or question.get("question") or qid)[:300],
                response_type=str(question.get("type", "pass_fail"))[:30],
                answer=data.get("answer"),
                result=result,
                notes=str(data.get("notes", "")).strip(),
                required=bool(question.get("required")),
                safety_critical=bool(question.get("safety_critical")),
            )
            if result == "fail" or data.get("abnormal") is True:
                abnormal.append((row, data))
        if submit:
            inspection.status = "Submitted"
            inspection.submitted_at = now
            inspection.save(update_fields=["status", "submitted_at", "updated_at"])
            findings: list[InspectionFinding] = []
            for response, data in abnormal:
                severity = (
                    "safety" if response.safety_critical else str(data.get("severity", "medium"))
                )
                if severity not in dict(InspectionFinding.SEVERITIES):
                    raise DomainError(
                        "Inspection finding severity is invalid", code="invalid_severity"
                    )
                description = str(
                    data.get("finding_description") or response.notes or response.question
                ).strip()
                finding = InspectionFinding.objects.create(
                    organization=organization,
                    inspection=inspection,
                    response=response,
                    asset=asset,
                    reported_by=actor,
                    severity=severity,
                    safety_related=response.safety_critical or severity == "safety",
                    description=description,
                )
                defect = Defect.objects.create(
                    organization=organization,
                    asset=asset,
                    inspection_response=response,
                    inspection_finding=finding,
                    reported_by=actor,
                    category="inspection",
                    description=description,
                    severity=severity,
                    safety_related=finding.safety_related,
                )
                findings.append(finding)
                audit(
                    organization=organization,
                    actor=actor,
                    action="inspection_finding.created",
                    resource=finding,
                    new_state="Open",
                    context={
                        "inspection_id": str(inspection.pk),
                        "inspection_response_id": str(response.pk),
                        "defect_id": str(defect.pk),
                    },
                    source=source,
                )
                emit(
                    organization=organization,
                    event_type="inspection_finding.created",
                    resource=finding,
                    payload={"defect_id": str(defect.pk)},
                )
                audit(
                    organization=organization,
                    actor=actor,
                    action="defect.created",
                    resource=defect,
                    new_state="Open",
                    context={
                        "inspection_id": str(inspection.pk),
                        "inspection_response_id": str(response.pk),
                        "inspection_finding_id": str(finding.pk),
                    },
                    source=source,
                )
                emit(organization=organization, event_type="defect.created", resource=defect)
            audit(
                organization=organization,
                actor=actor,
                action="inspection.submitted",
                resource=inspection,
                new_state="Submitted",
                context={
                    "abnormal_response_ids": [str(response.pk) for response, _ in abnormal],
                    "inspection_finding_ids": [str(finding.pk) for finding in findings],
                },
                source=source,
            )
            emit(organization=organization, event_type="inspection.submitted", resource=inspection)
        else:
            audit(
                organization=organization,
                actor=actor,
                action="inspection.draft_created",
                resource=inspection,
                source=source,
            )
    return inspection


def void_inspection_record(*, inspection: Inspection, actor: User, reason: str) -> Inspection:
    reason = reason.strip()
    if not reason:
        raise DomainError("A void reason is required", code="reason_required")
    with transaction.atomic():
        locked = Inspection.objects.select_for_update().get(pk=inspection.pk)
        if locked.status != "Submitted":
            raise DomainError(
                "Only a submitted inspection can be voided",
                code="invalid_inspection_transition",
                status=409,
            )
        locked.status, locked.void_reason, locked.voided_by, locked.voided_at = (
            "Voided",
            reason,
            actor,
            timezone.now(),
        )
        locked.save(update_fields=["status", "void_reason", "voided_by", "voided_at", "updated_at"])
        audit(
            organization=locked.organization,
            actor=actor,
            action="inspection.voided",
            resource=locked,
            previous_state="Submitted",
            new_state="Voided",
            context={"reason": reason},
        )
    return locked


def create_defect(
    *,
    organization: Organization,
    actor: User,
    asset: Asset,
    category: str,
    description: str,
    severity: str = "medium",
    safety_related: bool = False,
    record_id: uuid.UUID | None = None,
) -> Defect:
    _same_org(organization, asset)
    if severity not in dict(Defect.SEVERITIES):
        raise DomainError("Invalid defect severity", code="invalid_severity")
    if not category.strip() or not description.strip():
        raise DomainError(
            "Defect category and description are required", code="defect_fields_required"
        )
    with transaction.atomic():
        defect = Defect.objects.create(
            **({"id": record_id} if record_id is not None else {}),
            organization=organization,
            asset=asset,
            reported_by=actor,
            category=category.strip(),
            description=description.strip(),
            severity=severity,
            safety_related=safety_related or severity == "safety",
        )
        audit(
            organization=organization,
            actor=actor,
            action="defect.created",
            resource=defect,
            new_state="Open",
        )
        emit(organization=organization, event_type="defect.created", resource=defect)
    return defect


DEFECT_TRANSITIONS = {
    "Open": {"Acknowledged"},
    "Acknowledged": {"Deferred", "InRepair", "Corrected"},
    "Deferred": {"Acknowledged"},
    "InRepair": {"Corrected"},
    "Corrected": {"Verified"},
    "Verified": {"Closed"},
    "Closed": {"Open"},
}


def transition_defect(
    *,
    defect: Defect,
    actor: User,
    new_status: str,
    reason: str = "",
    auth: object | None = None,
) -> Defect:
    reason = reason.strip()
    with transaction.atomic():
        locked = Defect.objects.select_for_update().get(pk=defect.pk)
        previous = locked.status
        if new_status not in DEFECT_TRANSITIONS.get(previous, set()):
            raise DomainError(
                f"Defect cannot transition from {previous} to {new_status}",
                code="invalid_defect_transition",
                status=409,
            )
        if new_status in {"Deferred", "Open"} and not reason:
            raise DomainError("A reason is required", code="reason_required")
        if (
            new_status == "Deferred"
            and locked.safety_related
            and not has_permission(actor, "assets.manage", auth)
        ):
            raise DomainError(
                "Only a fleet manager may defer a safety defect",
                code="safety_override_required",
                status=403,
            )
        now = timezone.now()
        locked.status = new_status
        locked.disposition_reason = reason
        if new_status == "Acknowledged":
            locked.acknowledged_by, locked.acknowledged_at = actor, now
        if new_status == "Verified":
            locked.verified_by, locked.verified_at = actor, now
        locked.save()
        audit(
            organization=locked.organization,
            actor=actor,
            action="defect.transitioned",
            resource=locked,
            previous_state=previous,
            new_state=new_status,
            context={"reason": reason},
        )
    return locked


REQUEST_TRANSITIONS = {
    "Submitted": {"Triaged"},
    "Triaged": {"Approved", "Deferred", "Rejected"},
    "Deferred": {"Triaged"},
    "Rejected": {"Triaged"},
    "Approved": {"Converted", "Closed"},
    "Converted": {"Closed"},
    "Closed": {"Triaged"},
}


def create_request(
    *,
    organization: Organization,
    actor: User,
    asset: Asset,
    summary: str,
    description: str = "",
    priority: str = "normal",
    defect: Defect | None = None,
    alert: MaintenanceAlert | None = None,
    record_id: uuid.UUID | None = None,
) -> MaintenanceRequest:
    _same_org(organization, asset, defect, alert)
    if defect and alert:
        raise DomainError("A request may have one primary source", code="multiple_request_sources")
    if priority not in dict(MaintenanceRequest.PRIORITIES) or not summary.strip():
        raise DomainError("A valid summary and priority are required", code="invalid_request")
    with transaction.atomic():
        if defect:
            defect = (
                Defect.objects.select_for_update()
                .select_related("asset")
                .get(pk=defect.pk, organization=organization)
            )
            if defect.asset_id != asset.pk:
                raise DomainError(
                    "The defect belongs to a different asset", code="invalid_request_source"
                )
            if defect.maintenance_requests.exclude(status__in=["Rejected", "Closed"]).exists():
                raise DomainError(
                    "This defect already has an active request",
                    code="duplicate_request",
                    status=409,
                )
        request = MaintenanceRequest.objects.create(
            **({"id": record_id} if record_id is not None else {}),
            organization=organization,
            asset=asset,
            defect=defect,
            alert=alert,
            submitted_by=actor,
            summary=summary.strip(),
            description=description.strip(),
            priority=priority,
        )
        audit(
            organization=organization,
            actor=actor,
            action="maintenance_request.submitted",
            resource=request,
            new_state="Submitted",
        )
    return request


def transition_request(
    *,
    request: MaintenanceRequest,
    actor: User,
    new_status: str,
    reason: str = "",
    auth: object | None = None,
) -> MaintenanceRequest:
    reason = reason.strip()
    with transaction.atomic():
        locked = MaintenanceRequest.objects.select_for_update().get(pk=request.pk)
        previous = locked.status
        if new_status not in REQUEST_TRANSITIONS.get(previous, set()):
            raise DomainError(
                f"Request cannot transition from {previous} to {new_status}",
                code="invalid_request_transition",
                status=409,
            )
        if (
            new_status in {"Deferred", "Rejected", "Triaged"}
            and previous in {"Deferred", "Rejected", "Closed"}
            and not reason
        ):
            raise DomainError("A reason is required", code="reason_required")
        if new_status in {"Deferred", "Rejected"} and not reason:
            raise DomainError("A reason is required", code="reason_required")
        if locked.defect_id and new_status not in {"Rejected", "Closed"}:
            locked_defect = Defect.objects.select_for_update().get(
                pk=locked.defect_id,
                organization=locked.organization,
            )
            if (
                MaintenanceRequest.objects.filter(defect=locked_defect)
                .exclude(pk=locked.pk)
                .exclude(status__in=["Rejected", "Closed"])
                .exists()
            ):
                raise DomainError(
                    "This defect already has an active request",
                    code="duplicate_request",
                    status=409,
                )
        linked_defect_is_safety = bool(
            locked.defect_id
            and Defect.objects.filter(pk=locked.defect_id, safety_related=True).exists()
        )
        linked_alert_is_safety = bool(
            locked.alert_id
            and MaintenanceAlert.objects.filter(
                pk=locked.alert_id, severity__in=["safety", "critical"]
            ).exists()
        )
        safety_override = new_status in {"Deferred", "Rejected"} and (
            locked.priority == "safety" or linked_defect_is_safety or linked_alert_is_safety
        )
        if safety_override and not has_permission(actor, "assets.manage", auth):
            raise DomainError(
                "Only a fleet manager may defer or reject a safety request",
                code="safety_override_required",
                status=403,
            )
        locked.status, locked.decision_reason = new_status, reason
        if new_status == "Triaged":
            locked.triaged_by, locked.triaged_at = actor, timezone.now()
        locked.save()
        audit(
            organization=locked.organization,
            actor=actor,
            action="maintenance_request.transitioned",
            resource=locked,
            previous_state=previous,
            new_state=new_status,
            context={"reason": reason, "safety_override": safety_override},
        )
        if new_status == "Approved":
            emit(
                organization=locked.organization,
                event_type="maintenance_request.approved",
                resource=locked,
            )
    return locked


def create_work_order(
    *,
    organization: Organization,
    actor: User,
    asset: Asset,
    summary: str,
    request: MaintenanceRequest | None = None,
    plan: MaintenancePlan | None = None,
    package: ServicePackage | None = None,
    assigned_to: User | None = None,
    priority: str = "normal",
    complaint: str = "",
    requires_qc: bool = False,
    target_date: date | None = None,
    record_id: uuid.UUID | None = None,
) -> WorkOrder:
    _same_org(organization, asset, request, plan, package, assigned_to)
    if request and request.asset_id != asset.pk or plan and plan.asset_id != asset.pk:
        raise DomainError("Work-order source belongs to a different asset", code="invalid_source")
    if priority not in dict(WorkOrder.PRIORITIES) or not summary.strip():
        raise DomainError("A valid summary and priority are required", code="invalid_work_order")
    work_id = record_id or uuid.uuid4()
    with transaction.atomic():
        asset = _lock_serviceable_asset(organization, asset)
        if request:
            locked_request = MaintenanceRequest.objects.select_for_update().get(pk=request.pk)
            if locked_request.status != "Approved":
                raise DomainError(
                    "Only an approved request can become a work order",
                    code="request_not_approved",
                    status=409,
                )
            if hasattr(locked_request, "work_order"):
                raise DomainError(
                    "This request already has a work order", code="duplicate_work_order", status=409
                )
        if plan:
            plan = (
                MaintenancePlan.objects.select_for_update()
                .select_related("service_package")
                .get(pk=plan.pk, organization=organization)
            )
            if plan.asset_id != asset.pk:
                raise DomainError(
                    "Work-order source belongs to a different asset", code="invalid_source"
                )
            if (
                WorkOrder.objects.filter(organization=organization, maintenance_plan=plan)
                .exclude(status__in=["Closed", "Cancelled"])
                .exists()
            ):
                raise DomainError(
                    "This plan already has an open work order",
                    code="duplicate_planned_work",
                    status=409,
                )
        package = package or (plan.service_package if plan else None)
        work_order = WorkOrder.objects.create(
            id=work_id,
            organization=organization,
            number=f"WO-{timezone.localdate().year}-{str(work_id)[:8].upper()}",
            asset=asset,
            request=request,
            maintenance_plan=plan,
            service_package=package,
            service_package_snapshot=_snapshot_package(package),
            created_by=actor,
            assigned_to=assigned_to,
            priority=priority,
            summary=summary.strip(),
            complaint=complaint.strip(),
            requires_qc=requires_qc,
            target_date=target_date,
        )
        initial_assignment_event_ids: list[str] = []
        if assigned_to is not None:
            removed, added = sync_legacy_work_order_lead(
                work_order=work_order,
                actor=actor,
                assigned_to=assigned_to,
                reason="Initial work-order assignment",
            )
            initial_assignment_event_ids = [str(event.pk) for event in [*removed, *added]]
        for index, task in enumerate(work_order.service_package_snapshot.get("tasks", [])):
            WorkOrderTask.objects.create(
                id=uuid.uuid5(work_id, f"task:{index}"),
                organization=organization,
                work_order=work_order,
                title=str(task.get("title", "Task"))[:240],
                instructions=str(task.get("instructions", "")),
                required=bool(task.get("required", True)),
                sequence=int(task.get("sequence", index + 1)),
            )
        if request:
            locked_request.status = "Converted"
            locked_request.save(update_fields=["status", "updated_at"])
            defect = request.defect
            if defect and defect.status in {"Acknowledged", "Deferred"}:
                transition_defect(defect=defect, actor=actor, new_status="InRepair")
        audit(
            organization=organization,
            actor=actor,
            action="work_order.created",
            resource=work_order,
            new_state="Draft",
            context={
                "request_id": str(request.pk) if request else None,
                "plan_id": str(plan.pk) if plan else None,
                "assignment_event_ids": initial_assignment_event_ids,
            },
        )
    return work_order


WORK_ORDER_TRANSITIONS = {
    "Draft": {"Ready", "Cancelled"},
    "Ready": {"InProgress", "Blocked", "Cancelled"},
    "InProgress": {"Blocked", "QC", "Completed"},
    "Blocked": {"Ready", "InProgress", "Cancelled"},
    "QC": {"InProgress", "Completed"},
    "Completed": {"Closed"},
    "Closed": {"Reopened"},
    "Reopened": {"Ready", "InProgress", "Cancelled"},
    "Cancelled": set(),
}


def _reset_plan(
    plan: MaintenancePlan,
    completed_at: datetime,
    completion_meter: MeterReading | None = None,
) -> None:
    for trigger in plan.triggers.select_related("meter").all():
        if trigger.kind == "date":
            if trigger.reset_rule == "scheduled" and trigger.last_completed_at:
                trigger.last_completed_at += timedelta(days=float(trigger.interval))
            else:
                trigger.last_completed_at = completed_at
            trigger.save(update_fields=["last_completed_at", "updated_at"])
        elif trigger.reset_rule == "scheduled" and trigger.last_completed_value is not None:
            trigger.last_completed_value += trigger.interval
            trigger.save(update_fields=["last_completed_value", "updated_at"])
        else:
            reading = (
                completion_meter
                if completion_meter and completion_meter.meter_id == trigger.meter_id
                else trigger.meter.readings.filter(
                    quality=MeterReading.Quality.ACCEPTED,
                    correction__isnull=True,
                    observed_at__lte=completed_at,
                )
                .order_by("-observed_at", "-received_at")
                .first()
                if trigger.meter
                else None
            )
            if not reading:
                raise DomainError(
                    "An accepted completion meter is required",
                    code="completion_meter_required",
                    status=409,
                    details={"meter_id": str(trigger.meter_id)},
                )
            trigger.last_completed_value = reading.value
            trigger.save(update_fields=["last_completed_value", "updated_at"])
    calculate_plan_due(plan, as_of=completed_at)


def _plan_trigger_state(
    plan: MaintenancePlan, *, lock: bool = False
) -> list[dict[str, str | None]]:
    triggers = plan.triggers.order_by("id")
    if lock:
        triggers = triggers.select_for_update()
    return [
        {
            "id": str(trigger.pk),
            "last_completed_at": (
                trigger.last_completed_at.isoformat() if trigger.last_completed_at else None
            ),
            "last_completed_value": (
                str(trigger.last_completed_value)
                if trigger.last_completed_value is not None
                else None
            ),
        }
        for trigger in triggers
    ]


def _pm_projection_payload(
    plan: MaintenancePlan, work_order: WorkOrder | None = None
) -> dict[str, Any]:
    if plan.last_calculated_at is None:
        raise RuntimeError("A PM projection cannot be emitted before due calculation")
    fields = (
        "trigger_id",
        "kind",
        "status",
        "due_at",
        "due_value",
        "grace_until",
        "unit",
        "message",
    )
    return {
        "schema_version": "1.0",
        "asset_id": str(plan.asset_id),
        "source_system": plan.asset.source_system or None,
        "external_id": plan.asset.external_id or None,
        "work_order_id": str(work_order.pk) if work_order else None,
        "maintenance_plan_id": str(plan.pk),
        "due_status": plan.due_status,
        "next_due": [
            {field: reason[field] for field in fields if field in reason}
            for reason in plan.due_reasons
        ],
        "calculated_at": plan.last_calculated_at.isoformat(),
    }


def _restore_plan_trigger_state(plan: MaintenancePlan, state: list[dict[str, str | None]]) -> None:
    baseline = {str(item["id"]): item for item in state}
    triggers = list(plan.triggers.select_for_update().order_by("id"))
    if set(baseline) != {str(trigger.pk) for trigger in triggers}:
        raise DomainError(
            "The maintenance plan changed after this work order closed",
            code="maintenance_plan_changed",
            status=409,
        )
    for trigger in triggers:
        saved = baseline[str(trigger.pk)]
        raw_completed_at = saved.get("last_completed_at")
        raw_completed_value = saved.get("last_completed_value")
        trigger.last_completed_at = (
            datetime.fromisoformat(raw_completed_at) if raw_completed_at else None
        )
        trigger.last_completed_value = (
            Decimal(raw_completed_value) if raw_completed_value is not None else None
        )
        trigger.save(update_fields=["last_completed_at", "last_completed_value", "updated_at"])


def _apply_plan_reset_for_close(
    work_order: WorkOrder, closed_at: datetime
) -> tuple[list[dict[str, str | None]] | None, list[dict[str, str | None]], bool, str]:
    if not work_order.maintenance_plan_id:
        return None, [], False, ""
    plan = MaintenancePlan.objects.select_for_update().get(pk=work_order.maintenance_plan_id)
    current = _plan_trigger_state(plan, lock=True)
    prior_close = work_order.close_snapshots.order_by("-sequence").first()
    if prior_close:
        baseline = prior_close.snapshot.get("plan_reset_baseline")
        prior_after = prior_close.snapshot.get("plan_reset_after")
        if not isinstance(baseline, list) or not isinstance(prior_after, list):
            return None, current, False, "legacy_snapshot_has_no_reset_baseline"
        if current != prior_after:
            return baseline, current, False, "plan_advanced_after_prior_close"
        _restore_plan_trigger_state(plan, baseline)
    else:
        baseline = current
    _reset_plan(plan, work_order.completed_at or closed_at, work_order.completion_meter)
    return baseline, _plan_trigger_state(plan), True, ""


def _release_reservations_for_close(work_order: WorkOrder, actor: User) -> list[dict[str, str]]:
    from inventory.models import Reservation
    from inventory.services import release_reservation

    released: list[dict[str, str]] = []
    reservations = Reservation.objects.filter(
        organization=work_order.organization,
        work_order=work_order,
        status__in=[
            Reservation.Status.PENDING,
            Reservation.Status.ACTIVE,
            Reservation.Status.PARTIALLY_ISSUED,
        ],
    ).order_by("id")
    for reservation in reservations:
        remaining = reservation.remaining_quantity
        if remaining <= 0:
            continue
        result = release_reservation(
            organization=work_order.organization,
            actor=actor,
            reservation=reservation,
            reason=f"Remaining reservation released when {work_order.number} closed",
        )
        released.append({"reservation_id": str(result.pk), "quantity": str(remaining)})
    return released


def _close_snapshot_payload(
    work_order: WorkOrder,
    *,
    closed_at: datetime,
    released_reservations: list[dict[str, str]],
    plan_reset_baseline: list[dict[str, str | None]] | None,
    plan_reset_after: list[dict[str, str | None]],
    plan_reset_applied: bool,
    plan_reset_skip_reason: str,
) -> dict[str, Any]:
    from assets.services import _meter_snapshots_as_of
    from inventory.models import Reservation, StockTransaction

    work_payload = work_order.to_dict(include_financial=True)
    tasks = work_payload.pop("tasks")
    work_payload.update(
        {
            "created_by_id": str(work_order.created_by_id),
            "completed_by_id": (
                str(work_order.completed_by_id) if work_order.completed_by_id else None
            ),
            "closed_by_id": str(work_order.closed_by_id) if work_order.closed_by_id else None,
            "service_package_id": (
                str(work_order.service_package_id) if work_order.service_package_id else None
            ),
        }
    )
    stock_transactions = StockTransaction.objects.filter(
        organization=work_order.organization, work_order=work_order
    ).select_related("part", "bin", "bin__warehouse")
    reservations = Reservation.objects.filter(
        organization=work_order.organization, work_order=work_order
    ).select_related("part", "bin", "bin__warehouse")
    attachments = Attachment.objects.filter(
        organization=work_order.organization,
        resource_type="WorkOrder",
        resource_id=str(work_order.pk),
    ).order_by("created_at", "id")
    comments = Comment.objects.filter(
        organization=work_order.organization,
        resource_type="WorkOrder",
        resource_id=str(work_order.pk),
    ).order_by("created_at", "id")
    payload = {
        "schema_version": 1,
        "work_order": work_payload,
        "tasks": tasks,
        "labor_entries": [
            entry.to_dict(include_financial=True)
            for entry in work_order.labor_entries.select_related("technician").all()
        ],
        "stock_transactions": [
            entry.to_dict(include_financial=True) for entry in stock_transactions
        ],
        "reservations": [entry.to_dict() for entry in reservations],
        "attachments": [
            {
                "id": str(attachment.pk),
                "document_key": str(attachment.document_key),
                "category": attachment.category,
                "title": attachment.title,
                "version": attachment.version,
                "supersedes_id": (
                    str(attachment.supersedes_id) if attachment.supersedes_id else None
                ),
                "original_name": attachment.original_name,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "sha256": attachment.sha256,
                "uploader_id": str(attachment.uploader_id),
                "created_at": attachment.created_at,
            }
            for attachment in attachments
        ],
        "comments": [
            {
                "id": str(comment.pk),
                "author_id": str(comment.author_id),
                "body": comment.body,
                "created_at": comment.created_at,
            }
            for comment in comments
        ],
        "released_reservations": released_reservations,
        "plan_reset_baseline": plan_reset_baseline,
        "plan_reset_after": plan_reset_after,
        "plan_reset_applied": plan_reset_applied,
        "plan_reset_skip_reason": plan_reset_skip_reason,
        # Freeze the truck's usage at close. A non-PM repair closed without a
        # completion_meter would otherwise leave no mileage on the record at all.
        "asset_meters": _meter_snapshots_as_of(work_order.asset, closed_at),
    }
    return json.loads(json.dumps(payload, default=str))


def transition_work_order(
    *,
    work_order: WorkOrder,
    actor: User,
    new_status: str,
    reason: str = "",
    completion_summary: str = "",
    completion_meter: MeterReading | None = None,
) -> WorkOrder:
    reason, completion_summary = reason.strip(), completion_summary.strip()
    with transaction.atomic():
        locked = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
        released_reservations: list[dict[str, str]] = []
        plan_reset_baseline: list[dict[str, str | None]] | None = None
        plan_reset_after: list[dict[str, str | None]] = []
        plan_reset_applied = False
        plan_reset_skip_reason = ""
        previous = locked.status
        if new_status not in WORK_ORDER_TRANSITIONS.get(previous, set()):
            raise DomainError(
                f"Work order cannot transition from {previous} to {new_status}",
                code="invalid_work_order_transition",
                status=409,
            )
        if new_status in {"Blocked", "Cancelled", "Reopened"} and not reason:
            raise DomainError("A reason is required", code="reason_required")
        if locked.maintenance_plan_id and new_status not in {"Closed", "Cancelled"}:
            locked_plan = MaintenancePlan.objects.select_for_update().get(
                pk=locked.maintenance_plan_id,
                organization=locked.organization,
            )
            if (
                WorkOrder.objects.filter(maintenance_plan=locked_plan)
                .exclude(pk=locked.pk)
                .exclude(status__in=["Closed", "Cancelled"])
                .exists()
            ):
                raise DomainError(
                    "This plan already has an open work order",
                    code="duplicate_planned_work",
                    status=409,
                )
        if new_status in {"QC", "Completed"}:
            incomplete = list(
                locked.tasks.filter(required=True)
                .exclude(status="Completed")
                .values_list("id", flat=True)
            )
            if incomplete:
                raise DomainError(
                    "Required tasks must be completed",
                    code="required_tasks_incomplete",
                    status=409,
                    details={"task_ids": [str(pk) for pk in incomplete]},
                )
            if new_status == "Completed" and locked.requires_qc and previous != "QC":
                raise DomainError(
                    "This work order requires quality control", code="qc_required", status=409
                )
            if new_status == "Completed" and not (completion_summary or locked.completion_summary):
                raise DomainError(
                    "A completion summary is required", code="completion_summary_required"
                )
        now = timezone.now()
        locked.status = new_status
        locked.version += 1
        if new_status == "Ready":
            locked.ready_at = now
        elif new_status == "InProgress" and not locked.started_at:
            locked.started_at = now
        elif new_status == "Blocked":
            locked.blocked_reason = reason
        elif new_status == "Completed":
            locked.completed_at, locked.completed_by = now, actor
            locked.completion_summary = completion_summary or locked.completion_summary
            if completion_meter:
                _same_org(locked.organization, completion_meter)
                if completion_meter.meter.asset_id != locked.asset_id:
                    raise DomainError(
                        "Completion meter belongs to another asset", code="invalid_completion_meter"
                    )
                if completion_meter.quality != MeterReading.Quality.ACCEPTED:
                    raise DomainError(
                        "Completion meter must be accepted", code="invalid_completion_meter"
                    )
                locked.completion_meter = completion_meter
        elif new_status == "Closed":
            locked.closed_at, locked.closed_by = now, actor
            released_reservations = _release_reservations_for_close(locked, actor)
            (
                plan_reset_baseline,
                plan_reset_after,
                plan_reset_applied,
                plan_reset_skip_reason,
            ) = _apply_plan_reset_for_close(locked, now)
        elif new_status == "Reopened":
            locked.reopen_reason = reason
        locked.save()
        close_snapshot = None
        if new_status == "Closed":
            last_sequence = (
                locked.close_snapshots.order_by("-sequence")
                .values_list("sequence", flat=True)
                .first()
                or 0
            )
            close_snapshot = WorkOrderCloseSnapshot.objects.create(
                id=uuid.uuid5(locked.pk, f"close:{last_sequence + 1}"),
                organization=locked.organization,
                work_order=locked,
                sequence=last_sequence + 1,
                closed_by=actor,
                closed_at=now,
                snapshot=_close_snapshot_payload(
                    locked,
                    closed_at=now,
                    released_reservations=released_reservations,
                    plan_reset_baseline=plan_reset_baseline,
                    plan_reset_after=plan_reset_after,
                    plan_reset_applied=plan_reset_applied,
                    plan_reset_skip_reason=plan_reset_skip_reason,
                ),
            )
            audit(
                organization=locked.organization,
                actor=actor,
                action="work_order.close_snapshot_created",
                resource=close_snapshot,
                new_state="recorded",
                context={"work_order_id": str(locked.pk), "sequence": close_snapshot.sequence},
            )
        audit_context: dict[str, Any] = {"reason": reason}
        if close_snapshot:
            audit_context.update(
                {
                    "close_snapshot_id": str(close_snapshot.pk),
                    "released_reservations": released_reservations,
                    "plan_reset_applied": plan_reset_applied,
                    "plan_reset_skip_reason": plan_reset_skip_reason,
                }
            )
        audit(
            organization=locked.organization,
            actor=actor,
            action="work_order.transitioned",
            resource=locked,
            previous_state=previous,
            new_state=new_status,
            context=audit_context,
        )
        event = {
            "InProgress": "work_order.started",
            "Blocked": "work_order.blocked",
            "Completed": "work_order.completed",
        }.get(new_status)
        if event:
            emit(organization=locked.organization, event_type=event, resource=locked)
        if new_status == "Closed" and plan_reset_applied and locked.maintenance_plan_id is not None:
            plan = MaintenancePlan.objects.select_related("asset").get(
                pk=locked.maintenance_plan_id,
                organization=locked.organization,
            )
            emit(
                organization=locked.organization,
                event_type="maintenance.plan_projection_changed",
                resource=plan,
                payload=_pm_projection_payload(plan, locked),
            )
    return locked


UNSET: Any = object()


def _component_on_asset(component: Any, asset: Any) -> Any:
    """A task may tag a component installed here, or one not installed anywhere.

    The transmission about to go in is not on the asset yet, so an uninstalled
    component is legitimate; only one open on a *different* asset is rejected.
    """
    from assets.models import ComponentInstallation

    if component is None:
        return None
    if component.organization_id != asset.organization_id:
        raise DomainError("Unknown component", code="invalid_reference")
    open_row = (
        ComponentInstallation.objects.filter(component=component, removed_at__isnull=True)
        .select_related("asset")
        .first()
    )
    if open_row is not None and open_row.asset_id != asset.pk:
        raise DomainError(
            "That component is on another asset",
            code="component_not_on_asset",
            status=409,
            details={
                "asset_id": str(open_row.asset_id),
                "unit_number": open_row.asset.unit_number,
            },
        )
    return component


def update_work_order_task(
    *,
    task: WorkOrderTask,
    actor: User,
    status: str,
    notes: str = "",
    measurement: dict[str, Any] | None = None,
    base_version: int | None = None,
    component: Any = UNSET,
) -> WorkOrderTask:
    if measurement is not None:
        _reject_financial_payload(measurement, "Task measurement")
    if status not in dict(WorkOrderTask.STATUSES):
        raise DomainError("Invalid task status", code="invalid_task_status")
    if task.required and status == "Skipped":
        raise DomainError("A required task cannot be skipped", code="required_task")
    with transaction.atomic():
        work_order = WorkOrder.objects.select_for_update().get(pk=task.work_order_id)
        if base_version is not None and work_order.version != base_version:
            raise DomainError(
                "This work order changed on the server",
                code="sync_conflict",
                status=409,
                details={"work_order": work_order.to_dict()},
            )
        if work_order.status in {"Completed", "Closed", "Cancelled"}:
            raise DomainError(
                "Tasks on this work order cannot be changed",
                code="work_order_not_editable",
                status=409,
            )
        locked = WorkOrderTask.objects.select_for_update().get(pk=task.pk)
        previous = locked.status
        locked.status, locked.notes, locked.measurement = status, notes.strip(), measurement or {}
        if component is not UNSET:
            locked.component = _component_on_asset(component, work_order.asset)
        if status == "Completed":
            locked.completed_at, locked.completed_by = timezone.now(), actor
        else:
            locked.completed_at, locked.completed_by = None, None
        locked.save()
        work_order.version += 1
        work_order.save(update_fields=["version", "updated_at"])
        audit(
            organization=locked.organization,
            actor=actor,
            action="work_order_task.transitioned",
            resource=locked,
            previous_state=previous,
            new_state=status,
            context={"work_order_id": str(work_order.pk)},
        )
    return locked


def create_labor_entry(
    *,
    work_order: WorkOrder,
    actor: User,
    technician: User,
    minutes: int,
    hourly_rate: object = 0,
    note: str = "",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    corrects: LaborEntry | None = None,
    record_id: uuid.UUID | None = None,
) -> LaborEntry:
    if minutes <= 0 or minutes > 24 * 60:
        raise DomainError("Labor minutes must be between 1 and 1440", code="invalid_labor_duration")
    if ended_at and started_at and ended_at <= started_at:
        raise DomainError("Labor end time must follow start time", code="invalid_labor_time")
    note = note.strip()
    if corrects and not note:
        raise DomainError("A correction reason is required", code="reason_required")
    with transaction.atomic():
        locked_work = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
        _same_org(locked_work.organization, technician)
        if locked_work.status in {"Completed", "Closed", "Cancelled"}:
            raise DomainError(
                "Labor on this work order cannot be changed until it is reopened",
                code="work_order_not_editable",
                status=409,
            )
        locked_original = None
        if corrects:
            locked_original = (
                LaborEntry.objects.select_for_update()
                .filter(pk=corrects.pk, organization=locked_work.organization)
                .first()
            )
            if not locked_original:
                raise DomainError(
                    "Corrected labor is outside this organization", code="invalid_reference"
                )
            if locked_original.work_order_id != locked_work.pk:
                raise DomainError(
                    "Corrected labor belongs to another work order",
                    code="invalid_labor_correction",
                )
            existing = LaborEntry.objects.filter(corrects=locked_original).first()
            if existing:
                raise DomainError(
                    "This labor entry was already corrected; correct the latest record instead",
                    code="labor_already_corrected",
                    status=409,
                    details={"correction_id": str(existing.pk)},
                )
        entry = LaborEntry.objects.create(
            **({"id": record_id} if record_id is not None else {}),
            organization=locked_work.organization,
            work_order=locked_work,
            technician=technician,
            minutes=minutes,
            hourly_rate=_decimal(hourly_rate, "hourly rate"),
            note=note,
            started_at=started_at,
            ended_at=ended_at,
            corrects=locked_original,
        )
        audit(
            organization=locked_work.organization,
            actor=actor,
            action="labor.corrected" if locked_original else "labor.recorded",
            resource=entry,
            previous_state=(
                f"{locked_original.minutes} minutes / {locked_original.cost}"
                if locked_original
                else ""
            ),
            new_state=f"{entry.minutes} minutes / {entry.cost}",
            context={
                "work_order_id": str(locked_work.pk),
                "minutes": minutes,
                "corrects_id": str(locked_original.pk) if locked_original else None,
                "reason": note if locked_original else "",
            },
        )
    return entry


def record_alert_occurrence(
    *,
    organization: Organization,
    asset: Asset,
    source_id: str,
    dedupe_key: str,
    title: str,
    observed_at: datetime,
    severity: str = "warning",
    description: str = "",
    rule_version: str = "",
    source_type: str = "telematics",
) -> tuple[MaintenanceAlert, bool]:
    """Public adapter boundary: consolidate repeated telemetry without creating work."""
    _same_org(organization, asset)
    if not dedupe_key.strip() or not title.strip():
        raise DomainError("Alert title and dedupe key are required", code="invalid_alert")
    with transaction.atomic():
        alert = (
            MaintenanceAlert.objects.select_for_update()
            .filter(
                organization=organization,
                dedupe_key=dedupe_key,
                status__in=["New", "NeedsReview", "Acknowledged"],
            )
            .first()
        )
        if alert:
            alert.occurrence_count += 1
            alert.last_seen_at = max(alert.last_seen_at, observed_at)
            alert.save(update_fields=["occurrence_count", "last_seen_at", "updated_at"])
            return alert, False
        alert = MaintenanceAlert.objects.create(
            organization=organization,
            asset=asset,
            source_type=source_type,
            source_id=source_id,
            severity=severity,
            title=title.strip(),
            description=description.strip(),
            dedupe_key=dedupe_key,
            rule_version=rule_version,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        audit(
            organization=organization,
            actor=None,
            action="maintenance_alert.created",
            resource=alert,
            new_state="New",
            source=source_type,
        )
        emit(organization=organization, event_type="maintenance_alert.created", resource=alert)
        return alert, True


def _offline_fingerprint(operation: dict[str, Any]) -> str:
    payload = json.dumps(operation, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _staged_attachments(
    *,
    user: User,
    operation_id: uuid.UUID,
    operation_type: str,
    payload: dict[str, Any],
    operation: dict[str, Any],
) -> list[Attachment]:
    organization = user.organization
    if organization is None:
        raise DomainError("User has no organization", code="permission_denied", status=403)
    expected_types = {
        "defect.create": "defect",
        "inspection.submit": "inspection",
        "work_note.create": "work_note",
        "task.complete": "task",
    }
    raw_ids = operation.get("attachment_ids", payload.get("attachment_ids", []))
    if not isinstance(raw_ids, list) or len(raw_ids) > 10:
        raise DomainError(
            "attachment_ids must be a list of at most 10 IDs", code="invalid_attachments"
        )
    try:
        attachment_ids = [uuid.UUID(str(value)) for value in raw_ids]
    except (TypeError, ValueError) as exc:
        raise DomainError("Attachment IDs must be valid UUIDs", code="invalid_attachments") from exc
    if len(attachment_ids) != len(set(attachment_ids)):
        raise DomainError("Attachment IDs cannot be repeated", code="invalid_attachments")
    attachments = list(
        Attachment.objects.select_for_update().filter(
            pk__in=attachment_ids,
            organization=organization,
            uploader=user,
            resource_type=expected_types.get(operation_type, ""),
            resource_id=str(operation_id),
        )
    )
    if len(attachments) != len(attachment_ids):
        raise DomainError(
            "An attachment is missing or is not staged for this operation",
            code="invalid_attachments",
            status=409,
        )
    return attachments


def _link_staged_attachments(
    *, attachments: list[Attachment], target_type: str, target_id: str, actor: User
) -> None:
    for attachment in attachments:
        attachment.resource_type = target_type
        attachment.resource_id = target_id
        attachment.save(update_fields=["resource_type", "resource_id", "updated_at"])
        audit(
            organization=attachment.organization,
            actor=actor,
            action="attachment.linked",
            resource=attachment,
            context={"target_type": target_type, "target_id": target_id},
            source="offline_sync",
        )


def sync_field_operation(
    user: User, operation: dict[str, Any], auth: object | None = None
) -> dict[str, Any]:
    if not user.organization_id or not user.is_active:
        raise DomainError(
            "Offline access has been revoked", code="offline_access_revoked", status=403
        )
    organization = user.organization
    if organization is None:  # Narrow the nullable auth model after the access check.
        raise DomainError(
            "Offline access has been revoked", code="offline_access_revoked", status=403
        )
    try:
        operation_id = uuid.UUID(str(operation.get("operation_id")))
    except (TypeError, ValueError) as exc:
        raise DomainError("A valid operation_id is required", code="operation_id_required") from exc
    operation_type = str(operation.get("type", ""))
    payload = operation.get("payload") or {}
    if not isinstance(payload, dict):
        raise DomainError("Operation payload must be an object", code="invalid_payload")
    fingerprint = _offline_fingerprint(operation)
    route = f"offline:{operation_type}"

    def perform() -> dict[str, Any]:
        if operation_type == "defect.create":
            if not has_permission(user, "defects.create", auth):
                raise DomainError(
                    "Defect creation is not allowed", code="permission_denied", status=403
                )
            asset = Asset.objects.filter(
                pk=str(payload.get("asset_id")), organization=organization
            ).first()
            if not asset:
                raise DomainError("Asset not found", code="not_found", status=404)
            if (
                "driver" in user.role_slugs
                and not can_access_all_maintenance_assets(user, auth)
                and asset.assigned_driver_id != user.pk
            ):
                raise DomainError(
                    "Asset is not assigned to this driver", code="permission_denied", status=403
                )
            return create_defect(
                organization=organization,
                actor=user,
                asset=asset,
                category=str(payload.get("category", "other")),
                description=str(payload.get("description", "")),
                severity=str(payload.get("severity", "medium")),
                safety_related=bool(payload.get("safety_related")),
            ).to_dict()
        if operation_type == "inspection.submit":
            if not has_permission(user, "inspections.create", auth):
                raise DomainError(
                    "Inspection creation is not allowed", code="permission_denied", status=403
                )
            asset = Asset.objects.filter(
                pk=str(payload.get("asset_id")), organization=organization
            ).first()
            template = InspectionTemplate.objects.filter(
                pk=str(payload.get("template_id")), organization=organization, active=True
            ).first()
            if not asset or not template:
                raise DomainError(
                    "Asset or inspection template not found", code="not_found", status=404
                )
            if (
                "driver" in user.role_slugs
                and not can_access_all_maintenance_assets(user, auth)
                and asset.assigned_driver_id != user.pk
            ):
                raise DomainError(
                    "Asset is not assigned to this driver", code="permission_denied", status=403
                )
            return create_inspection(
                organization=organization,
                actor=user,
                asset=asset,
                template=template,
                responses=payload.get("responses", []),
                acknowledgment=str(payload.get("acknowledgment", "")),
                source="offline_sync",
            ).to_dict(include_financial=can_view_financials(user, auth))
        if operation_type == "work_note.create":
            work_order = WorkOrder.objects.filter(
                pk=str(payload.get("work_order_id")), organization=organization
            ).first()
            if not work_order:
                raise DomainError("Work order not found", code="not_found", status=404)
            if not (
                has_permission(user, "maintenance.manage", auth)
                or has_permission(user, "maintenance.execute", auth)
                and is_active_work_order_assignee(work_order, user)
            ):
                raise DomainError(
                    "Work order is not assigned to this user", code="permission_denied", status=403
                )
            body = str(payload.get("body", "")).strip()
            if not body:
                raise DomainError("A note is required", code="note_required")
            note = Comment.objects.create(
                organization=organization,
                author=user,
                resource_type="WorkOrder",
                resource_id=str(work_order.pk),
                body=body,
            )
            audit(
                organization=organization,
                actor=user,
                action="work_order.note_created",
                resource=work_order,
                context={"comment_id": str(note.pk)},
            )
            return {"id": str(note.pk), "body": note.body, "work_order_id": str(work_order.pk)}
        if operation_type == "task.complete":
            task = (
                WorkOrderTask.objects.select_related("work_order")
                .filter(pk=str(payload.get("task_id")), organization=organization)
                .first()
            )
            if not task:
                raise DomainError("Task not found", code="not_found", status=404)
            if not (
                has_permission(user, "maintenance.manage", auth)
                or has_permission(user, "maintenance.execute", auth)
                and is_active_work_order_assignee(task.work_order, user)
            ):
                raise DomainError(
                    "Work order is not assigned to this user", code="permission_denied", status=403
                )
            return update_work_order_task(
                task=task,
                actor=user,
                status="Completed",
                notes=str(payload.get("notes", "")),
                measurement=payload.get("measurement"),
                base_version=payload.get("base_version"),
            ).to_dict(include_financial=can_view_financials(user, auth))
        raise DomainError("Unsupported offline operation", code="unsupported_offline_operation")

    with transaction.atomic():
        try:
            record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
                organization=organization,
                user=user,
                route=route,
                key=str(operation_id),
                defaults={"request_fingerprint": fingerprint},
            )
        except IntegrityError:
            record = IdempotencyRecord.objects.select_for_update().get(
                organization=organization, user=user, route=route, key=str(operation_id)
            )
            created = False
        if not created:
            if record.request_fingerprint != fingerprint:
                raise DomainError(
                    "Operation ID was already used for different input",
                    code="idempotency_conflict",
                    status=409,
                )
            if record.state == "complete":
                if not isinstance(record.response_body, dict):
                    raise DomainError(
                        "Stored offline result is invalid",
                        code="invalid_idempotency_record",
                        status=500,
                    )
                replay_result = (
                    record.response_body
                    if can_view_financials(user, auth)
                    else redact_financial_fields(record.response_body)
                )
                if not isinstance(replay_result, dict):
                    raise DomainError(
                        "Stored offline result is invalid",
                        code="invalid_idempotency_record",
                        status=500,
                    )
                return cast(dict[str, Any], replay_result)
        attachments = _staged_attachments(
            user=user,
            operation_id=operation_id,
            operation_type=operation_type,
            payload=payload,
            operation=operation,
        )
        result = perform()
        target_type = {
            "defect.create": "Defect",
            "inspection.submit": "Inspection",
            "work_note.create": "WorkOrder",
            "task.complete": "WorkOrderTask",
        }[operation_type]
        target_id = (
            result["work_order_id"] if operation_type == "work_note.create" else result["id"]
        )
        _link_staged_attachments(
            attachments=attachments, target_type=target_type, target_id=target_id, actor=user
        )
        record.state, record.response_status, record.response_body = (
            "complete",
            200,
            json.loads(json.dumps(result, default=str)),
        )
        record.save(update_fields=["state", "response_status", "response_body", "updated_at"])
        return result
