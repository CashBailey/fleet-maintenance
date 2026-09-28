from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.exceptions import DomainError
from core.models import Location, Organization, User
from core.permissions import has_permission
from core.services import audit, emit
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

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


def _meter_snapshot(meter: Meter, reading: MeterReading) -> dict[str, Any]:
    """Freeze a reading by value. Later corrections must never rewrite a snapshot."""
    return {
        "meter_id": str(meter.pk),
        "meter_name": meter.name,
        "kind": meter.kind,
        "unit": meter.unit,
        "reading_id": str(reading.pk),
        "value": str(reading.value),
        "observed_at": reading.observed_at.isoformat(),
        "source": reading.source,
        "quality": reading.quality,
    }


def _meter_snapshots_as_of(asset: Asset, at: datetime) -> list[dict[str, Any]]:
    """Latest accepted, uncorrected reading per active cumulative meter, at or before `at`.

    Best-effort by design: a meter with no qualifying reading is omitted, never
    invented, and never blocks a technician. Retirement stays strict and keeps its
    own required-readings check.
    """
    snapshots: list[dict[str, Any]] = []
    meters = Meter.objects.filter(
        organization=asset.organization,
        asset=asset,
        active=True,
        kind__in=(Meter.Kind.ODOMETER, Meter.Kind.ENGINE_HOURS),
    ).order_by("kind", "name")
    for meter in meters:
        reading = (
            meter.readings.filter(
                quality=MeterReading.Quality.ACCEPTED,
                correction__isnull=True,
                observed_at__lte=at,
            )
            .order_by("-observed_at", "-received_at")
            .first()
        )
        if reading is not None:
            snapshots.append(_meter_snapshot(meter, reading))
    return snapshots


def _domain_validation(exc: ValidationError) -> DomainError:
    details = getattr(exc, "message_dict", None) or getattr(exc, "messages", None)
    return DomainError("Input validation failed", code="validation_error", details=details)


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainError("A valid meter value is required", code="invalid_meter_value") from exc
    if not parsed.is_finite() or parsed < 0:
        raise DomainError("Meter value must be a non-negative number", code="invalid_meter_value")
    return parsed


def create_asset(
    *,
    organization: Organization,
    actor: User,
    asset_type: AssetType,
    unit_number: str,
    home_location: Location | None = None,
    assigned_driver: User | None = None,
    status: str = Asset.Status.AVAILABLE,
    status_reason: str = "Initial asset status",
    status_source: str = "web",
    **fields: Any,
) -> Asset:
    if asset_type.organization_id != organization.pk:
        raise DomainError("Asset type is outside this organization", code="invalid_asset_type")
    if home_location and home_location.organization_id != organization.pk:
        raise DomainError("Location is outside this organization", code="invalid_location")
    if assigned_driver and assigned_driver.organization_id != organization.pk:
        raise DomainError("Driver is outside this organization", code="invalid_assigned_driver")
    if status not in Asset.Status.values or status == Asset.Status.RETIRED:
        raise DomainError("Invalid initial asset status", code="invalid_asset_status")
    asset = Asset(
        organization=organization,
        asset_type=asset_type,
        unit_number=unit_number,
        home_location=home_location,
        assigned_driver=assigned_driver,
        status=status,
        **fields,
    )
    try:
        with transaction.atomic():
            asset.full_clean()
            asset.save()
            event = AssetStatusEvent.objects.create(
                id=uuid.uuid5(asset.pk, "initial-status"),
                organization=organization,
                asset=asset,
                actor=actor,
                previous_status="",
                new_status=status,
                reason=status_reason.strip() or "Initial asset status",
                source=status_source,
            )
            audit(
                organization=organization,
                actor=actor,
                action="asset.created",
                resource=asset,
                new_state=status,
                context={"unit_number": asset.unit_number, "status_event_id": str(event.pk)},
            )
            emit(organization=organization, event_type="asset.created", resource=asset)
    except ValidationError as exc:
        raise _domain_validation(exc) from exc
    except IntegrityError as exc:
        raise DomainError(
            "Unit number or VIN already exists in this organization",
            code="duplicate_asset",
            status=409,
        ) from exc
    return asset


