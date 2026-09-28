from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from assets.models import Asset, Meter, MeterReading
from assets.services import record_meter_reading
from core.exceptions import DomainError
from core.models import User
from core.services import audit, emit
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .adapters import AdapterError, AutoPiAdapter, MeterValue, TelematicsEnvelope
from .diagnostics import evaluate_diagnostic
from .models import Device, DeviceAssetAssociation, NormalizedTelematicsEvent, TelematicsMessage


@dataclass(frozen=True)
class IngestResult:
    message: TelematicsMessage
    events: tuple[NormalizedTelematicsEvent, ...] = ()
    duplicate: bool = False
    response_status: int = 202
    error_code: str = ""


def authenticate_device(raw_token: str) -> Device:
    if not raw_token or len(raw_token) > 256:
        raise DomainError(
            "A valid X-Device-Token header is required",
            code="invalid_device_token",
            status=401,
        )
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    try:
        return Device.objects.select_related("organization").get(
            token_hash=digest, status=Device.Status.ACTIVE
        )
    except Device.DoesNotExist as exc:
        raise DomainError(
            "Invalid or disabled device token", code="invalid_device_token", status=401
        ) from exc


def associate_device_to_asset(
    *,
    device: Device,
    asset: Asset,
    actor: User,
    effective_from: Any,
    effective_to: Any = None,
) -> DeviceAssetAssociation:
    if (
        device.organization_id != asset.organization_id
        or actor.organization_id != device.organization_id
    ):
        raise DomainError(
            "Device and asset must belong to your organization", code="organization_mismatch"
        )
    if effective_to is not None and effective_to <= effective_from:
        raise DomainError("effective_to must be after effective_from", code="invalid_period")
    with transaction.atomic():
        device = Device.objects.select_for_update().get(pk=device.pk)
        associations = list(
            DeviceAssetAssociation.objects.select_for_update()
            .filter(device=device)
            .order_by("effective_from")
        )
        for existing in associations:
            if (
                existing.asset_id == asset.pk
                and existing.effective_from == effective_from
                and existing.effective_to == effective_to
            ):
                return existing

        open_association = next((item for item in associations if item.effective_to is None), None)
        if (
            open_association
            and effective_to is None
            and open_association.effective_from < effective_from
        ):
            open_association.effective_to = effective_from
            open_association.full_clean()
            open_association.save(update_fields=["effective_to", "updated_at"])

        overlaps = DeviceAssetAssociation.objects.filter(device=device).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from)
        )
        if effective_to is not None:
            overlaps = overlaps.filter(effective_from__lt=effective_to)
        if overlaps.exists():
            raise DomainError(
                "This device already has an asset association in that period",
                code="association_overlap",
                status=409,
            )
        association = DeviceAssetAssociation(
            organization=device.organization,
            device=device,
            asset=asset,
            effective_from=effective_from,
            effective_to=effective_to,
            assigned_by=actor,
        )
        association.full_clean()
        association.save()
        audit(
            organization=device.organization,
            actor=actor,
            action="device.associated",
            resource=association,
            context={"device_id": str(device.pk), "asset_id": str(asset.pk)},
        )
        return association


def _config(device: Device, key: str, default: object) -> object:
    section = device.organization.settings.get("telematics", {})
    return section.get(key, default) if isinstance(section, dict) else default


