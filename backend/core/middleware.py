from collections.abc import Callable
from typing import Any

from django.contrib.auth import logout


class ActiveUserMiddleware:
    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        if request.user.is_authenticated and not request.user.is_active:
            logout(request)
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        response = self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; "
            "img-src 'self' data: blob:; connect-src 'self'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; worker-src 'self' blob:"
        )
        response["Permissions-Policy"] = "camera=(self), geolocation=(), microphone=()"
        return response