def update_asset(*, asset: Asset, actor: User, fields: dict[str, Any]) -> Asset:
    previous = {name: getattr(asset, name) for name in fields}
    for name, value in fields.items():
        setattr(asset, name, value)
    try:
        with transaction.atomic():
            asset.full_clean()
            asset.save(update_fields=[*fields, "updated_at"])
            audit(
                organization=asset.organization,
                actor=actor,
                action="asset.updated",
                resource=asset,
                context={
                    "previous": {key: str(value) for key, value in previous.items()},
                    "fields": sorted(fields),
                },
            )
    except ValidationError as exc:
        raise _domain_validation(exc) from exc
    except IntegrityError as exc:
        raise DomainError(
            "Unit number or VIN already exists in this organization",
            code="duplicate_asset",
            status=409,
        ) from exc
    return asset


def upsert_external_asset(
    *,
    organization: Organization,
    actor: User,
    source_system: object,
    external_id: object,
    fields: dict[str, Any],
    correlation_id: str = "",
) -> tuple[Asset, bool]:
    try:
        source, identifier = normalize_external_identity(source_system, external_id)
    except ValidationError as exc:
        raise _domain_validation(exc) from exc
    fields = dict(fields)
    supplied_fields = sorted(fields)
    source_details = fields.pop("source_details", None)
    allowed = {
        "asset_type",
        "unit_number",
        "vin",
        "serial_number",
        "year",
        "make",
        "model",
        "ownership",
    }
    if unknown := sorted(set(fields) - allowed):
        raise DomainError(
            "Unsupported external asset fields", code="unsupported_fields", details=unknown
        )
    if "unit_number" in fields:
        fields["unit_number"] = str(fields["unit_number"] or "").strip().upper()
    if "vin" in fields:
        fields["vin"] = str(fields["vin"] or "").strip().upper()

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        asset = (
            Asset.objects.select_for_update()
            .filter(
                organization=organization,
                source_system=source,
                external_id=identifier,
            )
            .first()
        )
        changed: dict[str, Any] = {}
        if asset is None:
            asset_type = fields.get("asset_type")
            unit_number = str(fields.get("unit_number", "")).strip()
            if not isinstance(asset_type, AssetType):
                raise DomainError("asset_type is required", code="asset_type_required")
            if not unit_number:
                raise DomainError("unit_number is required", code="unit_number_required")
            if source_details is not None:
                fields["specs"] = _merge_external_source_details({}, source, source_details)
            asset = create_asset(
                organization=organization,
                actor=actor,
                source_system=source,
                external_id=identifier,
                status_reason=f"Created from {source}",
                status_source=source,
                **fields,
            )
            created = True
        else:
            changed = {
                name: value for name, value in fields.items() if getattr(asset, name) != value
            }
            if source_details is not None:
                merged_specs = _merge_external_source_details(asset.specs, source, source_details)
                if asset.specs != merged_specs:
                    changed["specs"] = merged_specs
            if changed:
                update_asset(asset=asset, actor=actor, fields=changed)
            created = False

        if created or changed:
            context = {
                "source_system": source,
                "external_id": identifier,
                "created": created,
                "fields": supplied_fields,
            }
            audit(
                organization=organization,
                actor=actor,
                action="asset.external_upserted",
                resource=asset,
                context=context,
                correlation_id=correlation_id,
                source=source,
            )
            emit(
                organization=organization,
                event_type="asset.external_upserted",
                resource=asset,
                payload=context,
            )
        return asset, created


def _merge_external_source_details(
    specs: object, source_system: str, source_details: object
) -> dict[str, Any]:
    if not isinstance(specs, dict) or not isinstance(source_details, dict):
        raise DomainError("External source details are invalid", code="invalid_source_details")
    merged = dict(specs)
    raw_integrations = merged.get("integrations", {})
    if not isinstance(raw_integrations, dict):
        raise DomainError(
            "Local integration details cannot be merged safely",
            code="invalid_local_specs",
            status=409,
        )
    integrations = dict(raw_integrations)
    raw_source = integrations.get(source_system, {})
    if not isinstance(raw_source, dict):
        raise DomainError(
            "Existing source details cannot be merged safely",
            code="invalid_local_specs",
            status=409,
        )
    integrations[source_system] = {**raw_source, **source_details}
    merged["integrations"] = integrations
    return merged


