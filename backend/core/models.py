from __future__ import annotations

import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.base import ModelBase


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=80, unique=True)
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Location(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="locations"
    )
    name = models.CharField(max_length=160)
    code = models.CharField(max_length=40)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_location_code_org")
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Role(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="roles")
    slug = models.SlugField(max_length=40)
    name = models.CharField(max_length=80)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="uniq_role_slug_org")
        ]

    def __str__(self) -> str:
        return self.name


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, null=True, blank=True, related_name="users"
    )
    default_location = models.ForeignKey(
        Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="default_users"
    )
    roles = models.ManyToManyField(Role, blank=True, related_name="users")
    mfa_secret = models.CharField(max_length=64, blank=True)
    offline_access_revoked_at = models.DateTimeField(null=True, blank=True)

    @property
    def role_slugs(self) -> set[str]:
        return set(self.roles.values_list("slug", flat=True))


class OrganizationOwnedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ImmutableModel(models.Model):
    """Application guard; PostgreSQL triggers provide the final immutable boundary."""

    class Meta:
        abstract = True

    def save(
        self,
        *args: Any,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if self.pk and type(self)._default_manager.filter(pk=self.pk).exists():
            raise ValidationError(f"{type(self).__name__} records are append-only")
        super().save(
            *args,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        raise ValidationError(f"{type(self).__name__} records cannot be deleted")


class AuditEvent(ImmutableModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="audit_events"
    )
    actor = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="audit_events"
    )
    action = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=80)
    previous_state = models.CharField(max_length=50, blank=True)
    new_state = models.CharField(max_length=50, blank=True)
    context = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=40, default="web")
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["organization", "resource_type", "resource_id"])]


class IdempotencyRecord(OrganizationOwnedModel):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="idempotency_records")
    key = models.CharField(max_length=80)
    route = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=20, default="processing")
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user", "route", "key"], name="uniq_idempotency_scope"
            )
        ]


class LoginAttemptThrottle(models.Model):
    class Dimension(models.TextChoices):
        ACCOUNT = "account", "Account"
        CLIENT = "client", "Client"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dimension = models.CharField(max_length=16, choices=Dimension.choices)
    key_hash = models.CharField(max_length=64)
    failure_count = models.PositiveSmallIntegerField(default=0)
    window_started_at = models.DateTimeField()
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dimension", "key_hash"], name="uniq_login_throttle_dimension_key"
            )
        ]
        indexes = [models.Index(fields=["updated_at"], name="login_throttle_updated_idx")]


class ApiToken(OrganizationOwnedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=12)
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="api_token_expiry_after_creation",
            )
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "name": self.name,
            "prefix": self.prefix,
            "scopes": self.scopes,
            "user_id": str(self.user_id),
            "user": self.user.get_full_name() or self.user.username,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


def attachment_path(instance: "Attachment", filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:12]
    return f"attachments/{instance.organization_id}/{uuid.uuid4().hex}{suffix}"


class Attachment(OrganizationOwnedModel):
    class Sensitivity(models.TextChoices):
        OPERATIONAL = "operational", "Operational"
        FINANCIAL = "financial", "Financial"

    uploader = models.ForeignKey(User, on_delete=models.PROTECT, related_name="attachments")
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=80)
    sensitivity = models.CharField(
        choices=Sensitivity.choices,
        default=Sensitivity.OPERATIONAL,
        max_length=16,
    )
    document_key = models.UUIDField(default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=80, blank=True)
    title = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1)
    supersedes = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    file = models.FileField(upload_to=attachment_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)

    class Meta:
        indexes = [models.Index(fields=["organization", "resource_type", "resource_id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_key", "version"],
                name="uniq_attachment_document_version",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="attachment_version_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(sensitivity__in=("operational", "financial")),
                name="attachment_sensitivity_valid",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not self.supersedes_id:
            if self.version != 1:
                raise ValidationError({"version": "An original attachment must be version 1"})
            return
        previous = type(self)._default_manager.filter(pk=self.supersedes_id).first()
        if not previous:
            return
        errors: dict[str, str] = {}
        if previous.organization_id != self.organization_id:
            errors["supersedes"] = "The prior attachment must belong to the same organization"
        if previous.resource_type != self.resource_type or previous.resource_id != self.resource_id:
            errors["supersedes"] = "The prior attachment must belong to the same resource"
        if previous.document_key != self.document_key:
            errors["document_key"] = "A new version must keep the prior document key"
        if self.version != previous.version + 1:
            errors["version"] = "A new version must follow the prior version"
        if errors:
            raise ValidationError(errors)


class Document(OrganizationOwnedModel):
    """Technical-document metadata for one immutable attachment revision."""

    class Status(models.TextChoices):
        QUARANTINED = "quarantined", "Quarantined"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        INDEXED = "indexed", "Indexed"
        OCR_UNAVAILABLE = "ocr_unavailable", "OCR unavailable"
        NEEDS_REVIEW = "needs_review", "Needs review"
        FAILED = "failed", "Failed"

    attachment = models.OneToOneField(
        Attachment, on_delete=models.PROTECT, related_name="technical_document"
    )
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="documents")
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=80)
    manufacturer = models.CharField(max_length=120, blank=True)
    model = models.CharField(max_length=120, blank=True)
    engine_type = models.CharField(max_length=120, blank=True)
    revision = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=255, blank=True)
    license = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUARANTINED)
    processing_detail = models.CharField(max_length=1000, blank=True)
    security_review_reference = models.CharField(max_length=255, blank=True)
    security_review_note = models.CharField(max_length=1000, blank=True)
    security_reviewed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_documents",
    )
    security_reviewed_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    supersedes = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )

    class Meta:
        indexes = [models.Index(fields=["organization", "status"])]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.attachment_id and self.attachment.organization_id != self.organization_id:
            errors["attachment"] = "The attachment must belong to the same organization"
        if self.asset_id and self.asset.organization_id != self.organization_id:
            errors["asset"] = "The asset must belong to the same organization"
        reviewer = self.security_reviewed_by
        if reviewer is not None and reviewer.organization_id != self.organization_id:
            errors["security_reviewed_by"] = "The reviewer must belong to the same organization"
        if (
            self.attachment_id
            and self.asset_id
            and (
                self.attachment.resource_type.casefold() != "asset"
                or self.attachment.resource_id != str(self.asset_id)
            )
        ):
            errors["attachment"] = "Technical documents must retain an asset attachment target"
        if self.supersedes_id:
            previous = type(self)._default_manager.filter(pk=self.supersedes_id).first()
            if previous:
                if previous.organization_id != self.organization_id:
                    errors["supersedes"] = "The prior document must belong to the same organization"
                if previous.asset_id != self.asset_id:
                    errors["supersedes"] = "A replacement must retain the same source asset"
                if self.attachment.document_key != previous.attachment.document_key:
                    errors["supersedes"] = "A replacement must retain the attachment document key"
                if self.attachment.version != previous.attachment.version + 1:
                    errors["supersedes"] = "A replacement must follow the prior attachment version"
        if errors:
            raise ValidationError(errors)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Technical documents are retained as immutable source history")


