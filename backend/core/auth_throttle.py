from __future__ import annotations

import hashlib
import hmac
import ipaddress
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.request import Request

from .models import LoginAttemptThrottle


def _key_hash(dimension: str, value: str) -> str:
    payload = f"fleetline-login-throttle\0{dimension}\0{value}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


def _client_address(request: Request) -> str:
    value = str(request.META.get("REMOTE_ADDR", "")).strip()
    if settings.TRUST_PROXY_HEADERS:
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).strip()
        if forwarded:
            # The trusted edge appends the address it observed at the right edge.
            value = forwarded.rsplit(",", 1)[-1].strip()
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return "unknown"


def _lock_row(dimension: str, value: str, now: datetime) -> LoginAttemptThrottle:
    row, _ = LoginAttemptThrottle.objects.select_for_update().get_or_create(
        dimension=dimension,
        key_hash=_key_hash(dimension, value),
        defaults={"window_started_at": now},
    )
    return row


@dataclass
class LoginAttemptGate:
    client: LoginAttemptThrottle
    rows: tuple[LoginAttemptThrottle, ...]
    account: LoginAttemptThrottle | None
    now: datetime

    @property
    def blocked(self) -> bool:
        return any(
            row.locked_until is not None and row.locked_until > self.now for row in self.rows
        )

    @property
    def client_blocked(self) -> bool:
        return self.client.locked_until is not None and self.client.locked_until > self.now

    @property
    def retry_after_seconds(self) -> int | None:
        if not self.client_blocked or self.client.locked_until is None:
            return None
        return max(1, int((self.client.locked_until - self.now).total_seconds()))

    def _record_row_failure(self, row: LoginAttemptThrottle) -> None:
        window = timedelta(seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS)
        lockout = timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
        if row.window_started_at > self.now or row.window_started_at <= self.now - window:
            row.window_started_at = self.now
            row.failure_count = 0
        row.failure_count += 1
        if row.failure_count >= settings.LOGIN_ATTEMPT_LIMIT:
            row.locked_until = self.now + lockout
        row.save(
            update_fields=[
                "window_started_at",
                "failure_count",
                "locked_until",
                "updated_at",
            ]
        )

    def record_failure(self) -> None:
        for row in self.rows:
            self._record_row_failure(row)

    def record_blocked_account_attempt(self) -> None:
        """Keep unknown and locked-account behavior equivalent at the client boundary."""

        self._record_row_failure(self.client)

    def record_success(self) -> None:
        if self.account is None:
            return
        self.account.failure_count = 0
        self.account.window_started_at = self.now
        self.account.locked_until = None
        self.account.save(
            update_fields=[
                "window_started_at",
                "failure_count",
                "locked_until",
                "updated_at",
            ]
        )


def _reset_elapsed(row: LoginAttemptThrottle, now: datetime) -> None:
    window = timedelta(seconds=settings.LOGIN_ATTEMPT_WINDOW_SECONDS)
    if row.locked_until is not None and row.locked_until > now:
        return
    if (
        row.locked_until is not None
        or row.window_started_at > now
        or row.window_started_at <= now - window
    ):
        row.failure_count = 0
        row.window_started_at = now
        row.locked_until = None
        row.save(
            update_fields=[
                "window_started_at",
                "failure_count",
                "locked_until",
                "updated_at",
            ]
        )


@contextmanager
def login_attempt_gate(request: Request, account_key: str | None) -> Iterator[LoginAttemptGate]:
    """Serialize and bound password/TOTP failures for both account and client."""

    now = timezone.now()
    with transaction.atomic():
        client = _lock_row(
            LoginAttemptThrottle.Dimension.CLIENT,
            _client_address(request),
            now,
        )
        _reset_elapsed(client, now)
        if client.locked_until is not None and client.locked_until > now:
            yield LoginAttemptGate(client=client, rows=(client,), account=None, now=now)
            return

        if account_key is None:
            yield LoginAttemptGate(client=client, rows=(client,), account=None, now=now)
            return
        account = _lock_row(
            LoginAttemptThrottle.Dimension.ACCOUNT,
            account_key,
            now,
        )
        _reset_elapsed(account, now)
        yield LoginAttemptGate(client=client, rows=(client, account), account=account, now=now)
