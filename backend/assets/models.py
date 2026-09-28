from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any

from core.models import ImmutableModel, OrganizationOwnedModel
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def normalize_external_identity(source_system: object, external_id: object) -> tuple[str, str]:
    source = str(source_system).strip().lower()
    identifier = str(external_id).strip()
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,39}", source):
        raise ValidationError(
            {"source_system": "Use 1-40 lowercase letters, numbers, dots, underscores, or hyphens"}
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", identifier):
        raise ValidationError(
            {"external_id": "Use 1-160 letters, numbers, dots, underscores, colons, or hyphens"}
        )
    return source, identifier


class AssetType(OrganizationOwnedModel):
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=40, default="vehicle")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_asset_type_name_org"
            )
        ]

    def __str__(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, object]:
        return {"id": str(self.pk), "name": self.name, "category": self.category}


class Asset(OrganizationOwnedModel):
    class Status(models.TextChoices):
        AVAILABLE = "Available", "Available"
        RESTRICTED = "Restricted", "Restricted"
        OUT_OF_SERVICE = "OutOfService", "Out of service"
        RETIRED = "Retired", "Retired"

    asset_type = models.ForeignKey(AssetType, on_delete=models.PROTECT, related_name="assets")
    home_location = models.ForeignKey(
        "core.Location", on_delete=models.PROTECT, null=True, blank=True, related_name="assets"
    )
    assigned_driver = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_assets",
    )
    unit_number = models.CharField(max_length=60)
    vin = models.CharField(max_length=32, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    ownership = models.CharField(max_length=120, blank=True)
    specs = models.JSONField(default=dict, blank=True)
    source_system = models.CharField(max_length=40, blank=True)
    external_id = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    status_changed_at = models.DateTimeField(default=timezone.now)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["unit_number"]
        indexes = [models.Index(fields=["organization", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "unit_number"], name="uniq_asset_unit_org"
            ),
            models.UniqueConstraint(
                fields=["organization", "vin"], condition=~Q(vin=""), name="uniq_asset_vin_org"
            ),
            models.UniqueConstraint(
                fields=["organization", "source_system", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_asset_external_identity",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_system="", external_id="")
                    | (~Q(source_system="") & ~Q(external_id=""))
                ),
                name="asset_external_identity_pair",
            ),
            models.CheckConstraint(condition=~Q(unit_number=""), name="asset_unit_not_empty"),
        ]

    def __str__(self) -> str:
        return self.unit_number

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.unit_number = self.unit_number.strip().upper()
        self.vin = self.vin.strip().upper()
        self.serial_number = self.serial_number.strip()
        self.source_system = self.source_system.strip().lower()
        self.external_id = self.external_id.strip()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if bool(self.source_system) != bool(self.external_id):
            raise ValidationError(
                {"external_id": "source_system and external_id must be supplied together"}
            )
        if self.source_system:
            self.source_system, self.external_id = normalize_external_identity(
                self.source_system, self.external_id
            )

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Assets are archived or retired, not deleted")

    def to_dict(self, *, include_meters: bool = True) -> dict[str, object]:
        asset_type = self.asset_type
        location = self.home_location
        driver = self.assigned_driver
        payload: dict[str, object] = {
            "id": str(self.pk),
            "organization_id": str(self.organization_id),
            "unit_number": self.unit_number,
            "vin": self.vin,
            "serial_number": self.serial_number,
            "year": self.year,
            "make": self.make,
            "model": self.model,
            "ownership": self.ownership,
            "specs": self.specs,
            "source_system": self.source_system,
            "external_id": self.external_id,
            "status": self.status,
            "status_changed_at": self.status_changed_at,
            "archived_at": self.archived_at,
            "asset_type_id": str(self.asset_type_id),
            "asset_type": asset_type.to_dict(),
            "home_location_id": str(self.home_location_id) if self.home_location_id else None,
            "home_location": (
                {"id": str(location.pk), "code": location.code, "name": location.name}
                if location
                else None
            ),
            "assigned_driver_id": str(self.assigned_driver_id) if self.assigned_driver_id else None,
            "assigned_driver": (
                {
                    "id": str(driver.pk),
                    "username": driver.username,
                    "name": driver.get_full_name() or driver.username,
                }
                if driver
                else None
            ),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_meters:
            payload["meters"] = [meter.to_dict() for meter in self.meters.filter(active=True)]
        return payload


class AssetStatusEvent(ImmutableModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="status_events")
    actor = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="asset_status_events",
    )
    previous_status = models.CharField(max_length=20, blank=True, choices=Asset.Status.choices)
    new_status = models.CharField(max_length=20, choices=Asset.Status.choices)
    reason = models.TextField(max_length=1000)
    source = models.CharField(max_length=40, default="web")
    classification = models.CharField(
        max_length=20,
        choices=[("company", "Company"), ("regulatory", "Regulatory")],
        default="company",
    )
    context = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["organization", "asset", "occurred_at"])]

    def to_dict(self) -> dict[str, object]:
        actor = self.actor
        return {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "reason": self.reason,
            "source": self.source,
            "classification": self.classification,
            "context": self.context,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "actor": actor.username if actor else None,
            "occurred_at": self.occurred_at,
        }


