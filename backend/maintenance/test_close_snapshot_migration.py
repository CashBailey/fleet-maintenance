from __future__ import annotations

import uuid

from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class WorkOrderCloseSnapshotMigrationTests(TransactionTestCase):
    migrate_from = [
        ("assets", "0003_asset_external_identity"),
        ("core", "0004_attachment_versions"),
        ("maintenance", "0005_maintenance_concurrency_constraints"),
    ]
    migrate_to = [
        ("assets", "0003_asset_external_identity"),
        ("maintenance", "0006_workorderclosesnapshot"),
    ]

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_closed_work_order_receives_immutable_deterministic_snapshot(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("core", "Organization")
        User = old_apps.get_model("core", "User")
        AssetType = old_apps.get_model("assets", "AssetType")
        Asset = old_apps.get_model("assets", "Asset")
        WorkOrder = old_apps.get_model("maintenance", "WorkOrder")
        WorkOrderTask = old_apps.get_model("maintenance", "WorkOrderTask")

        organization = Organization.objects.create(name="Legacy Fleet", slug="legacy-close")
        actor = User.objects.create(username="legacy-manager", organization=organization)
        asset_type = AssetType.objects.create(organization=organization, name="Truck")
        asset = Asset.objects.create(
            organization=organization, asset_type=asset_type, unit_number="LEGACY-01"
        )
        work_order_id = uuid.UUID("00000000-0000-0000-0000-000000000601")
        closed_at = timezone.now()
        work_order = WorkOrder.objects.create(
            id=work_order_id,
            organization=organization,
            number="WO-LEGACY-CLOSE",
            asset=asset,
            created_by=actor,
            assigned_to=actor,
            status="Closed",
            summary="Legacy closed work",
            completion_summary="Originally recorded result",
            completed_at=closed_at,
            completed_by=actor,
            closed_at=closed_at,
            closed_by=actor,
        )
        WorkOrderTask.objects.create(
            organization=organization,
            work_order=work_order,
            title="Legacy completed task",
            status="Completed",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        Snapshot = new_apps.get_model("maintenance", "WorkOrderCloseSnapshot")
        snapshot = Snapshot.objects.get(work_order_id=work_order_id)

        self.assertEqual(snapshot.pk, uuid.uuid5(work_order_id, "close:1"))
        self.assertEqual(snapshot.sequence, 1)
        self.assertTrue(snapshot.snapshot["legacy_backfill"])
        self.assertEqual(
            snapshot.snapshot["work_order"]["completion_summary"],
            "Originally recorded result",
        )
        self.assertEqual(snapshot.snapshot["tasks"][0]["title"], "Legacy completed task")
        self.assertIsNone(snapshot.snapshot["plan_reset_baseline"])
        with self.assertRaises(DatabaseError), transaction.atomic():
            Snapshot.objects.filter(pk=snapshot.pk).update(snapshot={"tampered": True})
