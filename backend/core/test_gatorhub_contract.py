from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from inspect import getsource
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from assets.models import Asset, AssetType, Meter, MeterReading, normalize_external_identity
from assets.services import change_asset_status
from assets.views import _external_asset_fields
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse
from maintenance.models import (
    ExternalEmployeeProjection,
    MaintenancePlan,
    MaintenanceTrigger,
    WorkOrderAssignment,
    normalize_external_employee_identity,
)
from maintenance.services import _recalculate_plans, transition_work_order
from maintenance.views import _external_employee_payload

from .permissions import ROLE_PERMISSIONS

CONTRACT_PATH = Path(settings.PROJECT_DIR) / "contracts" / "gatorhub-fleetline-v1.json"


def _schema_value(root: dict[str, Any], reference: str) -> dict[str, Any]:
    value: Any = root
    for part in reference.removeprefix("#/").split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _matches_schema(root: dict[str, Any], schema: dict[str, Any], value: Any) -> bool:
    try:
        _assert_schema(root, schema, value)
    except (AssertionError, TypeError, ValueError):
        return False
    return True


def _assert_schema(root: dict[str, Any], schema: dict[str, Any], value: Any) -> None:
    """Validate the small Draft 2020-12 subset used by this checked-in contract."""
    if "$ref" in schema:
        _assert_schema(root, _schema_value(root, schema["$ref"]), value)
    for item in schema.get("allOf", []):
        _assert_schema(root, item, value)
    if "anyOf" in schema:
        assert any(_matches_schema(root, item, value) for item in schema["anyOf"])
    if "oneOf" in schema:
        assert sum(_matches_schema(root, item, value) for item in schema["oneOf"]) == 1
    if "not" in schema:
        assert not _matches_schema(root, schema["not"], value)

    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        checks = {
            "object": lambda candidate: isinstance(candidate, dict),
            "array": lambda candidate: isinstance(candidate, list),
            "string": lambda candidate: isinstance(candidate, str),
            "integer": lambda candidate: (
                isinstance(candidate, int) and not isinstance(candidate, bool)
            ),
            "number": lambda candidate: (
                isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
            ),
            "boolean": lambda candidate: isinstance(candidate, bool),
            "null": lambda candidate: candidate is None,
        }
        assert any(checks[item](value) for item in expected_types)
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]

    if isinstance(value, dict):
        required = set(schema.get("required", []))
        assert required.issubset(value)
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(value).issubset(properties)
        for name, child in properties.items():
            if name in value:
                _assert_schema(root, child, value[name])
        for name, dependencies in schema.get("dependentRequired", {}).items():
            if name in value:
                assert set(dependencies).issubset(value)
        if "maxProperties" in schema:
            assert len(value) <= schema["maxProperties"]
        if names := schema.get("propertyNames"):
            for name in value:
                _assert_schema(root, names, name)
    if isinstance(value, list):
        if "minItems" in schema:
            assert len(value) >= schema["minItems"]
        if "maxItems" in schema:
            assert len(value) <= schema["maxItems"]
        if item_schema := schema.get("items"):
            for item in value:
                _assert_schema(root, item_schema, item)
    if isinstance(value, str):
        if "minLength" in schema:
            assert len(value) >= schema["minLength"]
        if "maxLength" in schema:
            assert len(value) <= schema["maxLength"]
        if "pattern" in schema:
            assert re.search(schema["pattern"], value)
        if schema.get("format") == "uuid":
            UUID(value)
        if schema.get("format") == "date-time":
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            assert value >= schema["minimum"]
        if "maximum" in schema:
            assert value <= schema["maximum"]