def create_meter(
    *,
    organization: Organization,
    asset: Asset,
    actor: User,
    name: str,
    kind: str,
    unit: str,
    record_id: uuid.UUID | None = None,
) -> Meter:
    if asset.organization_id != organization.pk:
        raise DomainError("Asset is outside this organization", code="invalid_asset")
    fields = {
        "organization": organization,
        "asset": asset,
        "name": name.strip(),
        "kind": kind,
        "unit": unit.strip(),
    }
    if record_id is not None:
        fields["id"] = record_id
    meter = Meter(**fields)
    try:
        with transaction.atomic():
            meter.full_clean()
            meter.save()
            audit(
                organization=organization,
                actor=actor,
                action="meter.created",
                resource=meter,
                context={"asset_id": str(asset.pk), "kind": kind, "unit": unit},
            )
    except ValidationError as exc:
        raise _domain_validation(exc) from exc
    except IntegrityError as exc:
        raise DomainError(
            "A meter with this name already exists", code="duplicate_meter", status=409
        ) from exc
    return meter


def record_meter_reading(
    *,
    meter: Meter,
    value: object,
    observed_at: Any,
    source: str,
    actor: User | None = None,
    quality: str = MeterReading.Quality.ACCEPTED,
    provenance: dict[str, Any] | None = None,
    external_id: str = "",
    reason: str = "",
    record_id: uuid.UUID | None = None,
    maximum_rate_per_hour: object | None = None,
    future_tolerance_seconds: int | None = None,
) -> MeterReading:
    if quality not in MeterReading.Quality.values:
        raise DomainError("Invalid meter reading quality", code="invalid_meter_quality")
    if not observed_at or timezone.is_naive(observed_at):
        raise DomainError("observed_at must include a timezone", code="invalid_observed_at")
    parsed = _decimal(value)
    source = source.strip().lower()
    if not source:
        raise DomainError("A meter source is required", code="meter_source_required")
    provenance_data = dict(provenance or {})
    external_id = external_id.strip()
    reason = reason.strip()
    submitted_reason = reason
    maximum_rate = _decimal(maximum_rate_per_hour) if maximum_rate_per_hour is not None else None
    if future_tolerance_seconds is not None and future_tolerance_seconds < 0:
        raise DomainError(
            "Future meter tolerance cannot be negative", code="invalid_meter_future_tolerance"
        )
    try:
        with transaction.atomic():
            meter = (
                Meter.objects.select_for_update().select_related("organization").get(pk=meter.pk)
            )
            if external_id:
                duplicate = MeterReading.objects.filter(
                    organization=meter.organization,
                    source=source,
                    external_id=external_id,
                ).first()
                if duplicate:
                    duplicate_submitted_reason = str(
                        duplicate.provenance.get("submitted_reason", duplicate.reason)
                    )
                    if (
                        duplicate.meter_id == meter.pk
                        and duplicate.value == parsed
                        and duplicate.observed_at == observed_at
                        and duplicate_submitted_reason == submitted_reason
                    ):
                        return duplicate
                    raise DomainError(
                        "This external reading ID was used for different data",
                        code="duplicate_meter_reading_conflict",
                        status=409,
                    )
            if (
                quality == MeterReading.Quality.ACCEPTED
                and future_tolerance_seconds is not None
                and observed_at > timezone.now() + timedelta(seconds=future_tolerance_seconds)
            ):
                quality = MeterReading.Quality.SUSPECT
                provenance_data["validation"] = "future_timestamp"
                reason = reason or "observed_at exceeds allowed future tolerance"
            if quality == MeterReading.Quality.ACCEPTED and meter.kind in {
                Meter.Kind.ODOMETER,
                Meter.Kind.ENGINE_HOURS,
            }:
                accepted = meter.readings.filter(
                    quality=MeterReading.Quality.ACCEPTED, correction__isnull=True
                )
                simultaneous = accepted.filter(observed_at=observed_at).first()
                previous = (
                    accepted.filter(observed_at__lt=observed_at)
                    .order_by("-observed_at", "-received_at")
                    .first()
                )
                following = (
                    accepted.filter(observed_at__gt=observed_at)
                    .order_by("observed_at", "received_at")
                    .first()
                )
                if simultaneous and parsed != simultaneous.value:
                    quality = MeterReading.Quality.SUSPECT
                    provenance_data["validation"] = "timestamp_conflict"
                    reason = reason or "reading conflicts with an accepted value at this time"
                elif (previous and parsed < previous.value) or (
                    following and parsed > following.value
                ):
                    quality = MeterReading.Quality.SUSPECT
                    provenance_data["validation"] = "non_monotonic"
                    reason = reason or "reading conflicts with accepted meter history"
                elif maximum_rate is not None:
                    if previous and parsed > previous.value:
                        hours = Decimal(
                            str((observed_at - previous.observed_at).total_seconds())
                        ) / Decimal("3600")
                        if hours > 0 and (parsed - previous.value) / hours > maximum_rate:
                            quality = MeterReading.Quality.SUSPECT
                    if (
                        quality == MeterReading.Quality.ACCEPTED
                        and following
                        and following.value > parsed
                    ):
                        hours = Decimal(
                            str((following.observed_at - observed_at).total_seconds())
                        ) / Decimal("3600")
                        if hours > 0 and (following.value - parsed) / hours > maximum_rate:
                            quality = MeterReading.Quality.SUSPECT
                    if quality == MeterReading.Quality.SUSPECT:
                        provenance_data["validation"] = "implausible_rate"
                        reason = reason or "change exceeds configured meter rate"
            if reason != submitted_reason:
                provenance_data["submitted_reason"] = submitted_reason
            reading_fields: dict[str, Any] = {
                "organization": meter.organization,
                "meter": meter,
                "value": parsed,
                "observed_at": observed_at,
                "source": source,
                "quality": quality,
                "provenance": provenance_data,
                "external_id": external_id,
                "reason": reason,
                "created_by": actor,
            }
            if record_id is not None:
                reading_fields["id"] = record_id
            reading = MeterReading(**reading_fields)
            reading.full_clean()
            reading.save()
            action = (
                "meter.reading_accepted"
                if reading.quality == MeterReading.Quality.ACCEPTED
                else "meter.reading_quarantined"
            )
            audit(
                organization=meter.organization,
                actor=actor,
                action=action,
                resource=reading,
                context={
                    "asset_id": str(meter.asset_id),
                    "meter_id": str(meter.pk),
                    "source": source,
                },
                source=source,
            )
            emit(
                organization=meter.organization,
                event_type=action,
                resource=reading,
                payload={"asset_id": str(meter.asset_id), "meter_id": str(meter.pk)},
            )
    except ValidationError as exc:
        raise _domain_validation(exc) from exc
    except IntegrityError as exc:
        raise DomainError(
            "This meter reading was already recorded", code="duplicate_meter_reading", status=409
        ) from exc
    return reading


