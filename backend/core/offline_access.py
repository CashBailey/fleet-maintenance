from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn

from django.conf import settings
from django.core import signing
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .exceptions import DomainError
from .models import ApiToken, User

OFFLINE_GRANT_SALT = "fleetline.offline-access.v1"
OFFLINE_GRANT_VERSION = 1
OFFLINE_GRANT_MAX_LENGTH = 4096


@dataclass(frozen=True)
class OfflineAccessGrant:
    token: str
    issued_at: datetime
    expires_at: datetime


def issue_offline_access_grant(
    user: User, *, issued_at: datetime | None = None
) -> OfflineAccessGrant:
    if user.organization_id is None:
        raise DomainError(
            "Offline access requires an organization",
            code="invalid_offline_grant",
            status=403,
        )
    issued_at = issued_at or timezone.now()
    if timezone.is_naive(issued_at):
        raise ValueError("issued_at must be timezone-aware")
    expires_at = issued_at + timedelta(hours=settings.OFFLINE_CACHE_HOURS)
    payload = {
        "version": OFFLINE_GRANT_VERSION,
        "user_id": str(user.pk),
        "organization_id": str(user.organization_id),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return OfflineAccessGrant(
        token=signing.dumps(payload, salt=OFFLINE_GRANT_SALT, compress=True),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def validate_offline_access_grant(
    token: str | None,
    *,
    user: User,
    auth: object | None = None,
    now: datetime | None = None,
) -> None:
    """Require a current human-session grant for an offline mutation."""

    if isinstance(auth, ApiToken):
        raise DomainError(
            "Offline access grants cannot be used with API tokens",
            code="invalid_offline_grant",
            status=403,
        )
    if not token:
        raise DomainError(
            "X-Offline-Grant is required",
            code="offline_grant_required",
            status=403,
        )
    if len(token) > OFFLINE_GRANT_MAX_LENGTH:
        _invalid_grant()
    try:
        payload = signing.loads(token, salt=OFFLINE_GRANT_SALT)
    except signing.BadSignature:
        _invalid_grant()
    if not isinstance(payload, dict):
        _invalid_grant()

    organization_id = user.organization_id
    if (
        payload.get("version") != OFFLINE_GRANT_VERSION
        or payload.get("user_id") != str(user.pk)
        or organization_id is None
        or payload.get("organization_id") != str(organization_id)
    ):
        _invalid_grant()

    issued_at = _grant_datetime(payload.get("issued_at"))
    expires_at = _grant_datetime(payload.get("expires_at"))
    checked_at = now or timezone.now()
    if timezone.is_naive(checked_at):
        raise ValueError("now must be timezone-aware")
    if issued_at > checked_at or expires_at <= issued_at:
        _invalid_grant()
    if expires_at <= checked_at:
        raise DomainError(
            "Offline access has expired",
            code="offline_access_expired",
            status=403,
        )

    revoked_at = (
        User.objects.filter(pk=user.pk).values_list("offline_access_revoked_at", flat=True).get()
    )
    if revoked_at is not None and issued_at <= revoked_at:
        raise DomainError(
            "Offline access has been revoked",
            code="offline_access_revoked",
            status=403,
        )


def _grant_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        _invalid_grant()
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        _invalid_grant()
    return parsed


def _invalid_grant() -> NoReturn:
    raise DomainError(
        "Offline access grant is invalid",
        code="invalid_offline_grant",
        status=403,
    )
