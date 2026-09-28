from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any

from core.models import ImmutableModel, OrganizationOwnedModel
from core.permissions import redact_financial_fields
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

_EXTERNAL_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,39}$")


def normalize_external_employee_identity(
    source_system: object, external_employee_id: object
) -> tuple[str, str]:
    """Return a stable, bounded external-person identity.

    The external ID remains opaque: GatorHub currently uses an Employee UUID, but a
    maintenance record must not assume every future identity provider does.
    """

    source = str(source_system or "").strip().lower()
    identifier = str(external_employee_id or "").strip()
    if not _EXTERNAL_SOURCE_PATTERN.fullmatch(source) or not 1 <= len(identifier) <= 160:
        raise ValidationError("External employee identity is invalid")
    return source, identifier


class ServicePackage(OrganizationOwnedModel):
    name = models.CharField(max_length=160)
    version = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)
    tasks = models.JSONField(default=list)
    expected_parts = models.JSONField(default=list, blank=True)
    expected_labor_minutes = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="service_packages_created"
    )

    class Meta:
        ordering = ["name", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name", "version"], name="uniq_service_package_version"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tasks": self.tasks,
            "expected_parts": (
                self.expected_parts
                if include_financial
                else redact_financial_fields(self.expected_parts)
            ),
            "expected_labor_minutes": self.expected_labor_minutes,
            "active": self.active,
            "created_at": self.created_at,
        }


class MaintenancePlan(OrganizationOwnedModel):
    DUE_STATUSES = [
        ("Unknown", "Unknown"),
        ("Current", "Current"),
        ("DueSoon", "Due soon"),
        ("Due", "Due"),
        ("Overdue", "Overdue"),
    ]

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="maintenance_plans"
    )
    service_package = models.ForeignKey(
        ServicePackage, on_delete=models.PROTECT, related_name="maintenance_plans"
    )
    name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)
    due_status = models.CharField(max_length=16, choices=DUE_STATUSES, default="Unknown")
    due_reasons = models.JSONField(default=list, blank=True)
    last_calculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["asset__unit_number", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "asset", "name"], name="uniq_maintenance_plan_asset_name"
            )
        ]

    def __str__(self) -> str:
        return f"{self.asset}: {self.name}"

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        triggers = self.triggers.select_related("meter").all()
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "name": self.name,
            "active": self.active,
            "due_status": self.due_status,
            "due_reasons": self.due_reasons,
            "last_calculated_at": self.last_calculated_at,
            "service_package": self.service_package.to_dict(include_financial=include_financial),
            "triggers": [trigger.to_dict() for trigger in triggers],
        }


class MaintenanceTrigger(OrganizationOwnedModel):
    KINDS = [("date", "Date"), ("mileage", "Mileage"), ("engine_hours", "Engine hours")]
    RESET_RULES = [("completion", "Actual completion"), ("scheduled", "Prior due value")]

    plan = models.ForeignKey(MaintenancePlan, on_delete=models.CASCADE, related_name="triggers")
    kind = models.CharField(max_length=20, choices=KINDS)
    interval = models.DecimalField(max_digits=12, decimal_places=2)
    grace = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    due_soon_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    meter = models.ForeignKey(
        "assets.Meter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="maintenance_triggers",
    )
    last_completed_at = models.DateTimeField(null=True, blank=True)
    last_completed_value = models.DecimalField(
        max_digits=16, decimal_places=3, null=True, blank=True
    )
    reset_rule = models.CharField(max_length=16, choices=RESET_RULES, default="completion")

    class Meta:
        ordering = ["kind", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "kind"], name="uniq_maintenance_trigger_kind")
        ]

    def clean(self) -> None:
        if self.interval <= 0:
            raise ValidationError({"interval": "Interval must be greater than zero"})
        if self.grace < 0 or self.due_soon_threshold < 0:
            raise ValidationError("Grace and due-soon threshold cannot be negative")
        if self.kind == "date" and self.meter_id:
            raise ValidationError({"meter": "Date triggers do not use a meter"})
        if self.kind != "date" and not self.meter_id:
            raise ValidationError({"meter": "Meter triggers require a meter"})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "kind": self.kind,
            "interval": str(self.interval),
            "grace": str(self.grace),
            "due_soon_threshold": str(self.due_soon_threshold),
            "meter_id": str(self.meter_id) if self.meter_id else None,
            "last_completed_at": self.last_completed_at,
            "last_completed_value": (
                format(self.last_completed_value, ".3f")
                if self.last_completed_value is not None
                else None
            ),
            "reset_rule": self.reset_rule,
        }


