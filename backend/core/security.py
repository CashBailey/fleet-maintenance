from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import struct
import time
from binascii import Error as BinasciiError
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_provisioning_uri(*, secret: str, account_name: str, issuer: str) -> str:
    label = f"{quote(issuer, safe='')}:{quote(account_name, safe='')}"
    parameters = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": 6,
            "period": 30,
        }
    )
    return f"otpauth://totp/{label}?{parameters}"


def totp(secret: str, at: int | None = None) -> str:
    counter = int((at if at is not None else time.time()) // 30)
    key = base64.b32decode(secret.upper())
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret: str, candidate: str) -> bool:
    if len(candidate) != 6 or not candidate.isascii() or not candidate.isdigit():
        return False
    now = int(time.time())
    try:
        return any(
            hmac.compare_digest(totp(secret, now + drift * 30), candidate) for drift in (-1, 0, 1)
        )
    except (BinasciiError, ValueError):
        return False


@dataclass(frozen=True)
class ResolvedOutboundURL:
    url: str
    scheme: str
    hostname: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


def resolve_outbound_url(url: str) -> ResolvedOutboundURL:
    """Validate a webhook URL and return only the IPs vetted in this resolution."""

    schemes = ["https", "http"] if settings.WEBHOOK_ALLOW_HTTP else ["https"]
    try:
        URLValidator(schemes=schemes)(url)
    except ValidationError as exc:
        raise ValueError("Webhook URL is invalid or uses a disabled scheme") from exc
    parsed = urlsplit(url)
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Webhook URL must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = tuple(
            dict.fromkeys(
                str(result[4][0])
                for result in socket.getaddrinfo(
                    parsed.hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            )
        )
    except (OSError, ValueError) as exc:
        raise ValueError("Webhook hostname or port cannot be resolved") from exc
    if not addresses:
        raise ValueError("Webhook hostname cannot be resolved")
    if not settings.WEBHOOK_ALLOW_PRIVATE_NETWORKS:
        for address in addresses:
            try:
                public = ipaddress.ip_address(address).is_global
            except ValueError as exc:
                raise ValueError("Webhook hostname resolved to an invalid address") from exc
            if not public:
                raise ValueError("Webhook URL resolves to a non-public address")
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    return ResolvedOutboundURL(
        url=url,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=port,
        request_target=request_target,
        addresses=addresses,
    )


def validate_outbound_url(url: str) -> str:
    """Reject unsafe webhook targets before storage."""

    resolve_outbound_url(url)
    return url