def correct_meter_reading(
    *, reading: MeterReading, value: object, reason: str, actor: User
) -> MeterReading:
    reason = reason.strip()
    if not reason:
        raise DomainError("A correction reason is required", code="reason_required")
    with transaction.atomic():
        original = (
            MeterReading.objects.select_for_update()
            .select_related("meter", "organization")
            .get(pk=reading.pk)
        )
        if hasattr(original, "correction"):
            raise DomainError(
                "This reading was already corrected; correct the latest record instead",
                code="reading_already_corrected",
                status=409,
                details={"correction_id": str(original.correction.pk)},
            )
        corrected = MeterReading(
            organization=original.organization,
            meter=original.meter,
            value=_decimal(value),
            observed_at=original.observed_at,
            source="correction",
            quality=MeterReading.Quality.ACCEPTED,
            provenance={"corrected_source": original.source},
            corrects=original,
            reason=reason,
            created_by=actor,
        )
        try:
            corrected.full_clean()
            corrected.save()
        except ValidationError as exc:
            raise _domain_validation(exc) from exc
        audit(
            organization=original.organization,
            actor=actor,
            action="meter.reading_corrected",
            resource=corrected,
            previous_state=str(original.value),
            new_state=str(corrected.value),
            context={"original_reading_id": str(original.pk), "reason": reason},
        )
        emit(
            organization=original.organization,
            event_type="meter.reading_accepted",
            resource=corrected,
            payload={"asset_id": str(original.meter.asset_id), "meter_id": str(original.meter_id)},
        )
    return corrected


