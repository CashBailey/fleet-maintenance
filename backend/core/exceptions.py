from __future__ import annotations

from typing import Any

from rest_framework.response import Response
from rest_framework.views import exception_handler


class DomainError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_operation",
        status: int = 400,
        details: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, DomainError):
        response = Response(
            {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            status=exc.status,
        )
        if exc.code == "login_throttled" and isinstance(exc.details, dict):
            retry_after = exc.details.get("retry_after_seconds")
            if isinstance(retry_after, int) and retry_after > 0:
                response["Retry-After"] = str(retry_after)
        return response
    response = exception_handler(exc, context)
    if response is None:
        return None
    detail = response.data
    message = (
        detail.get("detail", "Request failed") if isinstance(detail, dict) else "Request failed"
    )
    response.data = {
        "error": {
            "code": getattr(exc, "default_code", None)
            or {
                401: "not_authenticated",
                403: "permission_denied",
                404: "not_found",
                405: "method_not_allowed",
            }.get(response.status_code, "request_failed"),
            "message": str(message),
            "details": detail,
        }
    }
    return response
