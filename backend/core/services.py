from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from django.db import transaction
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response

from .exceptions import DomainError
from .models import AuditEvent, IdempotencyRecord, Organization, OutboxEvent, User


def audit(
    *,
    organization: Organization,
    actor: User | None,
    action: str,
    resource: object,
    previous_state: str = "",
    new_state: str = "",
    context: dict[str, Any] | None = None,
    correlation_id: str = "",
    source: str = "web",
) -> AuditEvent:
    resource_type = resource.__class__.__name__
    resource_id = str(getattr(resource, "pk", resource))
    return AuditEvent.objects.create(
        organization=organization,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        previous_state=previous_state,
        new_state=new_state,
        context=context or {},
        correlation_id=correlation_id,
        source=source,
    )


def emit(
    *,
    organization: Organization,
    event_type: str,
    resource: object,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    return OutboxEvent.objects.create(
        organization=organization,
        event_type=event_type,
        resource_type=resource.__class__.__name__,
        resource_id=str(getattr(resource, "pk", resource)),
        payload=payload or {},
        available_at=timezone.now(),
    )


def request_fingerprint(request: Request) -> str:
    files: list[dict[str, object]] = []
    uploaded = getattr(request, "FILES", None)
    if uploaded:
        for field in sorted(uploaded):
            for item in uploaded.getlist(field):
                position = item.tell()
                item.seek(0)
                digest = hashlib.sha256()
                for chunk in item.chunks():
                    digest.update(chunk)
                item.seek(position)
                files.append(
                    {
                        "field": field,
                        "name": item.name,
                        "size": item.size,
                        "content_type": item.content_type,
                        "sha256": digest.hexdigest(),
                    }
                )
    payload = json.dumps(
        {"data": request.data, "files": files},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(
        request.method.encode() + b"\0" + request.path.encode() + b"\0" + payload
    ).hexdigest()


def idempotent(
    request: Request,
    handler: Callable[[], Response | object],
    *,
    required: bool = True,
    stored_response_transform: Callable[[Response], object] | None = None,
    replay_response_transform: Callable[[object], object] | None = None,
) -> Response:
    key = request.headers.get("Idempotency-Key") or request.data.get("operation_id")
    if not key:
        if required:
            raise DomainError(
                "Idempotency-Key is required", code="idempotency_key_required", status=400
            )
        return handler()
    try:
        key = str(uuid.UUID(str(key)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError(
            "Idempotency-Key must be a UUID",
            code="invalid_idempotency_key",
        ) from exc
    fingerprint = request_fingerprint(request)
    user = cast(User, request.user)
    organization = cast(Organization, user.organization)
    with transaction.atomic():
        record, created = IdempotencyRecord.objects.select_for_update().get_or_create(
            organization=organization,
            user=user,
            route=request.path,
            key=key,
            defaults={"request_fingerprint": fingerprint},
        )
        if not created:
            if record.request_fingerprint != fingerprint:
                raise DomainError(
                    "This idempotency key was already used for different input",
                    code="idempotency_conflict",
                    status=409,
                )
            if record.state == "complete":
                replay_body = record.response_body
                if replay_response_transform:
                    replay_body = replay_response_transform(replay_body)
                return Response(replay_body, status=record.response_status)
        response = handler()
        if not isinstance(response, Response):
            response = Response(response)
        record.state = "complete"
        record.response_status = response.status_code
        stored_response = (
            stored_response_transform(response) if stored_response_transform else response.data
        )
        record.response_body = json.loads(json.dumps(stored_response, default=str))
        record.save(update_fields=["state", "response_status", "response_body", "updated_at"])
        return response
