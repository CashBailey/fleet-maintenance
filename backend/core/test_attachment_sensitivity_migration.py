from __future__ import annotations

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AttachmentSensitivityMigrationTests(TransactionTestCase):
    """Commercial legacy files fail closed until a financial reviewer classifies them."""

    migrate_from = [("core", "0009_document_workflow_and_attachment_lineage_guards")]
    migrate_to = [("core", "0011_document_attachment_sensitivity_guard")]

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_commercial_attachments_become_financial(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("core", "Organization")
        User = old_apps.get_model("core", "User")
        Attachment = old_apps.get_model("core", "Attachment")
        organization = Organization.objects.create(
            name="Legacy Attachment Fleet", slug="legacy-att"
        )
        user = User.objects.create(username="legacy-attachment-user", organization=organization)

        ids = {
            resource_type: Attachment.objects.create(
                organization=organization,
                uploader=user,
                resource_type=resource_type,
                resource_id=str(uuid.uuid4()),
                file="attachments/legacy.txt",
                original_name="legacy.txt",
                content_type="text/plain",
                size=6,
                sha256="a" * 64,
            ).pk
            for resource_type in ("Part", "Vendor", "purchase-order", "Receipt", "Asset", "Defect")
        }

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        Attachment = new_apps.get_model("core", "Attachment")
        classified = {
            resource_type: Attachment.objects.get(pk=attachment_id).sensitivity
            for resource_type, attachment_id in ids.items()
        }
        self.assertEqual(
            {key: classified[key] for key in ("Part", "Vendor", "purchase-order", "Receipt")},
            {key: "financial" for key in ("Part", "Vendor", "purchase-order", "Receipt")},
        )
        self.assertEqual(
            {key: classified[key] for key in ("Asset", "Defect")},
            {key: "operational" for key in ("Asset", "Defect")},
        )