class InspectionTemplate(OrganizationOwnedModel):
    name = models.CharField(max_length=160)
    version = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)
    questions = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    retention_months = models.PositiveSmallIntegerField(default=14)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inspection_templates_created",
    )

    class Meta:
        ordering = ["name", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name", "version"], name="uniq_inspection_template_version"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "questions": (
                self.questions if include_financial else redact_financial_fields(self.questions)
            ),
            "active": self.active,
            "retention_months": self.retention_months,
        }


class Inspection(OrganizationOwnedModel):
    STATUSES = [
        ("Draft", "Draft"),
        ("InProgress", "In progress"),
        ("Submitted", "Submitted"),
        ("Voided", "Voided"),
    ]

    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="inspections")
    template = models.ForeignKey(
        InspectionTemplate, on_delete=models.PROTECT, related_name="inspections"
    )
    template_snapshot = models.JSONField(default=dict)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspections_performed"
    )
    status = models.CharField(max_length=16, choices=STATUSES, default="Draft")
    started_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    acknowledgment = models.CharField(max_length=300, blank=True)
    void_reason = models.CharField(max_length=500, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inspections_voided",
    )
    replaces = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replacement"
    )

    class Meta:
        ordering = ["-started_at"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status in {"Submitted", "Voided"}:
                allowed_void = (
                    previous.status == "Submitted" and self.status == "Voided" and self.void_reason
                )
                immutable_fields = (
                    "organization_id",
                    "asset_id",
                    "template_id",
                    "template_snapshot",
                    "performed_by_id",
                    "started_at",
                    "submitted_at",
                    "acknowledgment",
                    "replaces_id",
                )
                if not allowed_void or any(
                    getattr(previous, field) != getattr(self, field) for field in immutable_fields
                ):
                    raise ValidationError(
                        "Submitted inspections are immutable; void and replace the record"
                    )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        if self.status in {"Submitted", "Voided"}:
            raise ValidationError("Submitted inspections cannot be deleted")
        return super().delete(*args, **kwargs)

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "template_id": str(self.template_id),
            "template_name": self.template_snapshot.get("name", self.template.name),
            "template_version": self.template_snapshot.get("version", self.template.version),
            "template_snapshot": (
                self.template_snapshot
                if include_financial
                else redact_financial_fields(self.template_snapshot)
            ),
            "performed_by_id": str(self.performed_by_id),
            "performed_by": self.performed_by.get_full_name() or self.performed_by.username,
            "status": self.status,
            "started_at": self.started_at,
            "submitted_at": self.submitted_at,
            "acknowledgment": self.acknowledgment,
            "void_reason": self.void_reason,
            "replaces_id": str(self.replaces_id) if self.replaces_id else None,
            "responses": [
                response.to_dict(include_financial=include_financial)
                for response in self.responses.all()
            ],
            "findings": [finding.to_dict() for finding in self.findings.all()],
        }