def change_asset_status(
    *,
    asset: Asset,
    new_status: str,
    reason: str,
    actor: User,
    classification: str = "company",
    override_reason: str = "",
    disposition: str = "",
    final_meter_readings: list[MeterReading] | None = None,
) -> AssetStatusEvent:
    reason = reason.strip()
    disposition = disposition.strip()
    if not reason:
        raise DomainError("A status-change reason is required", code="reason_required")
    if new_status not in Asset.Status.values:
        raise DomainError("Invalid asset status", code="invalid_asset_status")
    if classification not in {"company", "regulatory"}:
        raise DomainError("Invalid out-of-service classification", code="invalid_classification")
    if new_status == Asset.Status.RETIRED and not disposition:
        raise DomainError(
            "A retirement disposition is required",
            code="retirement_disposition_required",
        )
    transitions: dict[str, set[str]] = {
        Asset.Status.AVAILABLE: {
            Asset.Status.RESTRICTED,
            Asset.Status.OUT_OF_SERVICE,
            Asset.Status.RETIRED,
        },
        Asset.Status.RESTRICTED: {
            Asset.Status.AVAILABLE,
            Asset.Status.OUT_OF_SERVICE,
            Asset.Status.RETIRED,
        },
        Asset.Status.OUT_OF_SERVICE: {
            Asset.Status.AVAILABLE,
            Asset.Status.RESTRICTED,
            Asset.Status.RETIRED,
        },
        Asset.Status.RETIRED: set(),
    }
    with transaction.atomic():
        locked = Asset.objects.select_for_update().get(pk=asset.pk, organization=asset.organization)
        if new_status not in transitions[locked.status]:
            raise DomainError(
                f"Asset cannot transition from {locked.status} to {new_status}",
                code="invalid_asset_transition",
                status=409,
            )
        blockers: list[str] = []
        if new_status == Asset.Status.AVAILABLE:
            from maintenance.models import Defect

            blockers = [
                str(pk)
                for pk in Defect.objects.filter(
                    organization=locked.organization, asset=locked, safety_related=True
                )
                .exclude(status__in=["Verified", "Closed"])
                .values_list("pk", flat=True)
            ]
            if blockers and not (
                override_reason.strip() and has_permission(actor, "assets.manage")
            ):
                raise DomainError(
                    "Safety-related defects must be verified before return to service",
                    code="safety_blockers",
                    status=409,
                    details={"defect_ids": blockers},
                )

        previous = locked.status
        now = timezone.now()
        retirement_meter_snapshots: list[dict[str, object]] = []
        if new_status == Asset.Status.RETIRED:
            applicable_meters = list(
                Meter.objects.select_for_update()
                .filter(
                    organization=locked.organization,
                    asset=locked,
                    active=True,
                    kind__in=[Meter.Kind.ODOMETER, Meter.Kind.ENGINE_HOURS],
                )
                .order_by("pk")
            )
            supplied_ids = [reading.pk for reading in final_meter_readings or []]
            if len(supplied_ids) != len(set(supplied_ids)):
                raise DomainError(
                    "Each final meter reading may be supplied only once",
                    code="invalid_final_meter_readings",
                )
            supplied = {
                reading.pk: reading
                for reading in MeterReading.objects.select_for_update(of=("self",))
                .select_related("meter")
                .filter(organization=locked.organization, pk__in=supplied_ids)
            }
            if set(supplied) != set(supplied_ids):
                raise DomainError(
                    "A final meter reading is invalid or outside this organization",
                    code="invalid_final_meter_readings",
                )
            expected: dict[object, tuple[Meter, MeterReading | None]] = {
                meter.pk: (meter, meter.current_reading) for meter in applicable_meters
            }
            supplied_by_meter = {reading.meter_id: reading for reading in supplied.values()}
            expected_reading_ids = {
                current.pk for _, current in expected.values() if current is not None
            }
            if not set(supplied).issubset(expected_reading_ids):
                raise DomainError(
                    "Final meter evidence must contain only current accepted readings",
                    code="invalid_final_meter_readings",
                    details={"expected_reading_ids": sorted(map(str, expected_reading_ids))},
                )
            missing_meters = [
                {
                    "meter_id": str(meter.pk),
                    "name": meter.name,
                    "kind": meter.kind,
                    "unit": meter.unit,
                    "current_reading_id": str(current.pk) if current else None,
                }
                for meter, current in expected.values()
                if current is None or supplied_by_meter.get(meter.pk) != current
            ]
            if missing_meters:
                raise DomainError(
                    "Current accepted readings are required for active cumulative meters",
                    code="final_meter_readings_required",
                    details={"meters": missing_meters},
                )
            for meter, current in expected.values():
                assert current is not None
                retirement_meter_snapshots.append(_meter_snapshot(meter, current))
        locked.status = new_status
        locked.status_changed_at = now
        if new_status == Asset.Status.RETIRED:
            locked.archived_at = now
        locked.save(update_fields=["status", "status_changed_at", "archived_at", "updated_at"])
        removed_component_ids: list[str] = []
        if new_status == Asset.Status.RETIRED:
            open_installations = (
                ComponentInstallation.objects.select_for_update()
                .filter(asset=locked, removed_at__isnull=True)
                .select_related("component")
            )
            for row in open_installations:
                row.removed_at = now
                row.removed_by = actor
                row.removed_meters = retirement_meter_snapshots
                row.removal_reason = "Asset retired"
                # `source` records how the installation was created, not how it ended.
                row.save(
                    update_fields=[
                        "removed_at",
                        "removed_by",
                        "removed_meters",
                        "removal_reason",
                        "updated_at",
                    ]
                )
                removed_component_ids.append(str(row.component_id))
                audit(
                    organization=locked.organization,
                    actor=actor,
                    action="component.removed",
                    resource=row,
                    previous_state=locked.unit_number,
                    context={
                        "component_id": str(row.component_id),
                        "asset_id": str(locked.pk),
                        "reason": "Asset retired",
                        "removed_meters": row.removed_meters,
                    },
                )
        context: dict[str, object] = {"safety_blocker_ids": blockers}
        if override_reason.strip():
            context["override_reason"] = override_reason.strip()
        if new_status == Asset.Status.RETIRED:
            context.update(
                {
                    "retired_at": now.isoformat(),
                    "retirement_disposition": disposition,
                    "final_meter_readings": retirement_meter_snapshots,
                    "removed_component_ids": removed_component_ids,
                }
            )
        event = AssetStatusEvent.objects.create(
            organization=locked.organization,
            asset=locked,
            actor=actor,
            previous_status=previous,
            new_status=new_status,
            reason=reason,
            classification=classification,
            context=context,
        )
        audit(
            organization=locked.organization,
            actor=actor,
            action="asset.availability_changed",
            resource=locked,
            previous_state=previous,
            new_state=new_status,
            context={"reason": reason, "status_event_id": str(event.pk), **context},
        )
        emit(
            organization=locked.organization,
            event_type="asset.availability_changed",
            resource=locked,
            payload={"previous_status": previous, "new_status": new_status},
        )
    asset.refresh_from_db()
    return event