class Meter(OrganizationOwnedModel):
    class Kind(models.TextChoices):
        ODOMETER = "odometer", "Odometer"
        ENGINE_HOURS = "engine_hours", "Engine hours"
        OTHER = "other", "Other"

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="meters")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    unit = models.CharField(max_length=20)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "asset", "name"], name="uniq_meter_name_asset"
            )
        ]

    def __str__(self) -> str:
        return f"{self.asset.unit_number} {self.name}"

    def clean(self) -> None:
        super().clean()
        if self.asset_id and self.organization_id != self.asset.organization_id:
            raise ValidationError(
                {"organization": "Meter and asset must belong to the same organization"}
            )
        valid_units: dict[str, set[str]] = {
            self.Kind.ODOMETER: {"mi", "km"},
            self.Kind.ENGINE_HOURS: {"h"},
        }
        if self.kind in valid_units and self.unit not in valid_units[self.kind]:
            raise ValidationError({"unit": f"Invalid unit for {self.get_kind_display()}"})

    @property
    def current_reading(self) -> MeterReading | None:
        return (
            self.readings.filter(quality=MeterReading.Quality.ACCEPTED, correction__isnull=True)
            .order_by("-observed_at", "-received_at")
            .first()
        )

    @property
    def current_value(self) -> Decimal | None:
        reading = self.current_reading
        return reading.value if reading else None

    def to_dict(self, *, include_readings: bool = False) -> dict[str, object]:
        current = self.current_reading
        payload: dict[str, object] = {
            "id": str(self.pk),
            "asset_id": str(self.asset_id),
            "name": self.name,
            "kind": self.kind,
            "unit": self.unit,
            "active": self.active,
            "current_reading": current.to_dict() if current else None,
            "current_value": str(current.value) if current else None,
            "current_observed_at": current.observed_at if current else None,
        }
        if include_readings:
            payload["readings"] = [reading.to_dict() for reading in self.readings.all()]
        return payload