class InspectionResponse(OrganizationOwnedModel):
    RESULTS = [("pass", "Pass"), ("fail", "Fail"), ("na", "Not applicable"), ("value", "Value")]

    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="responses")
    question_id = models.CharField(max_length=80)
    question = models.CharField(max_length=300)
    response_type = models.CharField(max_length=30, default="pass_fail")
    answer = models.JSONField(null=True, blank=True)
    result = models.CharField(max_length=10, choices=RESULTS)
    notes = models.TextField(blank=True)
    required = models.BooleanField(default=False)
    safety_critical = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["inspection", "question_id"], name="uniq_inspection_question"
            )
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.inspection_id and self.inspection.status in {"Submitted", "Voided"}:
            raise ValidationError("Responses on submitted inspections are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        if self.inspection.status in {"Submitted", "Voided"}:
            raise ValidationError("Responses on submitted inspections cannot be deleted")
        return super().delete(*args, **kwargs)

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        finding = getattr(self, "finding", None)
        return {
            "id": str(self.pk),
            "question_id": self.question_id,
            "question": self.question,
            "response_type": self.response_type,
            "answer": (self.answer if include_financial else redact_financial_fields(self.answer)),
            "result": self.result,
            "notes": self.notes,
            "required": self.required,
            "safety_critical": self.safety_critical,
            "finding_id": str(finding.pk) if finding else None,
        }


class InspectionFinding(OrganizationOwnedModel):
    """Durable abnormal inspection outcome, distinct from its response and defect."""

    STATUSES = [
        ("Open", "Open"),
        ("Acknowledged", "Acknowledged"),
        ("Resolved", "Resolved"),
    ]
    SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("safety", "Safety")]

    inspection = models.ForeignKey(Inspection, on_delete=models.PROTECT, related_name="findings")
    response = models.OneToOneField(
        InspectionResponse, on_delete=models.PROTECT, related_name="finding"
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="inspection_findings"
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inspection_findings_reported",
    )
    status = models.CharField(max_length=16, choices=STATUSES, default="Open")
    severity = models.CharField(max_length=10, choices=SEVERITIES, default="medium")
    safety_related = models.BooleanField(default=False)
    description = models.TextField(max_length=5000)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            immutable_fields = (
                "organization_id",
                "inspection_id",
                "response_id",
                "asset_id",
                "reported_by_id",
                "severity",
                "safety_related",
                "description",
            )
            if previous and any(
                getattr(previous, field) != getattr(self, field) for field in immutable_fields
            ):
                raise ValidationError(
                    "Inspection finding evidence is immutable; update its disposition only"
                )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Inspection findings are retained with submitted inspections")

    def to_dict(self) -> dict[str, Any]:
        defect = getattr(self, "defect", None)
        return {
            "id": str(self.pk),
            "inspection_id": str(self.inspection_id),
            "response_id": str(self.response_id),
            "question_id": self.response.question_id,
            "asset_id": str(self.asset_id),
            "reported_by_id": str(self.reported_by_id),
            "reported_by": self.reported_by.get_full_name() or self.reported_by.username,
            "status": self.status,
            "severity": self.severity,
            "safety_related": self.safety_related,
            "description": self.description,
            "defect_id": str(defect.pk) if defect else None,
            "created_at": self.created_at,
        }


class Defect(OrganizationOwnedModel):
    STATUSES = [
        ("Open", "Open"),
        ("Acknowledged", "Acknowledged"),
        ("Deferred", "Deferred"),
        ("InRepair", "In repair"),
        ("Corrected", "Corrected"),
        ("Verified", "Verified"),
        ("Closed", "Closed"),
    ]
    SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("safety", "Safety")]

    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="defects")
    inspection_response = models.ForeignKey(
        InspectionResponse, on_delete=models.PROTECT, null=True, blank=True, related_name="defects"
    )
    inspection_finding = models.OneToOneField(
        InspectionFinding,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="defect",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="defects_reported"
    )
    category = models.CharField(max_length=80)
    description = models.TextField(max_length=5000)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default="medium")
    safety_related = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUSES, default="Open")
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="defects_acknowledged",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    disposition_reason = models.TextField(blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="defects_verified",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "inspection_response_id": (
                str(self.inspection_response_id) if self.inspection_response_id else None
            ),
            "inspection_finding_id": (
                str(self.inspection_finding_id) if self.inspection_finding_id else None
            ),
            "reported_by_id": str(self.reported_by_id),
            "reported_by": self.reported_by.get_full_name() or self.reported_by.username,
            "category": self.category,
            "description": self.description,
            "severity": self.severity,
            "safety_related": self.safety_related,
            "status": self.status,
            "disposition_reason": self.disposition_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MaintenanceAlert(OrganizationOwnedModel):
    STATUSES = [
        ("New", "New"),
        ("NeedsReview", "Needs review"),
        ("Acknowledged", "Acknowledged"),
        ("Converted", "Converted"),
        ("Suppressed", "Suppressed"),
        ("Resolved", "Resolved"),
        ("Dismissed", "Dismissed"),
    ]

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="maintenance_alerts"
    )
    source_type = models.CharField(max_length=80, default="telematics")
    source_id = models.CharField(max_length=100)
    status = models.CharField(max_length=16, choices=STATUSES, default="New")
    severity = models.CharField(max_length=20, default="warning")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    dedupe_key = models.CharField(max_length=255)
    rule_version = models.CharField(max_length=80, blank=True)
    occurrence_count = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    disposition_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-last_seen_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "dedupe_key"],
                condition=models.Q(status__in=["New", "NeedsReview", "Acknowledged"]),
                name="uniq_active_maintenance_alert",
            )
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "source_type": self.source_type,
            "source_id": self.source_id,
            "status": self.status,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "dedupe_key": self.dedupe_key,
            "rule_version": self.rule_version,
            "occurrence_count": self.occurrence_count,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "disposition_reason": self.disposition_reason,
        }