def _installed_elsewhere(open_row: ComponentInstallation) -> DomainError:
    return DomainError(
        "That component is already installed on another asset",
        code="component_installed_elsewhere",
        status=409,
        details={
            "asset_id": str(open_row.asset_id),
            "unit_number": open_row.asset.unit_number,
            "installation_id": str(open_row.pk),
        },
    )


def _resolve_component(
    *,
    organization: Organization,
    actor: User,
    component: Component | None,
    kind: str,
    serial_number: str,
    manufacturer: str,
    model: str,
) -> Component:
    if component is not None:
        if component.organization_id != organization.pk:
            raise DomainError("Unknown component", code="invalid_component")
        return component
    if not kind or not serial_number.strip():
        raise DomainError("kind and serial_number are required", code="component_identity_required")
    if kind not in Component.Kind.values:
        raise DomainError("Unknown component kind", code="invalid_component_kind")
    resolved, created = Component.objects.get_or_create(
        organization=organization,
        kind=kind,
        serial_number=serial_number.strip().upper(),
        defaults={"manufacturer": manufacturer[:100], "model": model[:100]},
    )
    if created:
        audit(
            organization=organization,
            actor=actor,
            action="component.created",
            resource=resolved,
            new_state=resolved.serial_number,
            context={"kind": resolved.kind, "serial_number": resolved.serial_number},
        )
    return resolved


