from __future__ import annotations

import uuid

from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class WorkOrderAssignmentMigrationTests(TransactionTestCase):
    """Exercise the assignment migration from a real pre-team work-order row."""

    migrate_from = [
        ("assets", "0003_asset_external_identity"),
        ("core", "0006_allow_staged_attachment_expiry"),
        ("maintenance", "0006_workorderclosesnapshot"),
    ]
    migrate_to = [
        ("assets", "0003_asset_external_identity"),
        ("core", "0006_allow_staged_attachment_expiry"),
        ("maintenance", "0008_work_order_assignment_identity_snapshots"),
    ]

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_primary_assignee_is_backfilled_as_immutable_lead_evidence(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("core", "Organization")
        User = old_apps.get_model("core", "User")
        AssetType = old_apps.get_model("assets", "AssetType")
        Asset = old_apps.get_model("assets", "Asset")
        WorkOrder = old_apps.get_model("maintenance", "WorkOrder")

        organization = Organization.objects.create(
            name="Legacy Assignment Fleet", slug="legacy-team"
        )
        lead = User.objects.create(username="legacy-team-lead", organization=organization)
        asset_type = AssetType.objects.create(organization=organization, name="Truck")
        asset = Asset.objects.create(
            organization=organization,
            asset_type=asset_type,
            unit_number="LEGACY-TEAM-01",
        )
        work_order_id = uuid.UUID("00000000-0000-0000-0000-000000000701")
        WorkOrder.objects.create(
            id=work_order_id,
            organization=organization,
            number="WO-LEGACY-TEAM",
            asset=asset,
            created_by=lead,
            assigned_to=lead,
            summary="Legacy single-assignee work",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        Assignment = new_apps.get_model("maintenance", "WorkOrderAssignment")
        event = Assignment.objects.get(work_order_id=work_order_id)

        self.assertEqual(event.pk, uuid.uuid5(work_order_id, "legacy-lead-assignment"))
        self.assertEqual(event.local_user_id, lead.pk)
        self.assertEqual((event.role, event.action, event.sequence), ("lead", "assigned", 1))
        self.assertEqual(event.reason, "Migrated from legacy assigned_to")
        self.assertEqual(event.subject_display_name, "legacy-team-lead")
        self.assertEqual(event.subject_source_system, "")
        self.assertEqual(event.subject_external_employee_id, "")
        self.assertEqual(event.subject_source_version, "")
        User = new_apps.get_model("core", "User")
        User.objects.filter(pk=lead.pk).update(first_name="Renamed", last_name="Lead")
        event.refresh_from_db()
        self.assertEqual(event.subject_display_name, "legacy-team-lead")
        with self.assertRaises(DatabaseError), transaction.atomic():
            Assignment.objects.filter(pk=event.pk).update(reason="silently altered")
