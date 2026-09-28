from __future__ import annotations

import hashlib

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiToken
from .permissions import bind_token_auth


class ScopedTokenAuthentication(BaseAuthentication):
    keyword = b"Bearer"

    def authenticate(self, request: object) -> tuple[object, ApiToken] | None:
        parts = get_authorization_header(request).split()
        if not parts or parts[0] != self.keyword:
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid bearer token header")
        digest = hashlib.sha256(parts[1]).hexdigest()
        try:
            token = ApiToken.objects.select_related("user", "organization").get(token_hash=digest)
        except ApiToken.DoesNotExist as exc:
            raise AuthenticationFailed("Invalid API token") from exc
        now = timezone.now()
        if (
            token.revoked_at
            or not token.expires_at
            or token.expires_at <= now
            or not token.user.is_active
            or token.user.organization_id != token.organization_id
        ):
            raise AuthenticationFailed("Expired or revoked API token")
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
        bind_token_auth(token.user, token)
        return token.user, token