def _decimal_config(device: Device, key: str, default: str) -> Decimal:
    try:
        value = Decimal(str(_config(device, key, default)))
        return value if value.is_finite() and value >= 0 else Decimal(default)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _int_config(device: Device, key: str, default: int) -> int:
    try:
        value = int(str(_config(device, key, default)))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def _invalid_raw_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _store_rejected(
    *,
    device: Device,
    payload: object,
    reason: str,
    status: str = TelematicsMessage.Status.REJECTED,
    envelope: TelematicsEnvelope | None = None,
) -> TelematicsMessage:
    raw_payload = payload if isinstance(payload, dict) else {"unparsed": str(payload)}
    digest = envelope.canonical_hash if envelope else _invalid_raw_hash(raw_payload)
    supplied_message_id = (
        envelope.message_id if envelope else str(raw_payload.get("messageId", ""))[:160]
    )
    # Rejected evidence must not reserve an otherwise valid source id forever. The
    # original value remains available in raw_payload for investigation.
    message_id = "" if status == TelematicsMessage.Status.REJECTED else supplied_message_id
    if message_id:
        existing = TelematicsMessage.objects.filter(device=device, message_id=message_id).first()
        if existing:
            Device.objects.filter(pk=device.pk).update(duplicate_count=F("duplicate_count") + 1)
            return existing
    existing = TelematicsMessage.objects.filter(
        device=device, canonical_hash=digest, status=status
    ).first()
    if existing:
        Device.objects.filter(pk=device.pk).update(duplicate_count=F("duplicate_count") + 1)
        return existing
    message = TelematicsMessage.objects.create(
        organization=device.organization,
        device=device,
        source=envelope.source if envelope else str(raw_payload.get("source", "unknown"))[:40],
        schema_version=(
            envelope.schema_version if envelope else str(raw_payload.get("schemaVersion", ""))[:20]
        ),
        message_id=message_id,
        canonical_hash=digest,
        message_type=(
            envelope.message_type if envelope else str(raw_payload.get("type", "unknown"))[:40]
        ),
        observed_at=envelope.observed_at if envelope else None,
        sent_at=envelope.sent_at if envelope else None,
        received_at=timezone.now(),
        sequence=envelope.sequence if envelope else None,
        raw_payload=raw_payload,
        status=status,
        rejection_reason=reason[:500],
    )
    counter = (
        "quarantined_count" if status == TelematicsMessage.Status.QUARANTINED else "rejected_count"
    )
    Device.objects.filter(pk=device.pk).update(**{counter: F(counter) + 1})
    audit(
        organization=device.organization,
        actor=None,
        action=f"telematics.message_{status}",
        resource=message,
        context={"device_id": str(device.pk), "reason": reason},
        source="autopi",
    )
    return message


def _association(device: Device, observed_at: Any) -> DeviceAssetAssociation | None:
    return (
        DeviceAssetAssociation.objects.select_related("asset")
        .filter(device=device, effective_from__lte=observed_at)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=observed_at))
        .order_by("-effective_from")
        .first()
    )


def _meter_value_for_unit(value: MeterValue, unit: str) -> Decimal | None:
    normalized = unit.strip().lower()
    if value.meter_kind == "odometer":
        if normalized in {"mi", "mile", "miles"}:
            return value.value
        if normalized in {"km", "kilometer", "kilometers"}:
            return value.value / Decimal("0.621371192")
    elif value.meter_kind == "engine_hours" and normalized in {"h", "hr", "hour", "hours"}:
        return value.value
    return None


def _meter_quality(
    *, meter: Meter, value: Decimal, observed_at: Any, device: Device
) -> tuple[str, str]:
    accepted = MeterReading.objects.filter(meter=meter, quality="accepted", correction__isnull=True)
    simultaneous = accepted.filter(observed_at=observed_at).first()
    previous = accepted.filter(observed_at__lt=observed_at).order_by("-observed_at").first()
    following = accepted.filter(observed_at__gt=observed_at).order_by("observed_at").first()
    absolute_default = "10000000" if meter.kind == "odometer" else "1000000"
    if value > _decimal_config(device, f"max_{meter.kind}", absolute_default):
        return "suspect", "value exceeds configured maximum"
    if simultaneous and value != simultaneous.value:
        return "suspect", "reading conflicts with an accepted value at the same timestamp"
    if previous and value < previous.value:
        return "suspect", "reading decreases from prior accepted value"
    if following and value > following.value:
        return "suspect", "late reading exceeds a later accepted value"
    default_rate = (
        str(getattr(settings, "METER_MAX_MILES_PER_HOUR", "100.0"))
        if meter.kind == "odometer"
        else "1.25"
    )
    maximum_rate = _decimal_config(device, f"max_{meter.kind}_per_hour", default_rate)
    if previous and value > previous.value:
        hours = Decimal(str((observed_at - previous.observed_at).total_seconds())) / Decimal("3600")
        if hours > 0:
            if (value - previous.value) / hours > maximum_rate:
                return "suspect", "change exceeds configured rate"
    if following and following.value > value:
        hours = Decimal(str((following.observed_at - observed_at).total_seconds())) / Decimal(
            "3600"
        )
        if hours > 0 and (following.value - value) / hours > maximum_rate:
            return "suspect", "change to later reading exceeds configured rate"
    return "accepted", ""