class MeterReading(ImmutableModel):
    class Quality(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        SUSPECT = "suspect", "Suspect"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="readings")
    value = models.DecimalField(max_digits=16, decimal_places=3)
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=40)
    quality = models.CharField(max_length=20, choices=Quality.choices, default=Quality.ACCEPTED)
    provenance = models.JSONField(default=dict, blank=True)
    external_id = models.CharField(max_length=160, blank=True)
    corrects = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="correction"
    )
    reason = models.TextField(max_length=1000, blank=True)
    created_by = models.ForeignKey(
        "core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="meter_readings"
    )

    class Meta:
        ordering = ["-observed_at", "-received_at"]
        indexes = [models.Index(fields=["organization", "meter", "observed_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(value__gte=0), name="meter_reading_nonnegative"),
            models.UniqueConstraint(
                fields=["organization", "source", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_meter_external_reading",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.meter_id and self.organization_id != self.meter.organization_id:
            raise ValidationError(
                {"organization": "Reading and meter must belong to the same organization"}
            )
        if self.corrects_id and self.corrects and self.corrects.meter_id != self.meter_id:
            raise ValidationError(
                {"corrects": "A correction must reference a reading from the same meter"}
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.pk),
            "meter_id": str(self.meter_id),
            "value": format(self.value, ".3f"),
            "unit": self.meter.unit,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "source": self.source,
            "quality": self.quality,
            "provenance": self.provenance,
            "external_id": self.external_id,
            "corrects_id": str(self.corrects_id) if self.corrects_id else None,
            "reason": self.reason,
            "created_by_id": str(self.created_by_id) if self.created_by_id else None,
        }


class Component(OrganizationOwnedModel):
    """A serviceable major part tracked by serial number, independent of any asset."""

    class Kind(models.TextChoices):
        ENGINE = "engine", "Engine"
        TRANSMISSION = "transmission", "Transmission"
        REEFER_UNIT = "reefer_unit", "Reefer unit"
        APU = "apu", "APU"
        AXLE = "axle", "Axle"
        AFTERTREATMENT = "aftertreatment", "Aftertreatment"
        OTHER = "other", "Other"

    kind = models.CharField(max_length=30, choices=Kind.choices)
    serial_number = models.CharField(max_length=100)
    manufacturer = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["kind", "serial_number"]
        indexes = [models.Index(fields=["organization", "kind"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "kind", "serial_number"],
                name="uniq_component_kind_serial_org",
            ),
            models.CheckConstraint(
                condition=~Q(serial_number=""), name="component_serial_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.serial_number}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.serial_number = self.serial_number.strip().upper()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Components are retired, not deleted")

    @property
    def open_installation(self) -> ComponentInstallation | None:
        return next((row for row in self.installations.all() if row.removed_at is None), None)

    def to_dict(self) -> dict[str, Any]:
        open_row = self.open_installation
        return {
            "id": str(self.pk),
            "kind": self.kind,
            "kind_label": self.get_kind_display(),
            "serial_number": self.serial_number,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "installed_on": (
                {
                    "installation_id": str(open_row.pk),
                    "asset_id": str(open_row.asset_id),
                    "unit_number": open_row.asset.unit_number,
                    "installed_at": open_row.installed_at,
                }
                if open_row is not None
                else None
            ),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ComponentInstallation(OrganizationOwnedModel):
    """One period a component spent on one asset. Write-once; the removal half fills once."""

    component = models.ForeignKey(Component, on_delete=models.PROTECT, related_name="installations")
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="component_installations"
    )
    installed_at = models.DateTimeField()
    installed_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_installs",
    )
    installed_work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_installs",
    )
    installed_meters = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=40, default="web")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        "core.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_removals",
    )
    removed_work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="component_removals",
    )
    removed_meters = models.JSONField(default=list, blank=True)
    removal_reason = models.TextField(max_length=1000, blank=True)

    class Meta:
        # The tie-break is load-bearing: the legacy backfill gives every equipment
        # section of one asset an identical installed_at.
        ordering = ["-installed_at", "-created_at", "id"]
        indexes = [
            models.Index(fields=["organization", "asset", "installed_at"]),
            models.Index(fields=["organization", "component", "installed_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(removed_at__isnull=True) | Q(removed_at__gt=models.F("installed_at")),
                name="component_installation_positive_period",
            ),
            models.UniqueConstraint(
                fields=["component"],
                condition=Q(removed_at__isnull=True),
                name="uniq_open_component_installation",
            ),
            models.CheckConstraint(
                condition=(
                    Q(removed_at__isnull=True, removal_reason="")
                    | (Q(removed_at__isnull=False) & ~Q(removal_reason=""))
                ),
                name="component_installation_removal_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.component} on {self.asset.unit_number}"

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Component installations are append-only")

    def clean(self) -> None:
        super().clean()
        now = timezone.now()
        if self.installed_at and self.installed_at > now:
            raise ValidationError({"installed_at": "Installed at cannot be in the future"})
        if self.removed_at and self.removed_at > now:
            raise ValidationError({"removed_at": "Removed at cannot be in the future"})
        if self.component_id and self.organization_id != self.component.organization_id:
            raise ValidationError({"component": "Must belong to the same organization"})
        if self.asset_id and self.organization_id != self.asset.organization_id:
            raise ValidationError({"asset": "Must belong to the same organization"})
        for name in ("installed_work_order", "removed_work_order"):
            work_order = getattr(self, name, None)
            if work_order is None:
                continue
            if work_order.organization_id != self.organization_id:
                raise ValidationError({name: "Must belong to the same organization"})
            if work_order.asset_id != self.asset_id:
                raise ValidationError({name: "Work order must be on the same asset"})
        if self.component_id and self.installed_at:
            # Period-overlap check, copied from DeviceAssetAssociation.clean.
            overlaps = type(self).objects.filter(component_id=self.component_id).exclude(pk=self.pk)
            overlaps = overlaps.filter(
                Q(removed_at__isnull=True) | Q(removed_at__gt=self.installed_at)
            )
            if self.removed_at:
                overlaps = overlaps.filter(installed_at__lt=self.removed_at)
            if overlaps.exists():
                raise ValidationError({"installed_at": "Installation periods cannot overlap"})

    def to_dict(self) -> dict[str, Any]:
        installed_by = self.installed_by
        removed_by = self.removed_by
        installed_wo = self.installed_work_order
        removed_wo = self.removed_work_order
        return {
            "id": str(self.pk),
            "component_id": str(self.component_id),
            "component": {
                "kind": self.component.kind,
                "kind_label": self.component.get_kind_display(),
                "serial_number": self.component.serial_number,
                "manufacturer": self.component.manufacturer,
                "model": self.component.model,
            },
            "asset_id": str(self.asset_id),
            "asset": {"unit_number": self.asset.unit_number},
            "installed_at": self.installed_at,
            "installed_by_id": str(self.installed_by_id) if self.installed_by_id else None,
            "installed_by": installed_by.get_full_name() if installed_by else "",
            "installed_work_order_id": (
                str(self.installed_work_order_id) if self.installed_work_order_id else None
            ),
            "installed_work_order_number": (installed_wo.number if installed_wo else ""),
            "installed_meters": self.installed_meters,
            "source": self.source,
            "removed_at": self.removed_at,
            "removed_by_id": str(self.removed_by_id) if self.removed_by_id else None,
            "removed_by": removed_by.get_full_name() if removed_by else "",
            "removed_work_order_id": (
                str(self.removed_work_order_id) if self.removed_work_order_id else None
            ),
            "removed_work_order_number": (removed_wo.number if removed_wo else ""),
            "removed_meters": self.removed_meters,
            "removal_reason": self.removal_reason,
        }
