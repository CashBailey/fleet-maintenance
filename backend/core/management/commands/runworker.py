from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from argparse import ArgumentParser
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    Notification,
    OutboxEvent,
    WebhookDelivery,
    WebhookSubscription,
    WorkerHeartbeat,
)
from core.permissions import redact_financial_fields
from core.security import ResolvedOutboundURL, resolve_outbound_url

MAX_WEBHOOK_ATTEMPTS = 8
MAX_WEBHOOK_RETRY_SECONDS = 3600
_last_delivery_organization_id: uuid.UUID | None = None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, *, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), timeout=self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, *, timeout: float):
        tls_context = ssl.create_default_context()
        super().__init__(hostname, port=port, timeout=timeout, context=tls_context)
        self._pinned_address = address
        self._tls_context = tls_context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port), timeout=self.timeout
        )
        self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)


class _WebhookResponse:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ):
        self.connection = connection
        self.response = response
        self.status = response.status

    def __enter__(self) -> "_WebhookResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.response.close()
        self.connection.close()


def _open_webhook(
    request: urllib.request.Request,
    *,
    target: ResolvedOutboundURL | None = None,
    timeout: float,
) -> _WebhookResponse:
    target = target or resolve_outbound_url(request.full_url)
    address = target.addresses[0]
    connection: http.client.HTTPConnection
    if target.scheme == "https":
        connection = _PinnedHTTPSConnection(target.hostname, address, target.port, timeout=timeout)
    else:
        connection = _PinnedHTTPConnection(target.hostname, address, target.port, timeout=timeout)
    try:
        connection.request(
            request.get_method(),
            target.request_target,
            body=request.data,
            headers=dict(request.header_items()),
        )
        return _WebhookResponse(connection, connection.getresponse())
    except Exception:
        connection.close()
        raise


def _retry_delay_seconds(attempts: int) -> int:
    ceiling = min(MAX_WEBHOOK_RETRY_SECONDS, 2 ** min(attempts, 12))
    floor = ceiling // 2
    return floor + secrets.randbelow(ceiling - floor + 1)