# WorkOrder statuses that can no longer receive work. Plain strings, matching the
# rest of the codebase -- WorkOrder has no Status enum, only WorkOrder.STATUSES.
CLOSED_WORK_ORDER_STATUSES = ("Completed", "Closed", "Cancelled")


def _validate_component_work_order(*, asset: Asset, actor: User, work_order: Any) -> Any:
    """Technicians act only through an assigned open work order; managers act freely."""
    from maintenance.services import is_active_work_order_assignee

    if work_order is not None:
        if work_order.organization_id != asset.organization_id:
            raise DomainError("Unknown work order", code="invalid_work_order")
        if work_order.asset_id != asset.pk:
            raise DomainError("Work order is not on this asset", code="invalid_work_order")
        if work_order.status in CLOSED_WORK_ORDER_STATUSES:
            raise DomainError("Work order is not open", code="work_order_not_open", status=409)
    if has_permission(actor, "assets.manage") or has_permission(actor, "maintenance.manage"):
        return work_order
    if not has_permission(actor, "maintenance.execute"):
        raise DomainError("Not allowed to change components", code="permission_denied", status=403)
    if work_order is None:
        raise DomainError("A work order is required", code="work_order_required", status=403)
    if not is_active_work_order_assignee(work_order, actor):
        raise DomainError("Not assigned to this work order", code="permission_denied", status=403)
    return work_order