def _normalize_meter(
    *,
    message: TelematicsMessage,
    device: Device,
    association: DeviceAssetAssociation,
    reading: MeterValue,
) -> NormalizedTelematicsEvent:
    observed_at = message.observed_at
    if observed_at is None:  # Model permits rejected raw messages without a valid source timestamp.
        raise DomainError("Normalized meter event requires observedAt", code="invalid_timestamp")
    meter = (
        Meter.objects.select_for_update()
        .filter(
            organization=device.organization,
            asset=association.asset,
            kind=reading.meter_kind,
            active=True,
        )
        .order_by("created_at")
        .first()
    )
    reason = ""
    quality: str = NormalizedTelematicsEvent.Quality.ACCEPTED
    value = reading.value
    meter_reading = None
    if meter is None:
        quality = NormalizedTelematicsEvent.Quality.SUSPECT
        reason = "asset has no active meter for this signal"
    else:
        converted = _meter_value_for_unit(reading, meter.unit)
        if converted is None:
            quality = NormalizedTelematicsEvent.Quality.REJECTED
            reason = "configured meter unit is incompatible with signal"
        else:
            value = converted
            quality, reason = _meter_quality(
                meter=meter, value=value, observed_at=observed_at, device=device
            )
            meter_reading = record_meter_reading(
                meter=meter,
                value=value,
                observed_at=observed_at,
                source="autopi",
                quality=quality,
                provenance={
                    "telematics_message_id": str(message.pk),
                    "device_id": str(device.pk),
                    "source_signal": reading.signal,
                    "source_value": str(reading.original_value),
                    "source_unit": reading.original_unit,
                },
                external_id=f"telematics:{message.pk}:{reading.signal}",
                reason=reason,
            )
    event = NormalizedTelematicsEvent.objects.create(
        organization=device.organization,
        message=message,
        device=device,
        asset=association.asset,
        kind=NormalizedTelematicsEvent.Kind.METER,
        signal=reading.signal,
        value=value,
        unit=meter.unit if meter else reading.unit,
        observed_at=observed_at,
        quality=quality,
        reason=reason,
        normalized_payload={
            "meter_kind": reading.meter_kind,
            "source_value": str(reading.original_value),
            "source_unit": reading.original_unit,
        },
        meter_reading=meter_reading,
    )
    emit(
        organization=device.organization,
        event_type="telematics.event_normalized",
        resource=event,
        payload={
            "asset_id": str(association.asset_id),
            "signal": reading.signal,
            "quality": quality,
        },
    )
    if quality != NormalizedTelematicsEvent.Quality.ACCEPTED:
        Device.objects.filter(pk=device.pk).update(quarantined_count=F("quarantined_count") + 1)
    return event


def _normalize_diagnostic(
    *,
    message: TelematicsMessage,
    device: Device,
    association: DeviceAssetAssociation,
    position: int,
    payload: dict[str, Any],
) -> NormalizedTelematicsEvent:
    observed_at = message.observed_at
    if observed_at is None:
        raise DomainError("Normalized diagnostic requires observedAt", code="invalid_timestamp")
    # Diagnostics remain reviewable evidence. Listed codes raise a human-reviewed
    # alert (see diagnostics.py); nothing here ever creates a work order.
    protocol = str(payload.get("protocol", "unknown")).lower()[:20]
    spn = str(payload.get("spn", "unknown"))[:20]
    fmi = str(payload.get("fmi", "unknown"))[:20]
    event = NormalizedTelematicsEvent.objects.create(
        organization=device.organization,
        message=message,
        device=device,
        asset=association.asset,
        kind=NormalizedTelematicsEvent.Kind.DIAGNOSTIC,
        signal=f"dtc:{position}:{protocol}:{spn}:{fmi}"[:80],
        observed_at=observed_at,
        quality=NormalizedTelematicsEvent.Quality.ACCEPTED,
        normalized_payload={
            "protocol": protocol,
            "spn": payload.get("spn"),
            "fmi": payload.get("fmi"),
            "ecu": payload.get("ecu"),
            "occurrences": payload.get("occurrences"),
        },
    )
    emit(
        organization=device.organization,
        event_type="telematics.event_normalized",
        resource=event,
        payload={
            "asset_id": str(association.asset_id),
            "signal": event.signal,
            "quality": event.quality,
        },
    )
    evaluate_diagnostic(event)
    return event