class DocumentApplicability(OrganizationOwnedModel):
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="applicability")
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_applicability",
    )
    asset_type = models.ForeignKey(
        "assets.AssetType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_applicability",
    )
    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    engine_type = models.CharField(max_length=120, blank=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "asset"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(asset__isnull=False)
                    | Q(asset_type__isnull=False)
                    | ~Q(make="")
                    | ~Q(model="")
                    | ~Q(engine_type="")
                ),
                name="document_applicability_target",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.document_id and self.document.organization_id != self.organization_id:
            errors["document"] = "The document must belong to the same organization"
        asset = self.asset
        if asset is not None and asset.organization_id != self.organization_id:
            errors["asset"] = "The asset must belong to the same organization"
        asset_type = self.asset_type
        if asset_type is not None and asset_type.organization_id != self.organization_id:
            errors["asset_type"] = "The asset type must belong to the same organization"
        if not any((self.asset_id, self.asset_type_id, self.make, self.model, self.engine_type)):
            errors["__all__"] = "At least one applicability target is required"
        if errors:
            raise ValidationError(errors)


class DocumentPage(ImmutableModel):
    class ExtractionMethod(models.TextChoices):
        EMBEDDED = "embedded", "Embedded PDF text"
        OCR = "ocr", "OCR"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="pages")
    page_number = models.PositiveIntegerField()
    text = models.TextField()
    extraction_method = models.CharField(max_length=16, choices=ExtractionMethod.choices)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    search_vector = SearchVectorField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["page_number"]
        indexes = [
            models.Index(fields=["organization", "document", "page_number"]),
            GinIndex(fields=["search_vector"], name="document_page_search_gin"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["document", "page_number"], name="uniq_document_page"),
            models.CheckConstraint(condition=Q(page_number__gte=1), name="document_page_positive"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.document_id and self.document.organization_id != self.organization_id:
            raise ValidationError({"document": "The document must belong to the same organization"})


class Comment(OrganizationOwnedModel):
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="comments")
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=80)
    body = models.TextField(max_length=5000)


class Notification(OrganizationOwnedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=180)
    body = models.TextField(max_length=1000)
    resource_type = models.CharField(max_length=80, blank=True)
    resource_id = models.CharField(max_length=80, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)


class OutboxEvent(OrganizationOwnedModel):
    event_type = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["processed_at", "available_at"])]


class WorkerHeartbeat(models.Model):
    name = models.CharField(max_length=80, primary_key=True)
    seen_at = models.DateTimeField()
    details = models.JSONField(default=dict, blank=True)


class SyncConflict(OrganizationOwnedModel):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sync_conflicts")
    operation_id = models.UUIDField()
    operation_type = models.CharField(max_length=80)
    message = models.CharField(max_length=500)
    client_payload = models.JSONField(default=dict)
    server_payload = models.JSONField(default=dict)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "operation_id"], name="uniq_sync_conflict_operation"
            )
        ]


class WebhookSubscription(OrganizationOwnedModel):
    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500)
    signing_secret = models.CharField(max_length=128)
    event_types = models.JSONField(default=list)
    active = models.BooleanField(default=True)


class WebhookDelivery(OrganizationOwnedModel):
    subscription = models.ForeignKey(
        WebhookSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    outbox_event = models.ForeignKey(
        OutboxEvent, on_delete=models.CASCADE, related_name="deliveries"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, default="pending")
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    next_attempt_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "outbox_event"], name="uniq_webhook_event_delivery"
            )
        ]
