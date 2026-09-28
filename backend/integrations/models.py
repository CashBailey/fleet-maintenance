from __future__ import annotations

import uuid

from core.models import ImmutableModel, Organization, OrganizationOwnedModel
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Device(OrganizationOwnedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"

    name = models.CharField(max_length=120)
    provider = models.CharField(max_length=40, default="autopi")
    vendor = models.CharField(max_length=80, default="AutoPi")
    model = models.CharField(max_length=120, blank=True)
    serial_number = models.CharField(max_length=160)
    external_id = models.CharField(max_length=160)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    token_prefix = models.CharField(max_length=12)
    token_hash = models.CharField(max_length=64, unique=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    message_count = models.PositiveBigIntegerField(default=0)
    duplicate_count = models.PositiveBigIntegerField(default=0)
    rejected_count = models.PositiveBigIntegerField(default=0)
    quarantined_count = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "provider", "serial_number"],
                name="uniq_device_provider_serial_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "external_id"], name="uniq_device_external_id_org"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class DeviceAssetAssociation(OrganizationOwnedModel):
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="associations")
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="device_associations"
    )
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(
        "core.User", on_delete=models.PROTECT, related_name="device_associations"
    )

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gt=models.F("effective_from")),
                name="device_association_positive_period",
            ),
            models.UniqueConstraint(
                fields=["device"],
                condition=Q(effective_to__isnull=True),
                name="uniq_open_device_association",
            ),
        ]

    def clean(self) -> None:
        if self.device_id and self.organization_id != self.device.organization_id:
            raise ValidationError("Device and association must belong to the same organization")
        if self.asset_id and self.organization_id != self.asset.organization_id:
            raise ValidationError("Asset and association must belong to the same organization")
        if self.device_id and self.effective_from:
            overlaps = type(self).objects.filter(device_id=self.device_id).exclude(pk=self.pk)
            overlaps = overlaps.filter(
                Q(effective_to__isnull=True) | Q(effective_to__gt=self.effective_from)
            )
            if self.effective_to:
                overlaps = overlaps.filter(effective_from__lt=self.effective_to)
            if overlaps.exists():
                raise ValidationError("Device association periods cannot overlap")


class TelematicsMessage(ImmutableModel):
    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        QUARANTINED = "quarantined", "Quarantined"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="telematics_messages"
    )
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="messages")
    source = models.CharField(max_length=40)
    schema_version = models.CharField(max_length=20)
    message_id = models.CharField(max_length=160, blank=True)
    canonical_hash = models.CharField(max_length=64)
    message_type = models.CharField(max_length=40)
    observed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField()
    sequence = models.BigIntegerField(null=True, blank=True)
    raw_payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices)
    rejection_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "message_id"],
                condition=~Q(message_id="") & Q(status="accepted"),
                name="uniq_device_message_id",
            ),
            models.UniqueConstraint(
                fields=["device", "canonical_hash"],
                condition=Q(status="accepted"),
                name="uniq_device_canonical_message",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "received_at"]),
            models.Index(fields=["device", "observed_at"]),
        ]


class NormalizedTelematicsEvent(ImmutableModel):
    class Kind(models.TextChoices):
        METER = "meter", "Meter"
        DIAGNOSTIC = "diagnostic", "Diagnostic"
        STATUS = "status", "Status"

    class Quality(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        SUSPECT = "suspect", "Suspect"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="normalized_telematics_events"
    )
    message = models.ForeignKey(
        TelematicsMessage, on_delete=models.PROTECT, related_name="normalized_events"
    )
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="normalized_events")
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="telematics_events",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    signal = models.CharField(max_length=80)
    value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    observed_at = models.DateTimeField()
    quality = models.CharField(max_length=20, choices=Quality.choices)
    reason = models.CharField(max_length=500, blank=True)
    normalized_payload = models.JSONField(default=dict)
    meter_reading = models.ForeignKey(
        "assets.MeterReading",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="telematics_events",
    )

    class Meta:
        ordering = ["-observed_at", "signal"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "kind", "signal"], name="uniq_normalized_signal_message"
            )
        ]
        indexes = [models.Index(fields=["organization", "quality", "observed_at"])]
