from __future__ import annotations

import io
from typing import Any

from django.conf import settings
from rest_framework.exceptions import APIException
from rest_framework.parsers import JSONParser


class TelematicsPayloadTooLarge(APIException):
    status_code = 413
    default_detail = "Telematics payload exceeds the configured size limit"
    default_code = "telematics_payload_too_large"


class BoundedTelematicsJSONParser(JSONParser):
    """Read at most the configured device-message limit before decoding JSON."""

    def parse(
        self,
        stream: Any,
        media_type: str | None = None,
        parser_context: dict[str, Any] | None = None,
    ) -> Any:
        raw = stream.read(settings.TELEMATICS_MAX_BODY_BYTES + 1)
        if len(raw) > settings.TELEMATICS_MAX_BODY_BYTES:
            raise TelematicsPayloadTooLarge()
        return super().parse(io.BytesIO(raw), media_type, parser_context)