class MaintenanceRequest(OrganizationOwnedModel):
    STATUSES = [
        ("Submitted", "Submitted"),
        ("Triaged", "Triaged"),
        ("Approved", "Approved"),
        ("Deferred", "Deferred"),
        ("Rejected", "Rejected"),
        ("Converted", "Converted"),
        ("Closed", "Closed"),
    ]
    PRIORITIES = [("low", "Low"), ("normal", "Normal"), ("high", "High"), ("safety", "Safety")]

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="maintenance_requests"
    )
    defect = models.ForeignKey(
        Defect, on_delete=models.PROTECT, null=True, blank=True, related_name="maintenance_requests"
    )
    alert = models.ForeignKey(
        MaintenanceAlert,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="maintenance_requests",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="maintenance_requests_submitted",
    )
    status = models.CharField(max_length=16, choices=STATUSES, default="Submitted")
    priority = models.CharField(max_length=10, choices=PRIORITIES, default="normal")
    summary = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    decision_reason = models.TextField(blank=True)
    triaged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="maintenance_requests_triaged",
    )
    triaged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(defect__isnull=True) | models.Q(alert__isnull=True),
                name="maintenance_request_one_primary_source",
            ),
            models.UniqueConstraint(
                fields=["defect"],
                condition=models.Q(defect__isnull=False)
                & ~models.Q(status__in=["Rejected", "Closed"]),
                name="uniq_active_request_defect",
            ),
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "defect_id": str(self.defect_id) if self.defect_id else None,
            "alert_id": str(self.alert_id) if self.alert_id else None,
            "status": self.status,
            "priority": self.priority,
            "summary": self.summary,
            "description": self.description,
            "decision_reason": self.decision_reason,
            "submitted_by_id": str(self.submitted_by_id),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkOrder(OrganizationOwnedModel):
    STATUSES = [
        ("Draft", "Draft"),
        ("Ready", "Ready"),
        ("InProgress", "In progress"),
        ("Blocked", "Blocked"),
        ("QC", "Quality control"),
        ("Completed", "Completed"),
        ("Closed", "Closed"),
        ("Cancelled", "Cancelled"),
        ("Reopened", "Reopened"),
    ]
    PRIORITIES = MaintenanceRequest.PRIORITIES

    number = models.CharField(max_length=40)
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="work_orders")
    request = models.OneToOneField(
        MaintenanceRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order",
    )
    maintenance_plan = models.ForeignKey(
        MaintenancePlan, on_delete=models.PROTECT, null=True, blank=True, related_name="work_orders"
    )
    service_package = models.ForeignKey(
        ServicePackage, on_delete=models.PROTECT, null=True, blank=True, related_name="work_orders"
    )
    service_package_snapshot = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="work_orders_created"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_orders_assigned",
    )
    status = models.CharField(max_length=16, choices=STATUSES, default="Draft")
    priority = models.CharField(max_length=10, choices=PRIORITIES, default="normal")
    summary = models.CharField(max_length=240)
    complaint = models.TextField(blank=True)
    diagnosis = models.TextField(blank=True)
    completion_summary = models.TextField(blank=True)
    blocked_reason = models.TextField(blank=True)
    reopen_reason = models.TextField(blank=True)
    requires_qc = models.BooleanField(default=False)
    target_date = models.DateField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_orders_completed",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_orders_closed",
    )
    completion_meter = models.ForeignKey(
        "assets.MeterReading",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="completed_work_orders",
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_work_order_number_org"
            ),
            models.UniqueConstraint(
                fields=["maintenance_plan"],
                condition=models.Q(maintenance_plan__isnull=False)
                & ~models.Q(status__in=["Closed", "Cancelled"]),
                name="uniq_active_work_order_plan",
            ),
        ]

    def __str__(self) -> str:
        return self.number

    def to_dict(
        self,
        *,
        include_close_snapshots: bool = False,
        include_financial: bool = False,
    ) -> dict[str, Any]:
        assignees = self.active_assignment_dicts()
        payload: dict[str, Any] = {
            "id": str(self.pk),
            "number": self.number,
            "asset_id": str(self.asset_id),
            "asset": getattr(self.asset, "unit_number", ""),
            "request_id": str(self.request_id) if self.request_id else None,
            "maintenance_plan_id": str(self.maintenance_plan_id)
            if self.maintenance_plan_id
            else None,
            "service_package_snapshot": (
                self.service_package_snapshot
                if include_financial
                else redact_financial_fields(self.service_package_snapshot)
            ),
            "status": self.status,
            "priority": self.priority,
            "summary": self.summary,
            "complaint": self.complaint,
            "diagnosis": self.diagnosis,
            "completion_summary": self.completion_summary,
            "blocked_reason": self.blocked_reason,
            "reopen_reason": self.reopen_reason,
            "requires_qc": self.requires_qc,
            "target_date": self.target_date,
            "assigned_to_id": str(self.assigned_to_id) if self.assigned_to_id else None,
            "assigned_to": (
                self.assigned_to.get_full_name() or self.assigned_to.username
                if self.assigned_to
                else None
            ),
            "assignees": assignees,
            "ready_at": self.ready_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "closed_at": self.closed_at,
            "completion_meter_id": str(self.completion_meter_id)
            if self.completion_meter_id
            else None,
            "version": self.version,
            "tasks": [
                task.to_dict(include_financial=include_financial) for task in self.tasks.all()
            ],
        }
        if include_close_snapshots:
            payload["assignment_history"] = [
                event.to_history_dict() for event in self.assignment_events_in_sequence()
            ]
            payload["close_snapshots"] = [
                snapshot.to_dict(include_financial=include_financial)
                for snapshot in self.close_snapshots.select_related("closed_by").all()
            ]
        return payload

    def active_assignments(self) -> list["WorkOrderAssignment"]:
        """Project active people from immutable assignment events.

        A work order is locked before events are appended, and their per-work-order
        sequence gives this projection a deterministic order without editing history.
        """

        active: dict[tuple[str, str], WorkOrderAssignment] = {}
        for event in self.assignment_events_in_sequence():
            if event.action == WorkOrderAssignment.Action.ASSIGNED:
                active[event.subject_key] = event
            else:
                active.pop(event.subject_key, None)
        return sorted(
            active.values(),
            key=lambda event: (
                event.role != WorkOrderAssignment.Role.LEAD,
                event.display_name.lower(),
            ),
        )

    def active_assignment_dicts(self) -> list[dict[str, Any]]:
        """Serialize the current team, including an explicit legacy fallback.

        Migration 0007 backfills every persisted legacy lead.  The fallback keeps
        restored records and controlled data-import fixtures readable until their
        first ordinary assignment change appends real evidence; it never invents
        an assignment timestamp or event ID.
        """

        active = self.active_assignments()
        if active:
            return [assignment.to_dict() for assignment in active]
        if not self.assigned_to_id:
            return []
        user = self.assigned_to
        if user is None:
            return []
        return [
            {
                "id": None,
                "role": WorkOrderAssignment.Role.LEAD,
                "subject_type": "user",
                "user_id": str(self.assigned_to_id),
                "external_employee_id": None,
                "source_system": None,
                "source_version": None,
                "display_name": user.get_full_name() or user.username,
                "assigned_at": None,
                "legacy_projection": True,
            }
        ]

    def assignment_events_in_sequence(self) -> list["WorkOrderAssignment"]:
        """Use a prefetched event history when callers render a work-order collection."""

        cached = getattr(self, "_prefetched_objects_cache", {}).get("assignment_events")
        if cached is not None:
            return sorted(cached, key=lambda event: event.sequence)
        return list(
            self.assignment_events.select_related(
                "assigned_by", "external_employee", "local_user"
            ).order_by("sequence")
        )


