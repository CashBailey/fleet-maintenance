from __future__ import annotations

from typing import Any

from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class OpenApiContractTests(SimpleTestCase):
    def test_schema_covers_versioned_modules_and_authentication(self) -> None:
        schema: dict[str, Any] = SchemaGenerator().get_schema(request=None, public=True)

        self.assertEqual(schema["openapi"], "3.0.3")
        self.assertEqual(schema["info"]["version"], "1.0.0")

        paths = schema["paths"]
        expected_paths = {
            "/api/v1/bootstrap/",
            "/api/v1/assets/",
            "/api/v1/assets/external/{source_system}/{external_id}/",
            "/api/v1/assets/external/{source_system}/{external_id}/meters/",
            "/api/v1/assets/{asset_id}/components/",
            "/api/v1/assets/components/{component_id}/",
            "/api/v1/assets/components/{component_id}/remove/",
            "/api/v1/maintenance/work-orders/",
            "/api/v1/inventory/parts/",
            "/api/v1/purchasing/purchase-orders/",
            "/api/v1/integrations/devices/",
        }
        self.assertTrue(expected_paths.issubset(paths))
        self.assertTrue(all(path.startswith("/api/v1/") for path in paths))

        security_schemes = schema["components"]["securitySchemes"]
        self.assertEqual(security_schemes["cookieAuth"]["in"], "cookie")
        self.assertEqual(security_schemes["cookieAuth"]["name"], "fleetline_sessionid")
        self.assertEqual(
            security_schemes["bearerAuth"],
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": "Fleetline scoped API token.",
            },
        )

        assets_get = paths["/api/v1/assets/"]["get"]
        self.assertIn({"cookieAuth": []}, assets_get["security"])
        self.assertIn({"bearerAuth": []}, assets_get["security"])
        response_schema = assets_get["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response_schema["type"], "object")

        operations = [
            operation
            for path in paths.values()
            for method, operation in path.items()
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        operation_ids = [operation["operationId"] for operation in operations]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        for operation in operations:
            self.assertTrue({"400", "401", "403", "404", "409"}.issubset(operation["responses"]))

        for path in paths.values():
            for method in {"post", "put", "patch", "delete"} & path.keys():
                parameter_names = {
                    parameter["name"] for parameter in path[method].get("parameters", [])
                }
                self.assertIn("Idempotency-Key", parameter_names)

        search_parameters = {row["name"] for row in paths["/api/v1/search/"]["get"]["parameters"]}
        self.assertIn("q", search_parameters)
        offline_parameters = {
            row["name"] for row in paths["/api/v1/offline/sync/"]["post"]["parameters"]
        }
        self.assertIn("X-Offline-Grant", offline_parameters)
        autopi_parameters = {
            row["name"]
            for row in paths["/api/v1/integrations/telematics/autopi/v1/messages/"]["post"][
                "parameters"
            ]
        }
        self.assertIn("X-Device-Token", autopi_parameters)
