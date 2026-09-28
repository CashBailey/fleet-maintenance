from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from assets.models import Asset
from core.exceptions import DomainError
from core.permissions import require_permission
from core.services import audit, idempotent
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Device, NormalizedTelematicsEvent, TelematicsMessage
from .parsers import BoundedTelematicsJSONParser
from .services import associate_device_to_asset, authenticate_device, ingest_autopi


def _device_json(device: Device) -> dict[str, object]:
    association = (
        device.associations.filter(effective_to__isnull=True).select_related("asset").first()
    )
    return {
        "id": str(device.pk),
        "name": device.name,
        "provider": device.provider,
        "vendor": device.vendor,
        "model": device.model,
        "serial_number": device.serial_number,
        "external_id": device.external_id,
        "status": device.status,
        "last_seen_at": device.last_seen_at,
        "message_count": device.message_count,
        "duplicate_count": device.duplicate_count,
        "rejected_count": device.rejected_count,
        "quarantined_count": device.quarantined_count,
        "asset": (
            {"id": str(association.asset_id), "unit_number": association.asset.unit_number}
            if association
            else None
        ),
    }


def _redact_device_token(response: Response) -> object:
    body = dict(response.data["device"])
    body.pop("token", None)
    return {
        "device": body,
        "secret_recoverable": False,
        "message": (
            "The device token was shown once and cannot be recovered; "
            "rotate it to obtain a new token."
        ),
    }


@api_view(["GET", "POST"])
@require_permission("integrations.manage")
def devices(request: Any) -> Response:
    organization = request.user.organization
    if request.method == "GET":
        rows = Device.objects.filter(organization=organization).prefetch_related("associations")
        return Response({"devices": [_device_json(device) for device in rows]})

    name = str(request.data.get("name", "")).strip()
    serial_number = str(request.data.get("serial_number", "")).strip()
    external_id = str(request.data.get("external_id", "")).strip()
    if not name or not serial_number:
        raise DomainError("name and serial_number are required", code="required_fields")
    if not external_id:
        external_id = serial_number

    def register() -> Response:
        token = f"fdev_{secrets.token_urlsafe(32)}"
        try:
            with transaction.atomic():
                device = Device.objects.create(
                    organization=organization,
                    name=name[:120],
                    provider="autopi",
                    vendor=str(request.data.get("vendor", "AutoPi")).strip()[:80] or "AutoPi",
                    model=str(request.data.get("model", "")).strip()[:120],
                    serial_number=serial_number[:160],
                    external_id=external_id[:160],
                    token_prefix=token[:12],
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                )
                audit(
                    organization=organization,
                    actor=request.user,
                    action="device.registered",
                    resource=device,
                    context={"provider": device.provider, "serial_number": device.serial_number},
                )
        except IntegrityError as exc:
            raise DomainError(
                "A device with this serial number or external ID already exists",
                code="duplicate_device",
                status=409,
            ) from exc
        body = _device_json(device)
        body["token"] = token
        return Response({"device": body, "secret_recoverable": True}, status=201)

    return idempotent(request, register, stored_response_transform=_redact_device_token)


@api_view(["POST"])
@require_permission("integrations.manage")
def rotate_device_token(request: Any, device_id: object) -> Response:
    organization = request.user.organization

    def rotate() -> Response:
        token = f"fdev_{secrets.token_urlsafe(32)}"
        with transaction.atomic():
            device = get_object_or_404(
                Device.objects.select_for_update(), pk=device_id, organization=organization
            )
            previous_prefix = device.token_prefix
            device.token_prefix = token[:12]
            device.token_hash = hashlib.sha256(token.encode()).hexdigest()
            device.save(update_fields=["token_prefix", "token_hash", "updated_at"])
            audit(
                organization=organization,
                actor=request.user,
                action="device.token_rotated",
                resource=device,
                context={"previous_token_prefix": previous_prefix},
            )
        body = _device_json(device)
        body["token"] = token
        return Response({"device": body, "secret_recoverable": True})

    return idempotent(request, rotate, stored_response_transform=_redact_device_token)


@api_view(["POST"])
@require_permission("integrations.manage")
def set_device_status(request: Any, device_id: object) -> Response:
    organization = request.user.organization
    status = str(request.data.get("status", "")).strip()
    allowed = {Device.Status.ACTIVE, Device.Status.DISABLED}
    if status not in allowed:
        raise DomainError(
            "status must be active or disabled", code="invalid_device_status", status=400
        )

    def update_status() -> Response:
        with transaction.atomic():
            device = get_object_or_404(
                Device.objects.select_for_update(), pk=device_id, organization=organization
            )
            previous_status = device.status
            if previous_status != status:
                device.status = status
                device.save(update_fields=["status", "updated_at"])
                audit(
                    organization=organization,
                    actor=request.user,
                    action="device.status_changed",
                    resource=device,
                    previous_state=previous_status,
                    new_state=status,
                )
        return Response({"device": _device_json(device)})

    return idempotent(request, update_status)