def install_component(
    *,
    asset: Asset,
    actor: User,
    component: Component | None = None,
    kind: str = "",
    serial_number: str = "",
    manufacturer: str = "",
    model: str = "",
    installed_at: datetime | None = None,
    work_order: Any = None,
    source: str = "web",
) -> ComponentInstallation:
    """Put a component on an asset. One open installation per component, enforced by the DB."""
    now = timezone.now()
    installed_at = installed_at or now
    if timezone.is_naive(installed_at):
        raise DomainError("installed_at must be timezone aware", code="invalid_installed_at")
    if installed_at > now:
        raise DomainError("installed_at cannot be in the future", code="invalid_installed_at")

    with transaction.atomic():
        # Lock order Asset -> Component -> installations, matching
        # associate_device_to_asset, so this cannot deadlock against retirement.
        locked_asset = Asset.objects.select_for_update().get(pk=asset.pk)
        if locked_asset.status == Asset.Status.RETIRED or locked_asset.archived_at:
            raise DomainError(
                "Retired assets cannot take components", code="asset_retired", status=409
            )
        resolved = _resolve_component(
            organization=locked_asset.organization,
            actor=actor,
            component=component,
            kind=kind,
            serial_number=serial_number,
            manufacturer=manufacturer,
            model=model,
        )
        work_order = _validate_component_work_order(
            asset=locked_asset, actor=actor, work_order=work_order
        )
        open_row = (
            ComponentInstallation.objects.select_for_update()
            .filter(component=resolved, removed_at__isnull=True)
            .select_related("asset")
            .first()
        )
        if open_row is not None:
            raise _installed_elsewhere(open_row)
        installation = ComponentInstallation(
            organization=locked_asset.organization,
            component=resolved,
            asset=locked_asset,
            installed_at=installed_at,
            installed_by=actor,
            installed_work_order=work_order,
            installed_meters=_meter_snapshots_as_of(locked_asset, installed_at),
            source=source,
        )
        try:
            installation.full_clean()
            installation.save()
        except ValidationError as exc:
            raise _domain_validation(exc) from exc
        except IntegrityError as exc:
            if "uniq_open_component_installation" not in str(exc):
                raise
            # Race path: another request opened an installation between the
            # pre-check and the insert. Re-read so the 409 has the same shape.
            raced = (
                ComponentInstallation.objects.filter(component=resolved, removed_at__isnull=True)
                .select_related("asset")
                .first()
            )
            if raced is None:
                raise
            raise _installed_elsewhere(raced) from exc

        audit(
            organization=locked_asset.organization,
            actor=actor,
            action="component.installed",
            resource=installation,
            new_state=locked_asset.unit_number,
            context={
                "component_id": str(resolved.pk),
                "asset_id": str(locked_asset.pk),
                "work_order_id": str(work_order.pk) if work_order else None,
                "installed_meters": installation.installed_meters,
            },
        )
        emit(
            organization=locked_asset.organization,
            event_type="component.installed",
            resource=installation,
            payload={
                "component_id": str(resolved.pk),
                "asset_id": str(locked_asset.pk),
                "installation_id": str(installation.pk),
            },
        )
    return installation


def remove_component(
    *,
    component: Component,
    actor: User,
    reason: str,
    removed_at: datetime | None = None,
    work_order: Any = None,
) -> ComponentInstallation:
    """Close the open installation period. This is the one UPDATE the trigger permits."""
    cleaned = reason.strip()
    if not cleaned:
        raise DomainError("A removal reason is required", code="reason_required")
    now = timezone.now()
    removed_at = removed_at or now
    if timezone.is_naive(removed_at):
        raise DomainError("removed_at must be timezone aware", code="invalid_removed_at")
    if removed_at > now:
        raise DomainError("removed_at cannot be in the future", code="invalid_removed_at")

    with transaction.atomic():
        installation = (
            ComponentInstallation.objects.select_for_update()
            .filter(component=component, removed_at__isnull=True)
            .select_related("asset")
            .first()
        )
        if installation is None:
            raise DomainError(
                "That component is not installed", code="component_not_installed", status=409
            )
        if removed_at <= installation.installed_at:
            raise DomainError("removed_at must be after installed_at", code="invalid_removed_at")
        asset = Asset.objects.select_for_update().get(pk=installation.asset_id)
        work_order = _validate_component_work_order(asset=asset, actor=actor, work_order=work_order)
        installation.removed_at = removed_at
        installation.removed_by = actor
        installation.removed_work_order = work_order
        installation.removed_meters = _meter_snapshots_as_of(asset, removed_at)
        installation.removal_reason = cleaned
        # Exactly the set the write-once trigger permits, plus updated_at.
        installation.save(
            update_fields=[
                "removed_at",
                "removed_by",
                "removed_work_order",
                "removed_meters",
                "removal_reason",
                "updated_at",
            ]
        )
        audit(
            organization=asset.organization,
            actor=actor,
            action="component.removed",
            resource=installation,
            previous_state=asset.unit_number,
            context={
                "component_id": str(component.pk),
                "asset_id": str(asset.pk),
                "work_order_id": str(work_order.pk) if work_order else None,
                "reason": cleaned,
                "removed_meters": installation.removed_meters,
            },
        )
        emit(
            organization=asset.organization,
            event_type="component.removed",
            resource=installation,
            payload={
                "component_id": str(component.pk),
                "asset_id": str(asset.pk),
                "installation_id": str(installation.pk),
            },
        )
    return installation