class GatorHubContractTests(SimpleTestCase):
    contract: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_identity_and_version_are_canonical(self) -> None:
        self.assertEqual(self.contract["contract_id"], "gatorhub-fleetline")
        self.assertRegex(self.contract["version"], re.compile(r"^1\.\d+\.\d+$"))
        self.assertEqual(self.contract["version"], "1.3.0")
        self.assertEqual(self.contract["authority"], "fleetline")
        self.assertEqual(self.contract["source_system"], "gatorhub")

        identity = self.contract["identifiers"]["vehicle"]
        self.assertEqual(identity["source_field"], "Vehicle.id")
        self.assertEqual(identity["target_external_id_field"], "Asset.external_id")
        self.assertIn("Vehicle.truck_no", identity["never_identity_fields"])

        employee_identity = self.contract["identifiers"]["employee"]
        self.assertEqual(employee_identity["source_field"], "Employee.id")
        self.assertEqual(employee_identity["source_format"], "uuid")
        self.assertEqual(
            employee_identity["target_external_id_field"],
            "ExternalEmployeeProjection.external_employee_id",
        )
        self.assertIn("User.id", employee_identity["never_identity_fields"])

    def test_contract_routes_and_token_scopes_match_runtime(self) -> None:
        transport = self.contract["transport"]
        self.assertEqual(
            transport["asset_upsert"]["path"],
            "/api/v1/assets/external/{source_system}/{external_id}/",
        )
        self.assertEqual(
            transport["meter_ingest"]["path"],
            "/api/v1/assets/external/{source_system}/{external_id}/meters/",
        )
        self.assertEqual(
            transport["personnel_projection_upsert"]["path"],
            "/api/v1/maintenance/personnel/external/{source_system}/{external_employee_id}/",
        )
        self.assertEqual(
            reverse("asset-external", args=["gatorhub", "vehicle-42"]),
            "/api/v1/assets/external/gatorhub/vehicle-42/",
        )
        self.assertEqual(
            reverse("asset-external-meters", args=["gatorhub", "vehicle-42"]),
            "/api/v1/assets/external/gatorhub/vehicle-42/meters/",
        )
        employee_id = "12121212-1212-4121-8121-121212121212"
        self.assertEqual(
            reverse("external-employee-detail", args=["gatorhub", employee_id]),
            f"/api/v1/maintenance/personnel/external/gatorhub/{employee_id}/",
        )
        work_order_id = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(
            reverse("work-order-assignments", args=[work_order_id]),
            f"/api/v1/maintenance/work-orders/{work_order_id}/assignments/",
        )
        integration_permissions = ROLE_PERMISSIONS["integration_admin"]
        self.assertTrue(set(transport["required_scopes"]).issubset(integration_permissions))
        self.assertEqual(
            transport["required_scopes_semantics"],
            "union_of_enabled_gatorhub_sync_operations_not_required_on_every_token",
        )
        self.assertEqual(
            transport["personnel_projection_upsert"]["required_scope"], "personnel.sync"
        )
        self.assertTrue(transport["personnel_projection_upsert"]["requires_api_token"])
        self.assertFalse(transport["work_order_assignment_replace"]["requires_api_token"])
        self.assertEqual(
            transport["work_order_assignment_replace"]["required_permission"],
            "maintenance.manage",
        )

        topology = transport["service_token_topology"]
        self.assertEqual(topology["issuer"], "fleetline")
        self.assertEqual(topology["holder"], "gatorhub_server_only")
        self.assertIn("gatorhub_human_jwt", topology["forbidden_substitutes"])
        self.assertNotIn("maintenance.manage", transport["required_scopes"])

    def test_status_and_role_targets_match_fleetline_enums(self) -> None:
        projections = self.contract["status_mappings"]["fleetline_to_gatorhub"]
        self.assertEqual(set(projections), set(Asset.Status.values))

        known_roles = set(ROLE_PERMISSIONS)
        self.assertTrue(set(self.contract["role_hints"].values()).issubset(known_roles))
        self.assertTrue(
            set(self.contract["roles_never_inferred_from_gatorhub"]).issubset(known_roles)
        )
        personnel_policy = self.contract["personnel_authorization_policy"]
        self.assertFalse(personnel_policy["projection_creates_fleetline_login"])
        self.assertFalse(personnel_policy["projection_grants_fleetline_role"])
        self.assertFalse(personnel_policy["gatorhub_role_hints_are_authorization"])

    def test_webhook_and_event_vocabulary_is_stable(self) -> None:
        webhook = self.contract["transport"]["webhook"]
        self.assertEqual(webhook["schema_version"], "1.0")
        self.assertEqual(webhook["signature_header"], "X-Fleetline-Signature")
        self.assertEqual(webhook["signature_algorithm"], "hmac-sha256")
        self.assertEqual(webhook["deduplication_field"], "id")
        event_types = set(self.contract["events"]["fleetline_to_gatorhub"])
        self.assertEqual(
            event_types,
            {
                "asset.availability_changed",
                "maintenance.due",
                "maintenance.plan_projection_changed",
                "work_order.completed",
            },
        )
        emitters = "\n".join(
            getsource(function)
            for function in (change_asset_status, _recalculate_plans, transition_work_order)
        )
        for event_type in event_types:
            self.assertIn(f'"{event_type}"', emitters)
        projection = self.contract["events"]["payloads"]["maintenance.plan_projection_changed"]
        self.assertEqual(projection["schema_version"], "1.0")
        self.assertEqual(projection["resource_type"], "MaintenancePlan")
        self.assertEqual(
            set(projection["emitted_when"]),
            {
                "maintenance_plan_created",
                "due_status_changed",
                "after_successful_pm_trigger_reset_on_work_order_close",
            },
        )
        self.assertEqual(
            set(projection["nullable_fields"]),
            {"source_system", "external_id", "work_order_id"},
        )
        self.assertEqual(
            set(projection["required_fields"]),
            {
                "schema_version",
                "asset_id",
                "source_system",
                "external_id",
                "work_order_id",
                "maintenance_plan_id",
                "due_status",
                "next_due",
                "calculated_at",
            },
        )

    def test_embedded_wire_examples_match_their_json_schemas(self) -> None:
        schemas = self.contract["schemas"]
        exercised = set()
        for name, schema in schemas.items():
            for example in schema.get("examples", []):
                with self.subTest(schema=name):
                    _assert_schema(self.contract, schema, example)
                exercised.add(name)
        self.assertEqual(
            exercised,
            {
                "ExternalAssetIdentityParameters",
                "ExternalEmployeeIdentityParameters",
                "ExternalEmployeePutRequest",
                "ExternalEmployeePutResponse",
                "ExternalAssetPutRequest",
                "ExternalAssetPutResponse",
                "ExternalMeterPostRequest",
                "ExternalMeterPostResponse",
                "ErrorEnvelope",
                "MaintenancePlanProjectionChangedWebhook",
                "WorkOrderAssignmentReplaceRequest",
            },
        )

        projection = deepcopy(schemas["MaintenancePlanProjectionChangedWebhook"]["examples"][0])
        projection["data"]["work_order_id"] = None
        projection["data"]["due_status"] = "Due"
        projection["data"]["next_due"] = [
            {
                "trigger_id": "99999999-9999-4999-8999-999999999999",
                "kind": "mileage",
                "status": "Due",
                "message": "Last completion baseline is unknown; initial service is due",
                "unit": "mi",
            }
        ]
        _assert_schema(
            self.contract,
            schemas["MaintenancePlanProjectionChangedWebhook"],
            projection,
        )

    def test_wire_schemas_match_runtime_fields_limits_and_enums(self) -> None:
        self.assertEqual(
            self.contract["json_schema_dialect"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        schemas = self.contract["schemas"]
        transport = self.contract["transport"]
        operation_scopes = {
            "asset_upsert": "assets.sync",
            "meter_ingest": "assets.sync",
            "personnel_projection_upsert": "personnel.sync",
        }
        for operation_name, expected_scope in operation_scopes.items():
            operation = transport[operation_name]
            self.assertTrue(operation["requires_api_token"])
            self.assertEqual(operation["required_scope"], expected_scope)
            for reference in [
                operation["path_schema"],
                operation["request_schema"],
                *operation["responses"].values(),
            ]:
                self.assertIsInstance(_schema_value(self.contract, reference), dict)

        asset_request = schemas["ExternalAssetPutRequest"]
        asset_properties = asset_request["properties"]
        self.assertNotIn("initial_status", asset_properties)
        self.assertNotIn("status", asset_properties)
        self.assertNotIn("ownership", asset_properties)
        self.assertEqual(asset_properties["source_details"]["maxProperties"], 32)
        self.assertEqual(asset_properties["source_details"]["x-max-serialized-bytes"], 8192)
        for name in ("unit_number", "vin", "serial_number", "make", "model"):
            self.assertEqual(
                asset_properties[name]["maxLength"],
                cast(Any, Asset._meta.get_field(name)).max_length,
            )
        self.assertEqual(
            asset_properties["asset_type"]["maxLength"],
            AssetType._meta.get_field("name").max_length,
        )
        self.assertEqual(
            schemas["ExternalAssetIdentityParameters"]["properties"]["source_system"]["maxLength"],
            Asset._meta.get_field("source_system").max_length,
        )
        identity_schema = schemas["ExternalAssetIdentityParameters"]
        self.assertEqual(identity_schema["properties"]["source_system"]["const"], "gatorhub")
        self.assertEqual(identity_schema["properties"]["external_id"]["format"], "uuid")
        source, identifier = normalize_external_identity(**identity_schema["examples"][0])
        self.assertEqual((source, identifier), ("gatorhub", identifier))

        personnel_identity = schemas["ExternalEmployeeIdentityParameters"]
        self.assertEqual(personnel_identity["properties"]["source_system"]["const"], "gatorhub")
        self.assertEqual(
            personnel_identity["properties"]["source_system"]["maxLength"],
            ExternalEmployeeProjection._meta.get_field("source_system").max_length,
        )
        self.assertEqual(
            personnel_identity["properties"]["external_employee_id"]["maxLength"],
            ExternalEmployeeProjection._meta.get_field("external_employee_id").max_length,
        )
        source, employee_id = normalize_external_employee_identity(
            **personnel_identity["examples"][0]
        )
        self.assertEqual((source, employee_id), ("gatorhub", employee_id))

        personnel_request = schemas["ExternalEmployeePutRequest"]
        personnel_properties = personnel_request["properties"]
        self.assertEqual(
            set(personnel_request["required"]),
            {"display_name", "active", "source_version", "source_updated_at"},
        )
        for name in (
            "display_name",
            "source_version",
            "external_user_id",
            "job_title",
            "department",
        ):
            self.assertEqual(
                personnel_properties[name]["maxLength"],
                cast(Any, ExternalEmployeeProjection._meta.get_field(name)).max_length,
            )
        personnel_source = getsource(_external_employee_payload)
        for name in personnel_properties:
            self.assertIn(f'"{name}"', personnel_source)
        self.assertEqual(
            set(schemas["ExternalEmployeeProjection"]["properties"]),
            {
                "id",
                "source_system",
                "external_employee_id",
                "display_name",
                "job_title",
                "department",
                "active",
                "source_version",
                "source_updated_at",
                "synced_at",
            },
        )

        assignment_request = schemas["WorkOrderAssignmentReplaceRequest"]
        self.assertEqual(assignment_request["x-max-leads"], 1)
        self.assertEqual(
            set(
                assignment_request["properties"]["assignees"]["items"]["properties"]["role"]["enum"]
            ),
            set(WorkOrderAssignment.Role.values),
        )
        self.assertEqual(
            assignment_request["properties"]["reason"]["maxLength"],
            WorkOrderAssignment._meta.get_field("reason").max_length,
        )
        assignment_history = self.contract["work_order_assignment_history"]
        self.assertEqual(
            set(assignment_history["assignment_time_snapshot_fields"]),
            {
                "subject_display_name",
                "subject_source_system",
                "subject_external_employee_id",
                "subject_source_version",
            },
        )
        for name in assignment_history["assignment_time_snapshot_fields"]:
            self.assertIsNotNone(WorkOrderAssignment._meta.get_field(name))
        assignment_serializer = getsource(WorkOrderAssignment.to_dict)
        for name in assignment_history["serialized_snapshot_fields"]:
            self.assertIn(f'"{name}"', assignment_serializer)
        self.assertTrue(assignment_history["unassignment_uses_original_assignment_snapshot"])
        self.assertEqual(
            assignment_history["legacy_backfill"],
            "migration_0008_freezes_best_available_identity_at_migration_time",
        )

        source_code = getsource(_external_asset_fields)
        for name in asset_properties:
            self.assertIn(f'"{name}"', source_code)
        meter_request = schemas["ExternalMeterPostRequest"]
        self.assertEqual(
            meter_request["x-external-id-uniqueness"], "organization_and_source_system"
        )
        meter_source = (settings.PROJECT_DIR / "backend/assets/views.py").read_text(
            encoding="utf-8"
        )
        for name in meter_request["properties"]:
            self.assertIn(f'"{name}"', meter_source)
        self.assertEqual(set(meter_request["properties"]["kind"]["enum"]), set(Meter.Kind.values))
        self.assertEqual(
            meter_request["properties"]["name"]["maxLength"],
            Meter._meta.get_field("name").max_length,
        )
        value_schema = meter_request["properties"]["value"]["oneOf"][1]
        value_field = MeterReading._meta.get_field("value")
        self.assertEqual(value_schema["x-decimal-max-digits"], value_field.max_digits)
        self.assertEqual(value_schema["x-decimal-places"], value_field.decimal_places)
        self.assertEqual(
            set(schemas["MeterReading"]["properties"]["quality"]["enum"]),
            set(MeterReading.Quality.values),
        )

        event_types = set(self.contract["events"]["fleetline_to_gatorhub"])
        self.assertEqual(set(schemas["WebhookEnvelope"]["properties"]["type"]["enum"]), event_types)
        projection = schemas["MaintenancePlanProjectionChangedData"]
        self.assertEqual(
            set(projection["properties"]["due_status"]["enum"]),
            set(dict(MaintenancePlan.DUE_STATUSES)),
        )
        self.assertEqual(
            set(projection["properties"]["next_due"]["items"]["properties"]["kind"]["enum"]),
            set(dict(MaintenanceTrigger.KINDS)),
        )
        manifest_projection = self.contract["events"]["payloads"][
            "maintenance.plan_projection_changed"
        ]
        self.assertEqual(set(projection["required"]), set(manifest_projection["required_fields"]))
        self.assertEqual(
            set(projection["properties"]["next_due"]["items"]["properties"]),
            set(manifest_projection["next_due_fields"]),
        )

    def test_wire_schemas_reject_unsafe_or_incomplete_fixtures(self) -> None:
        schemas = self.contract["schemas"]
        asset_request = deepcopy(schemas["ExternalAssetPutRequest"]["examples"][0])
        asset_request["initial_status"] = "OutOfService"
        self.assertFalse(
            _matches_schema(self.contract, schemas["ExternalAssetPutRequest"], asset_request)
        )

        meter_request = deepcopy(schemas["ExternalMeterPostRequest"]["examples"][0])
        meter_request.pop("kind")
        meter_request.pop("unit")
        self.assertFalse(
            _matches_schema(self.contract, schemas["ExternalMeterPostRequest"], meter_request)
        )

        employee_request = deepcopy(schemas["ExternalEmployeePutRequest"]["examples"][0])
        employee_request["email"] = "must-not-cross@example.com"
        self.assertFalse(
            _matches_schema(
                self.contract,
                schemas["ExternalEmployeePutRequest"],
                employee_request,
            )
        )
        employee_request.pop("email")
        employee_request.pop("source_updated_at")
        self.assertFalse(
            _matches_schema(
                self.contract,
                schemas["ExternalEmployeePutRequest"],
                employee_request,
            )
        )

        asset_response = deepcopy(schemas["ExternalAssetPutResponse"]["examples"][0])
        asset_response.pop("schedule_link")
        self.assertFalse(
            _matches_schema(self.contract, schemas["ExternalAssetPutResponse"], asset_response)
        )

        webhook = deepcopy(schemas["MaintenancePlanProjectionChangedWebhook"]["examples"][0])
        webhook["type"] = "maintenance.unknown"
        self.assertFalse(
            _matches_schema(
                self.contract,
                schemas["MaintenancePlanProjectionChangedWebhook"],
                webhook,
            )
        )

    def test_personnel_readiness_does_not_claim_live_gatorhub_sync(self) -> None:
        readiness = self.contract["personnel_sync_readiness"]
        self.assertFalse(readiness["live_gatorhub_sync"])
        self.assertEqual(
            readiness["status"],
            "fleetline_boundary_implemented_gatorhub_adapter_not_implemented",
        )
        self.assertEqual(
            {blocker["id"] for blocker in readiness["upstream_blockers"]},
            {
                "gatorhub_outbound_adapter",
                "approved_eligibility_mapping",
                "complete_change_watermark",
                "explicit_inactivation_delivery",
                "production_secret_provisioning",
            },
        )
        logical_operations = {
            item["logical_type"]: item for item in self.contract["events"]["gatorhub_to_fleetline"]
        }
        self.assertEqual(
            logical_operations["employee.projection_upsert"]["availability"],
            "fleetline_receiver_only_until_gatorhub_adapter_is_implemented",
        )