def _request_datetime(value: object, name: str, *, default_now: bool = False) -> datetime | None:
    if value in (None, "") and default_now:
        return timezone.now()
    if value in (None, ""):
        return None
    parsed = parse_datetime(str(value))
    if parsed is None or parsed.utcoffset() is None:
        raise DomainError(
            f"{name} must be an ISO 8601 timestamp with timezone", code="invalid_timestamp"
        )
    return parsed


def _response_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


@api_view(["POST"])
@require_permission("integrations.manage")
def associate_device(request: Any, device_id: object) -> Response:
    organization = request.user.organization
    try:
        asset_id = UUID(str(request.data.get("asset_id", "")))
    except ValueError as exc:
        raise DomainError("A valid asset_id is required", code="invalid_asset") from exc
    effective_from = _request_datetime(
        request.data.get("effective_from"), "effective_from", default_now=True
    )
    effective_to = _request_datetime(request.data.get("effective_to"), "effective_to")

    def perform() -> Response:
        device = get_object_or_404(Device, pk=device_id, organization=organization)
        asset = get_object_or_404(
            Asset, pk=asset_id, organization=organization, archived_at__isnull=True
        )
        association = associate_device_to_asset(
            device=device,
            asset=asset,
            actor=request.user,
            effective_from=effective_from,
            effective_to=effective_to,
        )
        return Response(
            {
                "association": {
                    "id": str(association.pk),
                    "device_id": str(association.device_id),
                    "asset_id": str(association.asset_id),
                    "effective_from": _response_datetime(association.effective_from),
                    "effective_to": _response_datetime(association.effective_to),
                }
            },
            status=201,
        )

    return idempotent(request, perform)


@api_view(["POST"])
@parser_classes([BoundedTelematicsJSONParser])
@authentication_classes([])
@permission_classes([AllowAny])
def autopi_ingest(request: Any) -> Response:
    device = authenticate_device(request.headers.get("X-Device-Token", ""))
    result = ingest_autopi(device=device, payload=request.data)
    if result.error_code:
        messages = {
            "identity_mismatch": "Payload identity does not match the authenticated device",
            "message_id_conflict": "messageId was already used for different content",
            "sequence_conflict": "sequence was already used for different content",
            "no_association": "No device-to-asset association exists at observedAt",
            "future_timestamp": "observedAt is too far in the future",
            "expired_message": "observedAt is outside the retention window",
        }
        return Response(
            {
                "error": {
                    "code": result.error_code,
                    "message": messages.get(
                        result.error_code, result.message.rejection_reason or "Message rejected"
                    ),
                    "details": {"message_id": str(result.message.pk)},
                }
            },
            status=result.response_status,
        )
    return Response(
        {
            "message": {
                "id": str(result.message.pk),
                "status": result.message.status,
                "duplicate": result.duplicate,
                "normalized_events": [
                    {
                        "id": str(event.pk),
                        "kind": event.kind,
                        "signal": event.signal,
                        "quality": event.quality,
                        "reason": event.reason,
                    }
                    for event in result.events
                ],
            }
        },
        status=result.response_status,
    )


@api_view(["GET"])
@require_permission("reports.integration")
def data_quality(request: Any) -> Response:
    organization = request.user.organization
    devices_qs = Device.objects.filter(organization=organization)
    message_counts = {
        row["status"]: row["count"]
        for row in TelematicsMessage.objects.filter(organization=organization)
        .values("status")
        .annotate(count=Count("id"))
    }
    suspect = (
        NormalizedTelematicsEvent.objects.filter(organization=organization)
        .exclude(quality=NormalizedTelematicsEvent.Quality.ACCEPTED)
        .select_related("asset", "device")[:100]
    )
    return Response(
        {
            "summary": {
                "active_devices": devices_qs.filter(status=Device.Status.ACTIVE).count(),
                "accepted_messages": message_counts.get(TelematicsMessage.Status.ACCEPTED, 0),
                "quarantined_messages": message_counts.get(TelematicsMessage.Status.QUARANTINED, 0),
                "rejected_messages": message_counts.get(TelematicsMessage.Status.REJECTED, 0),
                "suspect_events": NormalizedTelematicsEvent.objects.filter(
                    organization=organization,
                    quality=NormalizedTelematicsEvent.Quality.SUSPECT,
                ).count(),
            },
            "devices": [_device_json(device) for device in devices_qs],
            "exceptions": [
                {
                    "id": str(event.pk),
                    "device": event.device.name,
                    "asset": event.asset.unit_number if event.asset else None,
                    "signal": event.signal,
                    "quality": event.quality,
                    "reason": event.reason,
                    "observed_at": event.observed_at,
                }
                for event in suspect
            ],
        }
    )
