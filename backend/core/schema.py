from __future__ import annotations

import re
from typing import Any, Literal

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.generics import GenericAPIView
from rest_framework.views import APIView


class FleetlineAutoSchema(AutoSchema):
    """Use a free-form object for function views that do not declare serializers."""

    def _uses_implicit_json_contract(self) -> bool:
        view = self.view
        return (
            isinstance(view, APIView)
            and not isinstance(view, GenericAPIView)
            and not callable(getattr(view, "get_serializer", None))
            and not callable(getattr(view, "get_serializer_class", None))
            and not hasattr(view, "serializer_class")
        )

    def get_request_serializer(self) -> Any:
        if self._uses_implicit_json_contract():
            return OpenApiTypes.OBJECT
        return super().get_request_serializer()

    def get_response_serializers(self) -> Any:
        if self._uses_implicit_json_contract():
            return OpenApiTypes.OBJECT
        return super().get_response_serializers()

    def get_operation_id(self) -> str:
        path = re.sub(r"\{([^}]+)\}", r"by_\1", self.path.strip("/"))
        return re.sub(r"[^a-zA-Z0-9_]+", "_", f"{path}_{self.method.lower()}")

    def get_override_parameters(self) -> list[Any]:
        parameters = list(super().get_override_parameters())
        if self.method not in {"GET", "HEAD", "OPTIONS"}:
            parameters.append(
                OpenApiParameter(
                    name="Idempotency-Key",
                    location=OpenApiParameter.HEADER,
                    type=OpenApiTypes.UUID,
                    required=False,
                    description=(
                        "Stable UUID required by transactional endpoints; replaying the same "
                        "request returns the original logical result."
                    ),
                )
            )
        if self.path == "/api/v1/offline/sync/":
            parameters.append(
                OpenApiParameter(
                    name="X-Offline-Grant",
                    location=OpenApiParameter.HEADER,
                    type=OpenApiTypes.STR,
                    required=True,
                    description="Short-lived, user- and organization-bound offline grant.",
                )
            )
        if self.path.endswith("/telematics/autopi/v1/messages/"):
            parameters.append(
                OpenApiParameter(
                    name="X-Device-Token",
                    location=OpenApiParameter.HEADER,
                    type=OpenApiTypes.STR,
                    required=True,
                    description="Device credential; independent of payload-declared identity.",
                )
            )
        query_parameters = {
            "/api/v1/users/": ("role",),
            "/api/v1/assets/": ("q", "status", "include_archived"),
            "/api/v1/search/": ("q",),
            "/api/v1/audit-events/": ("resource_type", "resource_id"),
            "/api/v1/attachments/": ("resource_type", "resource_id"),
            "/api/v1/webhooks/deliveries/": ("status",),
            "/api/v1/inventory/parts/": ("identifier", "q", "active"),
            "/api/v1/inventory/bins/": ("warehouse_id",),
            "/api/v1/inventory/stock/": ("part_id", "bin_id"),
            "/api/v1/inventory/transactions/": ("work_order_id",),
            "/api/v1/maintenance/requests/": ("asset_id",),
            "/api/v1/maintenance/work-orders/": ("status", "asset_id", "assigned_to_me"),
        }
        for name in query_parameters.get(self.path, ()):
            parameters.append(
                OpenApiParameter(
                    name=name,
                    location=OpenApiParameter.QUERY,
                    type=OpenApiTypes.STR,
                    required=False,
                )
            )
        return parameters

    def _get_response_bodies(self, direction: Literal["request", "response"] = "response") -> Any:
        responses = super()._get_response_bodies(direction)
        error_schema = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {},
                    },
                }
            },
        }
        for status, description in {
            "400": "Invalid input or missing idempotency contract.",
            "401": "Authentication failed or an MFA challenge is required.",
            "403": "The authenticated principal is not authorized.",
            "404": "The organization-scoped resource was not found.",
            "409": "State, concurrency, or idempotency conflict.",
        }.items():
            responses.setdefault(
                status,
                {
                    "description": description,
                    "content": {"application/json": {"schema": error_schema}},
                },
            )
        return responses


class ScopedTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.ScopedTokenAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, str]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
            "description": "Fleetline scoped API token.",
        }