class ExternalEmployeeProjection(OrganizationOwnedModel):
    """Minimal, source-owned assignment projection; never a Fleetline login account."""

    source_system = models.CharField(max_length=40)
    external_employee_id = models.CharField(max_length=160)
    external_user_id = models.CharField(max_length=160, blank=True)
    display_name = models.CharField(max_length=200)
    job_title = models.CharField(max_length=160, blank=True)
    department = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)
    source_version = models.CharField(max_length=160)
    source_updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_system", "external_employee_id"],
                name="uniq_external_employee_identity_org",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "source_system", "active"],
                name="external_employee_active_idx",
            )
        ]

    def clean(self) -> None:
        self.source_system, self.external_employee_id = normalize_external_employee_identity(
            self.source_system, self.external_employee_id
        )
        self.display_name = self.display_name.strip()
        if not self.display_name:
            raise ValidationError({"display_name": "A display name is required"})

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "source_system": self.source_system,
            "external_employee_id": self.external_employee_id,
            "display_name": self.display_name,
            "job_title": self.job_title,
            "department": self.department,
            "active": self.active,
            "source_version": self.source_version,
            "source_updated_at": self.source_updated_at,
            "synced_at": self.updated_at,
        }


class WorkOrderAssignment(ImmutableModel):
    """Append-only assignment or unassignment evidence for a work-order team."""

    class Role(models.TextChoices):
        LEAD = "lead", "Lead"
        TECHNICIAN = "technician", "Technician"

    class Action(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        UNASSIGNED = "unassigned", "Unassigned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, related_name="work_order_assignment_events"
    )
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.PROTECT, related_name="assignment_events"
    )
    local_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_assignment_events",
    )
    external_employee = models.ForeignKey(
        ExternalEmployeeProjection,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_assignment_events",
    )
    # Assignment evidence must retain the identity that was known when the
    # event was recorded. The external-person projection is deliberately
    # mutable because GatorHub owns it; resolving a history row through that
    # projection would silently rewrite past attribution after a rename.
    subject_display_name = models.CharField(max_length=200)
    subject_source_system = models.CharField(max_length=40, blank=True)
    subject_external_employee_id = models.CharField(max_length=160, blank=True)
    subject_source_version = models.CharField(max_length=160, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.TECHNICIAN)
    action = models.CharField(max_length=16, choices=Action.choices)
    sequence = models.PositiveIntegerField()
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_assignments_made",
    )
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "work_order", "sequence"],
                name="uniq_work_order_assignment_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="work_order_assignment_sequence_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(local_user__isnull=False, external_employee__isnull=True)
                    | models.Q(local_user__isnull=True, external_employee__isnull=False)
                ),
                name="work_order_assignment_single_subject",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "work_order", "local_user", "sequence"],
                name="work_order_assignment_user_idx",
            )
        ]

    @property
    def subject_key(self) -> tuple[str, str]:
        if self.local_user_id:
            return ("user", str(self.local_user_id))
        return ("external_employee", str(self.external_employee_id))

    @property
    def display_name(self) -> str:
        """The immutable assignment-time display identity, never a live profile lookup."""

        return self.subject_display_name

    def clean(self) -> None:
        super().clean()
        subjects = [subject for subject in (self.local_user, self.external_employee) if subject]
        if len(subjects) != 1:
            raise ValidationError("An assignment needs exactly one person")
        if self.work_order_id and self.organization_id != self.work_order.organization_id:
            raise ValidationError({"organization": "Work order must belong to this organization"})
        subject = subjects[0]
        if self.organization_id != subject.organization_id:
            raise ValidationError({"organization": "Assignee must belong to this organization"})
        self.subject_display_name = self.subject_display_name.strip()
        if not self.subject_display_name:
            raise ValidationError(
                {"subject_display_name": "An assignment display name is required"}
            )
        if self.local_user_id:
            if any(
                (
                    self.subject_source_system,
                    self.subject_external_employee_id,
                    self.subject_source_version,
                )
            ):
                raise ValidationError(
                    "Local-user assignments cannot contain an external identity snapshot"
                )
            return

        external_employee = self.external_employee
        if external_employee is None:
            raise ValidationError("An assignment needs exactly one person")
        source_system, external_employee_id = normalize_external_employee_identity(
            self.subject_source_system, self.subject_external_employee_id
        )
        if not self.subject_source_version.strip():
            raise ValidationError(
                {"subject_source_version": "An external source version is required"}
            )
        self.subject_source_system = source_system
        self.subject_external_employee_id = external_employee_id
        if (
            source_system != external_employee.source_system
            or external_employee_id != external_employee.external_employee_id
        ):
            raise ValidationError(
                "External assignment snapshot identity must match its external employee"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "role": self.role,
            "subject_type": "user" if self.local_user_id else "external_employee",
            "user_id": str(self.local_user_id) if self.local_user_id else None,
            "external_employee_id": (
                self.subject_external_employee_id if self.external_employee_id else None
            ),
            "source_system": self.subject_source_system if self.external_employee_id else None,
            "source_version": self.subject_source_version if self.external_employee_id else None,
            "display_name": self.display_name,
            "assigned_at": self.created_at,
        }

    def to_history_dict(self) -> dict[str, Any]:
        """Expose immutable assignment evidence only where work-order history is visible."""

        return {
            **self.to_dict(),
            "action": self.action,
            "sequence": self.sequence,
            "reason": self.reason,
            "assigned_by_id": str(self.assigned_by_id) if self.assigned_by_id else None,
            "assigned_by": (
                self.assigned_by.get_full_name() or self.assigned_by.username
                if self.assigned_by
                else None
            ),
        }