class Command(BaseCommand):
    help = "Process Fleetline's PostgreSQL-backed transactional outbox"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll", type=float, default=0.5)

    def handle(self, *args: Any, **options: Any) -> None:
        next_pm_recalculation = 0.0
        pm_recalculation_error = ""
        while True:
            now = time.monotonic()
            if now >= next_pm_recalculation:
                try:
                    from maintenance.services import recalculate_date_plans

                    recalculate_date_plans()
                except Exception as exc:  # noqa: BLE001 - periodic work must not stop the worker
                    pm_recalculation_error = f"{type(exc).__name__}: {exc}"[:1000]
                    self.stderr.write(
                        f"Preventive-maintenance recalculation failed: {pm_recalculation_error}"
                    )
                    next_pm_recalculation = now + min(
                        60, settings.PM_RECALCULATION_INTERVAL_SECONDS
                    )
                else:
                    pm_recalculation_error = ""
                    next_pm_recalculation = now + settings.PM_RECALCULATION_INTERVAL_SECONDS
            WorkerHeartbeat.objects.update_or_create(
                name="default",
                defaults={
                    "seen_at": timezone.now(),
                    "details": {
                        "pid": str(__import__("os").getpid()),
                        "pm_recalculation_error": pm_recalculation_error,
                    },
                },
            )
            processed = self.process_one()
            self.deliver_one()
            if options["once"]:
                return
            if not processed:
                time.sleep(options["poll"])

    def process_one(self) -> bool:
        with transaction.atomic():
            event = (
                OutboxEvent.objects.select_for_update(skip_locked=True)
                .filter(processed_at__isnull=True, available_at__lte=timezone.now())
                .order_by("created_at")
                .first()
            )
            if event is None:
                return False
            try:
                # A database error in a handler aborts its transaction. Keep the
                # outbox claim outside a savepoint so the event can be recorded for
                # retry instead of leaving the worker transaction unusable.
                with transaction.atomic():
                    if event.event_type == "meter.reading_accepted":
                        from maintenance.services import recalculate_asset_plans

                        recalculate_asset_plans(event.organization, event.payload.get("asset_id"))
                    elif event.event_type == "document.extraction_requested":
                        from core.document_library import process_document_event

                        process_document_event(event.resource_id, event.organization_id)
                    self.create_notifications(event)
                    for subscription in WebhookSubscription.objects.filter(
                        organization=event.organization, active=True
                    ):
                        if (
                            not subscription.event_types
                            or event.event_type in subscription.event_types
                        ):
                            WebhookDelivery.objects.get_or_create(
                                organization=event.organization,
                                subscription=subscription,
                                outbox_event=event,
                                defaults={"next_attempt_at": timezone.now()},
                            )
                    event.processed_at = timezone.now()
                    event.last_error = ""
            except Exception as exc:  # pragma: no cover - worker resilience path
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                event.available_at = timezone.now() + timedelta(seconds=min(300, 2**event.attempts))
            event.save(
                update_fields=[
                    "attempts",
                    "processed_at",
                    "last_error",
                    "available_at",
                    "updated_at",
                ]
            )
            return True

    @staticmethod
    def create_notifications(event: OutboxEvent) -> None:
        role_targets = {
            "defect.created": {"supervisor", "fleet_manager"},
            "maintenance.due": {"supervisor", "fleet_manager"},
            "receipt.posted": {"purchasing_manager"},
            "meter.reading_quarantined": {"fleet_manager", "integration_admin"},
        }.get(event.event_type, set())
        if not role_targets:
            return
        users = event.organization.users.filter(
            is_active=True, roles__slug__in=role_targets
        ).distinct()
        for user in users:
            Notification.objects.create(
                organization=event.organization,
                user=user,
                title=event.event_type.replace(".", " ").title(),
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                body=str(event.payload.get("summary", "Review the linked operational record.")),
            )

    @staticmethod
    def deliver_one() -> bool:
        global _last_delivery_organization_id
        # Keep the row lock through the request: a crashed worker rolls the claim back and
        # another worker can retry it without a lease/reaper subsystem.
        with transaction.atomic():
            due = (
                WebhookDelivery.objects.select_for_update(of=("self",), skip_locked=True)
                .select_related("subscription", "outbox_event", "organization")
                .filter(status__in=["pending", "retry"], next_attempt_at__lte=timezone.now())
                .order_by("next_attempt_at", "created_at", "pk")
            )
            delivery = None
            if _last_delivery_organization_id is not None:
                delivery = due.exclude(organization_id=_last_delivery_organization_id).first()
            delivery = delivery or due.first()
            if delivery is None:
                return False
            _last_delivery_organization_id = delivery.organization_id
            Command._deliver_locked(delivery)
            return True

    @staticmethod
    def _deliver_locked(delivery: WebhookDelivery) -> None:
        event = delivery.outbox_event
        if not delivery.subscription.active:
            delivery.status = "dead"
            delivery.delivered_at = None
            delivery.last_error = "Subscription deactivated"
            delivery.save(update_fields=["status", "delivered_at", "last_error", "updated_at"])
            return
        try:
            target = resolve_outbound_url(delivery.subscription.url)
        except ValueError as exc:
            delivery.attempts += 1
            delivery.last_error = str(exc)[:2000]
            delivery.status = "dead"
            delivery.save(update_fields=["attempts", "last_error", "status", "updated_at"])
            return
        payload = json.dumps(
            {
                "id": str(event.pk),
                "schema_version": "1.0",
                "type": event.event_type,
                "organization_id": str(event.organization_id),
                "occurred_at": event.created_at.isoformat(),
                "resource": {"type": event.resource_type, "id": event.resource_id},
                "data": redact_financial_fields(event.payload),
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(
            delivery.subscription.signing_secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        request = urllib.request.Request(  # noqa: S310 - target is validated above
            delivery.subscription.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Fleetline-Signature": f"sha256={signature}",
            },
        )
        try:
            with _open_webhook(
                request,
                target=target,
                timeout=settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS,
            ) as response:
                delivery.response_status = response.status
                if 200 <= response.status < 300:
                    delivery.status = "delivered"
                    delivery.delivered_at = timezone.now()
                elif 300 <= response.status < 400:
                    raise RuntimeError("Webhook redirects are not allowed")
                else:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:  # noqa: BLE001 - isolate a failed remote delivery from the worker
            if isinstance(exc, urllib.error.HTTPError):
                delivery.response_status = exc.code
            delivery.attempts += 1
            delivery.delivered_at = None
            delivery.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            delivery.status = "dead" if delivery.attempts >= MAX_WEBHOOK_ATTEMPTS else "retry"
            if delivery.status == "retry":
                delivery.next_attempt_at = timezone.now() + timedelta(
                    seconds=_retry_delay_seconds(delivery.attempts)
                )
        delivery.save()