def ingest_autopi(*, device: Device, payload: object) -> IngestResult:
    adapter = AutoPiAdapter()
    try:
        envelope = adapter.parse(payload)
    except AdapterError as exc:
        with transaction.atomic():
            locked = (
                Device.objects.select_for_update().select_related("organization").get(pk=device.pk)
            )
            message = _store_rejected(device=locked, payload=payload, reason=str(exc))
        return IngestResult(message=message, response_status=400, error_code=exc.code)

    with transaction.atomic():
        device = Device.objects.select_for_update().select_related("organization").get(pk=device.pk)
        expected_org = str(device.organization_id)
        expected_devices = {str(device.pk), device.external_id}
        if envelope.organization_id != expected_org or envelope.device_id not in expected_devices:
            message = _store_rejected(
                device=device,
                payload=payload,
                envelope=envelope,
                reason="payload identity does not match authenticated device",
            )
            return IngestResult(
                message=message, response_status=403, error_code="identity_mismatch"
            )

        dedupe_hashes = {envelope.canonical_hash, envelope.legacy_canonical_hash}
        eligible = TelematicsMessage.objects.filter(
            device=device, status=TelematicsMessage.Status.ACCEPTED
        )
        same_id = (
            eligible.filter(message_id=envelope.message_id).first() if envelope.message_id else None
        )
        if same_id:
            Device.objects.filter(pk=device.pk).update(duplicate_count=F("duplicate_count") + 1)
            if same_id.canonical_hash not in dedupe_hashes:
                conflict = _store_rejected(
                    device=device,
                    payload=payload,
                    envelope=envelope,
                    reason="messageId was already used for different content",
                )
                return IngestResult(
                    message=conflict, response_status=409, error_code="message_id_conflict"
                )
            return IngestResult(message=same_id, duplicate=True, response_status=200)

        if envelope.sequence is not None:
            same_sequence = eligible.filter(sequence=envelope.sequence).first()
            if same_sequence:
                if same_sequence.canonical_hash in dedupe_hashes:
                    Device.objects.filter(pk=device.pk).update(
                        duplicate_count=F("duplicate_count") + 1
                    )
                    return IngestResult(message=same_sequence, duplicate=True, response_status=200)
                conflict = _store_rejected(
                    device=device,
                    payload=payload,
                    envelope=envelope,
                    reason="sequence was already used for different content",
                )
                return IngestResult(
                    message=conflict, response_status=409, error_code="sequence_conflict"
                )

        duplicate = eligible.filter(canonical_hash__in=dedupe_hashes).first()
        if duplicate:
            Device.objects.filter(pk=device.pk).update(duplicate_count=F("duplicate_count") + 1)
            return IngestResult(message=duplicate, duplicate=True, response_status=200)

        now = timezone.now()
        future_tolerance = timedelta(
            seconds=_int_config(device, "future_timestamp_tolerance_seconds", 300)
        )
        retention = timedelta(days=_int_config(device, "history_retention_days", 3650))
        if envelope.observed_at > now + future_tolerance:
            message = _store_rejected(
                device=device,
                payload=payload,
                envelope=envelope,
                status=TelematicsMessage.Status.QUARANTINED,
                reason="observedAt is too far in the future",
            )
            return IngestResult(message=message, response_status=422, error_code="future_timestamp")
        if envelope.observed_at < now - retention:
            message = _store_rejected(
                device=device,
                payload=payload,
                envelope=envelope,
                status=TelematicsMessage.Status.QUARANTINED,
                reason="observedAt is outside the configured history retention window",
            )
            return IngestResult(message=message, response_status=422, error_code="expired_message")
        association = _association(device, envelope.observed_at)
        if association is None:
            message = _store_rejected(
                device=device,
                payload=payload,
                envelope=envelope,
                status=TelematicsMessage.Status.QUARANTINED,
                reason="no device-to-asset association exists at observedAt",
            )
            return IngestResult(message=message, response_status=422, error_code="no_association")

        message = TelematicsMessage.objects.create(
            organization=device.organization,
            device=device,
            source=envelope.source,
            schema_version=envelope.schema_version,
            message_id=envelope.message_id,
            canonical_hash=envelope.canonical_hash,
            message_type=envelope.message_type,
            observed_at=envelope.observed_at,
            sent_at=envelope.sent_at,
            received_at=now,
            sequence=envelope.sequence,
            raw_payload=payload,
            status=TelematicsMessage.Status.ACCEPTED,
        )
        events = [
            _normalize_meter(
                message=message, device=device, association=association, reading=reading
            )
            for reading in envelope.meters
        ]
        events.extend(
            _normalize_diagnostic(
                message=message,
                device=device,
                association=association,
                position=index,
                payload=diagnostic,
            )
            for index, diagnostic in enumerate(envelope.diagnostics)
        )
        Device.objects.filter(pk=device.pk).update(
            message_count=F("message_count") + 1, last_seen_at=now
        )
        audit(
            organization=device.organization,
            actor=None,
            action="telematics.message_accepted",
            resource=message,
            context={
                "device_id": str(device.pk),
                "asset_id": str(association.asset_id),
                "normalized_event_count": len(events),
            },
            source="autopi",
        )
        return IngestResult(message=message, events=tuple(events))