class WorkOrderCloseSnapshot(ImmutableModel):
    """Append-only evidence of exactly what was recorded each time a work order closed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "core.Organization", on_delete=models.PROTECT, related_name="work_order_close_snapshots"
    )
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.PROTECT, related_name="close_snapshots"
    )
    sequence = models.PositiveIntegerField()
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_close_snapshots",
    )
    closed_at = models.DateTimeField()
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "work_order", "sequence"],
                name="uniq_work_order_close_snapshot_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="work_order_close_snapshot_sequence_positive",
            ),
        ]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        return {
            "id": str(self.pk),
            "sequence": self.sequence,
            "closed_at": self.closed_at,
            "closed_by_id": str(self.closed_by_id) if self.closed_by_id else None,
            "closed_by": (
                self.closed_by.get_full_name() or self.closed_by.username
                if self.closed_by
                else None
            ),
            "snapshot": (
                self.snapshot if include_financial else redact_financial_fields(self.snapshot)
            ),
            "created_at": self.created_at,
        }


class WorkOrderTask(OrganizationOwnedModel):
    STATUSES = [
        ("Pending", "Pending"),
        ("InProgress", "In progress"),
        ("Completed", "Completed"),
        ("Skipped", "Skipped"),
    ]

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=240)
    instructions = models.TextField(blank=True)
    sequence = models.PositiveSmallIntegerField(default=0)
    required = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=STATUSES, default="Pending")
    notes = models.TextField(blank=True)
    measurement = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_tasks_completed",
    )
    component = models.ForeignKey(
        "assets.Component",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_order_tasks",
    )

    class Meta:
        ordering = ["sequence", "created_at"]

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        component = self.component
        return {
            "id": str(self.pk),
            "title": self.title,
            "instructions": self.instructions,
            "sequence": self.sequence,
            "required": self.required,
            "status": self.status,
            "notes": self.notes,
            "measurement": (
                self.measurement if include_financial else redact_financial_fields(self.measurement)
            ),
            "completed_at": self.completed_at,
            "completed_by_id": str(self.completed_by_id) if self.completed_by_id else None,
            "component_id": str(self.component_id) if self.component_id else None,
            "component": (
                {
                    "kind": component.kind,
                    "kind_label": component.get_kind_display(),
                    "serial_number": component.serial_number,
                }
                if component
                else None
            ),
        }


class LaborEntry(OrganizationOwnedModel):
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.PROTECT, related_name="labor_entries"
    )
    technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="labor_entries"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    minutes = models.PositiveIntegerField()
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(blank=True)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="correction"
    )

    class Meta:
        ordering = ["started_at", "created_at"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Labor entries are append-only; create a correcting entry")
        self.cost = (Decimal(self.minutes) / Decimal(60) * self.hourly_rate).quantize(
            Decimal("0.01")
        )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Labor entries cannot be deleted")

    def to_dict(self, *, include_financial: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(self.pk),
            "technician_id": str(self.technician_id),
            "technician": self.technician.get_full_name() or self.technician.username,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "minutes": self.minutes,
            "note": self.note,
            "corrects_id": str(self.corrects_id) if self.corrects_id else None,
        }
        if include_financial:
            payload["hourly_rate"] = format(self.hourly_rate, ".4f")
            payload["cost"] = format(self.cost, ".4f")
        return payload
