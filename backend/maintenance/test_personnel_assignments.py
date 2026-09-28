from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from assets.models import Asset, AssetType
from core.models import ApiToken, AuditEvent, Comment, Organization, Role, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from inventory.models import Bin, Part, Warehouse
from inventory.services import receive_stock
from rest_framework.test import APIClient

from .models import ExternalEmployeeProjection, WorkOrder, WorkOrderAssignment, WorkOrderTask
from .services import create_work_order, is_active_work_order_assignee

ASSIGNMENT_IDENTITY_FIELDS = (
    "display_name",
    "source_system",
    "external_employee_id",
    "source_version",
)


class PersonnelProjectionApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Personnel Fleet", slug="personnel")
        self.other_organization = Organization.objects.create(
            name="Other Personnel Fleet", slug="personnel-other"
        )
        self.integration_user = self._user(self.organization, "gatorhub-sync", "integration_admin")
        self.manager = self._user(self.organization, "personnel-manager", "fleet_manager")
        self.other_integration_user = self._user(
            self.other_organization, "other-gatorhub-sync", "integration_admin"
        )

    def _user(self, organization: Organization, username: str, role_slug: str) -> User:
        role, _ = Role.objects.get_or_create(
            organization=organization,
            slug=role_slug,
            defaults={"name": role_slug.replace("_", " ").title()},
        )
        user = User.objects.create_user(username=username, organization=organization)
        user.roles.add(role)
        return user

    def _token_client(
        self, user: User | None = None, *, scopes: list[str] | None = None
    ) -> APIClient:
        user = user or self.integration_user
        assert user.organization is not None
        raw = f"flt_personnel_{uuid.uuid4().hex}"
        ApiToken.objects.create(
            organization=user.organization,
            user=user,
            name="GatorHub personnel synchronization",
            prefix=raw[:12],
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            scopes=scopes if scopes is not None else ["personnel.sync"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    def _payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "display_name": "Avery Technician",
            "active": True,
            "source_version": "employee-v1",
            "source_updated_at": timezone.now().replace(microsecond=0).isoformat(),
            "external_user_id": "gatorhub-login-42",
            "job_title": "Technician",
            "department": "Maintenance",
        }
        payload.update(overrides)
        return payload

    def _upsert(
        self,
        client: APIClient,
        payload: dict[str, object],
        *,
        employee_id: str = "employee-42",
        key: str | None = None,
    ) -> Any:
        return client.put(
            reverse("external-employee-detail", args=["gatorhub", employee_id]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_scoped_sync_is_idempotent_audited_and_tenant_scoped(self) -> None:
        client = self._token_client()
        payload = self._payload()
        key = str(uuid.uuid4())

        created = self._upsert(client, payload, key=key)
        replay = self._upsert(client, payload, key=key)
        self.assertEqual((created.status_code, replay.status_code), (201, 201))
        self.assertTrue(created.data["created"])
        self.assertTrue(created.data["changed"])
        self.assertNotIn("external_user_id", created.data["employee"])

        employee = ExternalEmployeeProjection.objects.get(
            organization=self.organization,
            source_system="gatorhub",
            external_employee_id="employee-42",
        )
        self.assertEqual(employee.external_user_id, "gatorhub-login-42")
        self.assertEqual(employee.display_name, "Avery Technician")
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.organization,
                action="personnel.external_upserted",
                resource_id=str(employee.pk),
            ).count(),
            1,
        )

        canonical_replay = self._upsert(client, payload)
        self.assertEqual(canonical_replay.status_code, 200)
        self.assertFalse(canonical_replay.data["created"])
        self.assertFalse(canonical_replay.data["changed"])

        stale = self._upsert(
            client,
            self._payload(
                source_version="employee-v0",
                source_updated_at=(timezone.now() - timedelta(days=1)).isoformat(),
            ),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data["error"]["code"], "stale_external_employee")

        manager_client = APIClient()
        manager_client.force_authenticate(self.manager)
        no_token = self._upsert(manager_client, self._payload())
        self.assertEqual(no_token.status_code, 403)
        self.assertEqual(no_token.data["error"]["code"], "api_token_required")

        missing_scope = self._upsert(
            self._token_client(scopes=["assets.sync"]), self._payload(), employee_id="employee-43"
        )
        self.assertEqual(missing_scope.status_code, 403)
        self.assertEqual(missing_scope.data["error"]["code"], "permission_denied")
        self.assertFalse(
            ExternalEmployeeProjection.objects.filter(
                organization=self.organization, external_employee_id="employee-43"
            ).exists()
        )

        other = self._upsert(
            self._token_client(self.other_integration_user),
            self._payload(display_name="Other Avery"),
        )
        self.assertEqual(other.status_code, 201)
        self.assertEqual(
            ExternalEmployeeProjection.objects.filter(
                source_system="gatorhub", external_employee_id="employee-42"
            ).count(),
            2,
        )

        employees = manager_client.get(reverse("external-employees"))
        self.assertEqual(employees.status_code, 200)
        self.assertEqual(
            [row["display_name"] for row in employees.data["employees"]], ["Avery Technician"]
        )


class WorkOrderAssignmentApiTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Assignment Fleet", slug="assignments")
        self.asset_type = AssetType.objects.create(organization=self.organization, name="Truck")
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=self.asset_type,
            unit_number="ASSIGN-01",
        )
        self.manager = self._user("assignment-manager", "fleet_manager")
        self.lead = self._user("assignment-lead", "technician")
        self.teammate = self._user("assignment-teammate", "technician")
        self.outsider = self._user("assignment-outsider", "driver")
        self.external_employee = ExternalEmployeeProjection.objects.create(
            organization=self.organization,
            source_system="gatorhub",
            external_employee_id="employee-external-7",
            display_name="Jordan External Technician",
            active=True,
            source_version="v1",
            source_updated_at=timezone.now(),
        )
        self.work_order = create_work_order(
            organization=self.organization,
            actor=self.manager,
            asset=self.asset,
            assigned_to=self.lead,
            summary="Replace damaged air line",
        )
        self.task = WorkOrderTask.objects.create(
            organization=self.organization,
            work_order=self.work_order,
            title="Inspect and replace air line",
            sequence=1,
        )

    def _user(self, username: str, role_slug: str) -> User:
        role, _ = Role.objects.get_or_create(
            organization=self.organization,
            slug=role_slug,
            defaults={"name": role_slug.replace("_", " ").title()},
        )
        user = User.objects.create_user(username=username, organization=self.organization)
        user.roles.add(role)
        return user

    def _client(self, user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _assign(self, payload: dict[str, object], *, key: str | None = None) -> Any:
        return self._client(self.manager).post(
            reverse("work-order-assignments", args=[self.work_order.pk]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_manager_assigns_team_while_legacy_lead_and_my_work_remain_compatible(self) -> None:
        initial = WorkOrderAssignment.objects.get(work_order=self.work_order, sequence=1)
        self.assertEqual(
            (initial.local_user, initial.role, initial.action),
            (
                self.lead,
                "lead",
                "assigned",
            ),
        )
        self.assertEqual(self.work_order.to_dict()["assigned_to_id"], str(self.lead.pk))

        payload = {
            "base_version": self.work_order.version,
            "reason": "Assign lead and supporting technician",
            "assignees": [
                {"user_id": str(self.lead.pk), "role": "lead"},
                {"user_id": str(self.teammate.pk), "role": "technician"},
                {
                    "source_system": "gatorhub",
                    "external_employee_id": "employee-external-7",
                    "role": "technician",
                },
            ],
        }
        key = str(uuid.uuid4())
        assigned = self._assign(payload, key=key)
        replay = self._assign(payload, key=key)
        self.assertEqual((assigned.status_code, replay.status_code), (200, 200))
        self.assertEqual(WorkOrderAssignment.objects.filter(work_order=self.work_order).count(), 3)
        self.assertEqual(len(assigned.data["work_order"]["assignees"]), 3)

        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.assigned_to_id, self.lead.pk)
        self.assertTrue(is_active_work_order_assignee(self.work_order, self.teammate))
        self.assertTrue(is_active_work_order_assignee(self.work_order, self.lead))
        self.assertEqual(
            AuditEvent.objects.filter(
                action="work_order.assignments_changed", resource_id=str(self.work_order.pk)
            ).count(),
            1,
        )

        teammate_client = self._client(self.teammate)
        my_work = teammate_client.get(f"{reverse('work-orders')}?mine=true")
        self.assertEqual(my_work.status_code, 200)
        self.assertEqual(
            [row["id"] for row in my_work.data["work_orders"]], [str(self.work_order.pk)]
        )
        detail = teammate_client.get(reverse("work-order-detail", args=[self.work_order.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.data["work_order"]["assignment_history"]), 3)

        self.work_order.refresh_from_db()
        task_update = teammate_client.patch(
            reverse("work-order-task", args=[self.work_order.pk, self.task.pk]),
            {
                "base_version": self.work_order.version,
                "status": "Completed",
                "notes": "Verified the replacement fitting is secure.",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(task_update.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_by_id, self.teammate.pk)

        self.work_order.refresh_from_db()
        updated = teammate_client.patch(
            reverse("work-order-detail", args=[self.work_order.pk]),
            {"base_version": self.work_order.version, "diagnosis": "Leak isolated at rear axle."},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(updated.status_code, 200)

        self.work_order.refresh_from_db()
        bootstrap = teammate_client.get(reverse("bootstrap"))
        self.assertEqual(bootstrap.status_code, 200)
        offline_grant = bootstrap.data["offline_grant"]
        operation = {
            "operation_id": str(uuid.uuid4()),
            "type": "work_note.create",
            "payload": {
                "work_order_id": str(self.work_order.pk),
                "body": "Team note recorded while disconnected.",
            },
        }
        offline_note = teammate_client.post(
            reverse("offline-sync"),
            {"operations": [operation]},
            format="json",
            HTTP_X_OFFLINE_GRANT=offline_grant,
        )
        replayed_note = teammate_client.post(
            reverse("offline-sync"),
            {"operations": [operation]},
            format="json",
            HTTP_X_OFFLINE_GRANT=offline_grant,
        )
        self.assertEqual((offline_note.status_code, replayed_note.status_code), (200, 200))
        self.assertEqual(offline_note.data["results"][0]["status"], "synced")
        self.assertEqual(
            Comment.objects.filter(
                organization=self.organization,
                resource_type="WorkOrder",
                resource_id=str(self.work_order.pk),
            ).count(),
            1,
        )

        # Team membership follows the same authorization path for bootstrap,
        # search, comments/attachments, and issued work-order parts.
        self.assertIn(
            str(self.work_order.pk),
            [item["id"] for item in bootstrap.data["work_orders"]],
        )
        searched = teammate_client.get(reverse("search"), {"q": "damaged air"})
        self.assertEqual(searched.status_code, 200)
        self.assertIn(
            str(self.work_order.pk),
            [item["id"] for item in searched.data["results"] if item["type"] == "work_order"],
        )
        attachment = teammate_client.post(
            reverse("attachments"),
            {
                "resource_type": "work_order",
                "resource_id": str(self.work_order.pk),
                "file": SimpleUploadedFile("team-note.txt", b"Team evidence", "text/plain"),
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(attachment.status_code, 201)
        comment = teammate_client.post(
            reverse("comments"),
            {
                "resource_type": "work_order",
                "resource_id": str(self.work_order.pk),
                "body": "Second technician verified the repair.",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(comment.status_code, 201)

        from core.models import Location

        location = Location.objects.create(
            organization=self.organization, name="Parts Yard", code="PARTS"
        )
        warehouse = Warehouse.objects.create(
            organization=self.organization, location=location, code="CAGE", name="Parts cage"
        )
        stock_bin = Bin.objects.create(
            organization=self.organization, warehouse=warehouse, code="A-01"
        )
        part = Part.objects.create(
            organization=self.organization, number="TEAM-PART", name="Team repair part"
        )
        receive_stock(
            organization=self.organization,
            actor=self.manager,
            part=part,
            bin=stock_bin,
            quantity="2",
            unit_cost="1",
            operation_id=uuid.uuid4(),
        )
        issue = teammate_client.post(
            reverse("issues"),
            {
                "part_id": str(part.pk),
                "bin_id": str(stock_bin.pk),
                "work_order_id": str(self.work_order.pk),
                "quantity": "1",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(issue.status_code, 201)
        visible_issues = teammate_client.get(reverse("issues"))
        self.assertEqual(visible_issues.status_code, 200)
        self.assertEqual(
            [item["id"] for item in visible_issues.data["transactions"]],
            [issue.data["transaction"]["id"]],
        )

        removed = self._assign(
            {
                "base_version": self.work_order.version,
                "reason": "Support work complete",
                "assignees": [
                    {"user_id": str(self.lead.pk), "role": "lead"},
                    {
                        "source_system": "gatorhub",
                        "external_employee_id": "employee-external-7",
                        "role": "technician",
                    },
                ],
            }
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(WorkOrderAssignment.objects.filter(work_order=self.work_order).count(), 4)
        self.work_order.refresh_from_db()
        self.assertFalse(is_active_work_order_assignee(self.work_order, self.teammate))
        denied_detail = teammate_client.get(reverse("work-order-detail", args=[self.work_order.pk]))
        self.assertEqual(denied_detail.status_code, 404)
        denied_offline_note = teammate_client.post(
            reverse("offline-sync"),
            {
                "operations": [
                    {
                        "operation_id": str(uuid.uuid4()),
                        "type": "work_note.create",
                        "payload": {
                            "work_order_id": str(self.work_order.pk),
                            "body": "This must no longer be accepted.",
                        },
                    }
                ]
            },
            format="json",
            HTTP_X_OFFLINE_GRANT=offline_grant,
        )
        self.assertEqual(denied_offline_note.status_code, 200)
        self.assertEqual(denied_offline_note.data["results"][0]["status"], "rejected")
        self.assertEqual(denied_offline_note.data["results"][0]["code"], "permission_denied")
        self.assertEqual(teammate_client.get(reverse("issues")).data["transactions"], [])
        self.assertEqual(
            teammate_client.get(
                reverse("attachments"),
                {"resource_type": "work_order", "resource_id": str(self.work_order.pk)},
            ).status_code,
            403,
        )
        self.assertEqual(
            teammate_client.get(
                reverse("comments"),
                {"resource_type": "work_order", "resource_id": str(self.work_order.pk)},
            ).status_code,
            403,
        )

    def test_assignment_history_retains_identity_snapshots_after_profile_changes(self) -> None:
        initial = WorkOrderAssignment.objects.get(work_order=self.work_order, sequence=1)
        self.assertEqual(initial.to_history_dict()["display_name"], "assignment-lead")

        # Local identity display is retained too, while the durable user FK is
        # still used for authorization and active-team projection.
        self.lead.first_name = "Renamed"
        self.lead.last_name = "Lead"
        self.lead.save(update_fields=["first_name", "last_name"])
        initial.refresh_from_db()
        self.assertEqual(initial.to_history_dict()["display_name"], "assignment-lead")

        assigned = self._assign(
            {
                "base_version": self.work_order.version,
                "assignees": [
                    {"user_id": str(self.lead.pk), "role": "lead"},
                    {
                        "source_system": "gatorhub",
                        "external_employee_id": "employee-external-7",
                        "role": "technician",
                    },
                ],
            }
        )
        self.assertEqual(assigned.status_code, 200)
        external_assignment = WorkOrderAssignment.objects.get(
            work_order=self.work_order,
            external_employee=self.external_employee,
            action=WorkOrderAssignment.Action.ASSIGNED,
        )
        self.assertEqual(
            external_assignment.to_history_dict(),
            {
                **external_assignment.to_dict(),
                "action": "assigned",
                "sequence": 2,
                "reason": "",
                "assigned_by_id": str(self.manager.pk),
                "assigned_by": "assignment-manager",
            },
        )
        self.assertEqual(
            {
                field: external_assignment.to_history_dict()[field]
                for field in ASSIGNMENT_IDENTITY_FIELDS
            },
            {
                "display_name": "Jordan External Technician",
                "source_system": "gatorhub",
                "external_employee_id": "employee-external-7",
                "source_version": "v1",
            },
        )

        # GatorHub may update its current personnel projection without
        # retroactively changing this assignment evidence.
        self.external_employee.display_name = "Jordan Renamed Technician"
        self.external_employee.source_version = "v2"
        self.external_employee.save(update_fields=["display_name", "source_version", "updated_at"])
        historical = self._client(self.manager).get(
            reverse("work-order-detail", args=[self.work_order.pk])
        )
        self.assertEqual(historical.status_code, 200)
        history = historical.data["work_order"]["assignment_history"]
        assigned_history = next(row for row in history if row["id"] == str(external_assignment.pk))
        self.assertEqual(
            {field: assigned_history[field] for field in ASSIGNMENT_IDENTITY_FIELDS},
            {
                "display_name": "Jordan External Technician",
                "source_system": "gatorhub",
                "external_employee_id": "employee-external-7",
                "source_version": "v1",
            },
        )

        # A removal is a compensating event and retains the original assignment
        # identity, rather than snapshotting the renamed current profile.
        self.work_order.refresh_from_db()
        removed = self._assign(
            {
                "base_version": self.work_order.version,
                "assignees": [{"user_id": str(self.lead.pk), "role": "lead"}],
            }
        )
        self.assertEqual(removed.status_code, 200)
        removal = WorkOrderAssignment.objects.get(
            work_order=self.work_order,
            external_employee=self.external_employee,
            action=WorkOrderAssignment.Action.UNASSIGNED,
        )
        self.assertEqual(
            {field: removal.to_history_dict()[field] for field in ASSIGNMENT_IDENTITY_FIELDS},
            {
                "display_name": "Jordan External Technician",
                "source_system": "gatorhub",
                "external_employee_id": "employee-external-7",
                "source_version": "v1",
            },
        )

        # A fresh assignment explicitly captures the new source-owned version.
        self.work_order.refresh_from_db()
        reassigned = self._assign(
            {
                "base_version": self.work_order.version,
                "assignees": [
                    {"user_id": str(self.lead.pk), "role": "lead"},
                    {
                        "source_system": "gatorhub",
                        "external_employee_id": "employee-external-7",
                        "role": "technician",
                    },
                ],
            }
        )
        self.assertEqual(reassigned.status_code, 200)
        replacement = WorkOrderAssignment.objects.get(
            work_order=self.work_order,
            external_employee=self.external_employee,
            action=WorkOrderAssignment.Action.ASSIGNED,
            sequence=4,
        )
        self.assertEqual(
            (replacement.subject_display_name, replacement.subject_source_version),
            ("Jordan Renamed Technician", "v2"),
        )

    def test_legacy_primary_assignment_patch_appends_events_and_replaces_lead(self) -> None:
        client = self._client(self.manager)
        response = client.patch(
            reverse("work-order-detail", args=[self.work_order.pk]),
            {"base_version": self.work_order.version, "assigned_to_id": str(self.teammate.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.assigned_to_id, self.teammate.pk)
        active = self.work_order.active_assignments()
        self.assertEqual(
            [(event.local_user_id, event.role) for event in active], [(self.teammate.pk, "lead")]
        )
        self.assertEqual(
            list(
                WorkOrderAssignment.objects.filter(work_order=self.work_order).values_list(
                    "action", "sequence"
                )
            ),
            [("assigned", 1), ("unassigned", 2), ("assigned", 3)],
        )

    def test_assignment_endpoint_rejects_unauthorized_and_invalid_team_shapes(self) -> None:
        denied = self._client(self.lead).post(
            reverse("work-order-assignments", args=[self.work_order.pk]),
            {"base_version": self.work_order.version, "assignees": []},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(denied.status_code, 403)

        multiple_leads = self._assign(
            {
                "base_version": self.work_order.version,
                "assignees": [
                    {"user_id": str(self.lead.pk), "role": "lead"},
                    {"user_id": str(self.teammate.pk), "role": "lead"},
                ],
            }
        )
        self.assertEqual(multiple_leads.status_code, 400)
        self.assertEqual(multiple_leads.data["error"]["code"], "multiple_assignment_leads")

        self.external_employee.active = False
        self.external_employee.save(update_fields=["active", "updated_at"])
        inactive = self._assign(
            {
                "base_version": self.work_order.version,
                "assignees": [
                    {
                        "source_system": "gatorhub",
                        "external_employee_id": "employee-external-7",
                        "role": "technician",
                    }
                ],
            }
        )
        self.assertEqual(inactive.status_code, 400)
        self.assertEqual(inactive.data["error"]["code"], "inactive_assignee")

        outsider = self._client(self.outsider).get(
            reverse("work-order-detail", args=[self.work_order.pk])
        )
        self.assertEqual(outsider.status_code, 403)

    def test_stale_assignment_submission_creates_no_additional_events(self) -> None:
        base_version = self.work_order.version
        first = self._assign(
            {
                "base_version": base_version,
                "assignees": [
                    {"user_id": str(self.lead.pk), "role": "lead"},
                    {"user_id": str(self.teammate.pk), "role": "technician"},
                ],
            }
        )
        self.assertEqual(first.status_code, 200)
        event_count = WorkOrderAssignment.objects.filter(work_order=self.work_order).count()

        stale = self._assign(
            {
                "base_version": base_version,
                "assignees": [{"user_id": str(self.lead.pk), "role": "lead"}],
            }
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data["error"]["code"], "sync_conflict")
        self.assertEqual(
            WorkOrderAssignment.objects.filter(work_order=self.work_order).count(), event_count
        )


class WorkOrderAssignmentModelTests(TestCase):
    def test_assignment_events_reject_model_updates(self) -> None:
        organization = Organization.objects.create(
            name="Immutable Assignment", slug="immutable-assignment"
        )
        asset_type = AssetType.objects.create(organization=organization, name="Truck")
        asset = Asset.objects.create(
            organization=organization,
            asset_type=asset_type,
            unit_number="IMMUTABLE-01",
        )
        role = Role.objects.create(organization=organization, slug="fleet_manager", name="Manager")
        user = User.objects.create_user(username="immutable-manager", organization=organization)
        user.roles.add(role)
        work_order = create_work_order(
            organization=organization,
            actor=user,
            asset=asset,
            assigned_to=user,
            summary="Immutable assignment evidence",
        )
        event = WorkOrderAssignment.objects.get(work_order=work_order)
        event.reason = "Changed after the fact"
        with self.assertRaises(ValidationError):
            event.save()

        legacy = WorkOrder.objects.create(
            organization=organization,
            number="WO-IMPORTED-LEGACY",
            asset=asset,
            created_by=user,
            assigned_to=user,
            summary="Imported work order awaiting assignment migration",
        )
        self.assertFalse(WorkOrderAssignment.objects.filter(work_order=legacy).exists())
        assignee = legacy.to_dict()["assignees"][0]
        self.assertEqual((assignee["user_id"], assignee["role"]), (str(user.pk), "lead"))
        self.assertTrue(assignee["legacy_projection"])
