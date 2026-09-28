from __future__ import annotations

import hashlib
import shutil
import tempfile

from assets.models import Asset, AssetType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Attachment, AuditEvent, Location, Organization, Role, User

TEST_PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test credential


class AttachmentVersionApiTests(TestCase):
    organization: Organization
    other_organization: Organization
    location: Location
    other_location: Location
    role: Role
    driver_role: Role
    user: User
    driver: User
    asset: Asset
    other_asset: Asset
    client: APIClient

    @classmethod
    def setUpTestData(cls) -> None:
        cls.organization = Organization.objects.create(name="Document Fleet", slug="documents")
        cls.other_organization = Organization.objects.create(
            name="Other Document Fleet", slug="other-documents"
        )
        cls.location = Location.objects.create(
            organization=cls.organization, name="Main Shop", code="MAIN"
        )
        cls.other_location = Location.objects.create(
            organization=cls.other_organization, name="Other Shop", code="MAIN"
        )
        cls.role = Role.objects.create(
            organization=cls.organization, slug="supervisor", name="Supervisor"
        )
        cls.driver_role = Role.objects.create(
            organization=cls.organization, slug="driver", name="Driver"
        )
        cls.user = User.objects.create_user(
            username="document-supervisor",
            password=TEST_PASSWORD,
            organization=cls.organization,
            default_location=cls.location,
        )
        cls.user.roles.add(cls.role)
        cls.driver = User.objects.create_user(
            username="document-driver",
            password=TEST_PASSWORD,
            organization=cls.organization,
            default_location=cls.location,
        )
        cls.driver.roles.add(cls.driver_role)
        asset_type = AssetType.objects.create(organization=cls.organization, name="Truck")
        cls.asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            assigned_driver=cls.driver,
            unit_number="DOC-100",
        )
        cls.other_asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="DOC-200",
        )

    def setUp(self) -> None:
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.media_dir = tempfile.mkdtemp(prefix="fleetline-attachment-version-test-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_dir, True)

    def _upload(
        self,
        *,
        asset: Asset,
        name: str,
        body: bytes,
        key: str,
        supersedes_id: str = "",
        category: str | None = None,
        title: str | None = None,
    ):
        payload: dict[str, object] = {
            "resource_type": "asset",
            "resource_id": str(asset.pk),
            "file": SimpleUploadedFile(name, body, content_type="text/plain"),
        }
        if supersedes_id:
            payload["supersedes_id"] = supersedes_id
        if category is not None:
            payload["category"] = category
        if title is not None:
            payload["title"] = title
        return self.client.post(
            reverse("attachments"),
            payload,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_uploading_a_new_version_preserves_and_lists_the_complete_lineage(self) -> None:
        first_body = b"registration version one"
        first_response = self._upload(
            asset=self.asset,
            name="registration-v1.txt",
            body=first_body,
            category="registration",
            title="Vehicle registration",
            key="00000000-0000-0000-0000-000000000701",
        )
        self.assertEqual(first_response.status_code, 201)
        first_payload = first_response.json()["attachment"]
        self.assertEqual(first_payload["version"], 1)
        self.assertIsNone(first_payload["supersedes_id"])

        second_body = b"registration version two"
        second_response = self._upload(
            asset=self.asset,
            name="registration-v2.txt",
            body=second_body,
            supersedes_id=first_payload["id"],
            key="00000000-0000-0000-0000-000000000702",
        )
        self.assertEqual(second_response.status_code, 201)
        second_payload = second_response.json()["attachment"]
        self.assertEqual(second_payload["document_key"], first_payload["document_key"])
        self.assertEqual(second_payload["version"], 2)
        self.assertEqual(second_payload["supersedes_id"], first_payload["id"])
        self.assertEqual(second_payload["category"], "registration")
        self.assertEqual(second_payload["title"], "Vehicle registration")

        first = Attachment.objects.get(pk=first_payload["id"])
        second = Attachment.objects.get(pk=second_payload["id"])
        self.assertNotEqual(first.file.name, second.file.name)
        with first.file.open("rb") as stored_first:
            self.assertEqual(stored_first.read(), first_body)
        with second.file.open("rb") as stored_second:
            self.assertEqual(stored_second.read(), second_body)
        first_download = self.client.get(reverse("attachment-download", args=[first.pk]))
        self.assertEqual(first_download.status_code, 200)
        self.assertEqual(b"".join(first_download.streaming_content), first_body)  # type: ignore[attr-defined]

        listed = self.client.get(
            reverse("attachments"),
            {"resource_type": "asset", "resource_id": str(self.asset.pk)},
        )
        self.assertEqual(listed.status_code, 200)
        lineage = listed.json()["attachments"]
        self.assertEqual([row["version"] for row in lineage], [1, 2])
        self.assertEqual([row["document_key"] for row in lineage], [str(first.document_key)] * 2)
        self.assertEqual(lineage[0]["superseded_by_id"], str(second.pk))
        self.assertFalse(lineage[0]["is_current"])
        self.assertIsNone(lineage[1]["superseded_by_id"])
        self.assertTrue(lineage[1]["is_current"])

        self.assertEqual(
            AuditEvent.objects.filter(
                action="attachment.created", resource_id=str(second.pk)
            ).count(),
            1,
        )
        superseded = AuditEvent.objects.get(
            action="attachment.superseded", resource_id=str(first.pk)
        )
        self.assertEqual(superseded.actor, self.user)
        self.assertEqual(superseded.previous_state, "1")
        self.assertEqual(superseded.new_state, "2")
        self.assertEqual(superseded.context["superseded_by_id"], str(second.pk))
        self.assertEqual(superseded.correlation_id, "00000000-0000-0000-0000-000000000702")

        replay = self._upload(
            asset=self.asset,
            name="registration-v2.txt",
            body=second_body,
            supersedes_id=first_payload["id"],
            key="00000000-0000-0000-0000-000000000702",
        )
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json()["attachment"]["id"], str(second.pk))
        self.assertEqual(
            Attachment.objects.filter(document_key=first.document_key).count(),
            2,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action="attachment.superseded", resource_id=str(first.pk)
            ).count(),
            1,
        )

    def test_supersession_rejects_a_different_target_tenant_or_noncurrent_version(self) -> None:
        first_response = self._upload(
            asset=self.asset,
            name="permit-v1.txt",
            body=b"permit one",
            key="00000000-0000-0000-0000-000000000711",
        )
        first_id = first_response.json()["attachment"]["id"]

        malformed = self._upload(
            asset=self.asset,
            name="bad-version.txt",
            body=b"bad",
            supersedes_id="not-a-uuid",
            key="00000000-0000-0000-0000-000000000716",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "invalid_supersedes_id")

        wrong_target = self._upload(
            asset=self.other_asset,
            name="permit-v2.txt",
            body=b"permit two",
            supersedes_id=first_id,
            key="00000000-0000-0000-0000-000000000712",
        )
        self.assertEqual(wrong_target.status_code, 409)
        self.assertEqual(wrong_target.json()["error"]["code"], "attachment_target_mismatch")

        other_attachment = Attachment.objects.create(
            organization=self.other_organization,
            uploader=User.objects.create_user(
                username="other-document-user",
                password=TEST_PASSWORD,
                organization=self.other_organization,
                default_location=self.other_location,
            ),
            resource_type="asset",
            resource_id="00000000-0000-0000-0000-000000000799",
            file=SimpleUploadedFile("other.txt", b"other", content_type="text/plain"),
            original_name="other.txt",
            content_type="text/plain",
            size=5,
            sha256=hashlib.sha256(b"other").hexdigest(),
        )
        cross_tenant = self._upload(
            asset=self.asset,
            name="cross-tenant.txt",
            body=b"forbidden",
            supersedes_id=str(other_attachment.pk),
            key="00000000-0000-0000-0000-000000000713",
        )
        self.assertEqual(cross_tenant.status_code, 404)

        second_response = self._upload(
            asset=self.asset,
            name="permit-v2.txt",
            body=b"permit two",
            supersedes_id=first_id,
            key="00000000-0000-0000-0000-000000000714",
        )
        self.assertEqual(second_response.status_code, 201)
        fork = self._upload(
            asset=self.asset,
            name="permit-v2-fork.txt",
            body=b"fork",
            supersedes_id=first_id,
            key="00000000-0000-0000-0000-000000000715",
        )
        self.assertEqual(fork.status_code, 409)
        self.assertEqual(fork.json()["error"]["code"], "attachment_not_current")
        self.assertEqual(Attachment.objects.filter(organization=self.organization).count(), 2)

    def test_hidden_successor_does_not_make_an_old_version_look_current(self) -> None:
        self.client.force_authenticate(self.driver)
        first = self._upload(
            asset=self.asset,
            name="driver-document-v1.txt",
            body=b"driver version",
            key="00000000-0000-0000-0000-000000000717",
        ).json()["attachment"]

        self.client.force_authenticate(self.user)
        second = self._upload(
            asset=self.asset,
            name="supervisor-document-v2.txt",
            body=b"supervisor version",
            supersedes_id=first["id"],
            key="00000000-0000-0000-0000-000000000718",
        )
        self.assertEqual(second.status_code, 201)

        self.client.force_authenticate(self.driver)
        visible = self.client.get(
            reverse("attachments"),
            {"resource_type": "asset", "resource_id": str(self.asset.pk)},
        ).json()["attachments"]
        self.assertEqual([row["id"] for row in visible], [first["id"]])
        self.assertFalse(visible[0]["is_current"])
        self.assertIsNone(visible[0]["superseded_by_id"])


class AttachmentImmutabilityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self) -> None:
        self.media_dir = tempfile.mkdtemp(prefix="fleetline-attachment-immutability-test-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_dir, True)
        organization = Organization.objects.create(name="Immutable Fleet", slug="immutable")
        location = Location.objects.create(organization=organization, name="Main Shop", code="MAIN")
        role = Role.objects.create(organization=organization, slug="driver", name="Driver")
        uploader = User.objects.create_user(
            username="immutable-uploader",
            password=TEST_PASSWORD,
            organization=organization,
            default_location=location,
        )
        uploader.roles.add(role)
        self.operation_id = "00000000-0000-0000-0000-000000000721"
        self.attachment = Attachment.objects.create(
            organization=organization,
            uploader=uploader,
            resource_type="defect",
            resource_id=self.operation_id,
            title="Brake photo",
            file=SimpleUploadedFile("brake.txt", b"original", content_type="text/plain"),
            original_name="brake.txt",
            content_type="text/plain",
            size=8,
            sha256=hashlib.sha256(b"original").hexdigest(),
        )

    def test_database_rejects_mutation_and_delete_but_allows_one_staged_link(self) -> None:
        with self.assertRaises(DatabaseError), transaction.atomic():
            Attachment.objects.filter(pk=self.attachment.pk).update(title="tampered")

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core_attachment SET sha256 = %s, version = 9 WHERE id = %s",
                ["f" * 64, self.attachment.pk],
            )

        target_id = "00000000-0000-0000-0000-000000000722"
        updated = Attachment.objects.filter(pk=self.attachment.pk).update(
            resource_type="Defect", resource_id=target_id
        )
        self.assertEqual(updated, 1)
        self.attachment.refresh_from_db()
        self.assertEqual(
            (self.attachment.resource_type, self.attachment.resource_id),
            ("Defect", target_id),
        )
        self.assertEqual(self.attachment.title, "Brake photo")
        self.assertEqual(self.attachment.sha256, hashlib.sha256(b"original").hexdigest())

        with self.assertRaises(DatabaseError), transaction.atomic():
            Attachment.objects.filter(pk=self.attachment.pk).update(
                resource_type="Defect",
                resource_id="00000000-0000-0000-0000-000000000723",
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Attachment.objects.filter(pk=self.attachment.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_attachment WHERE id = %s", [self.attachment.pk])

        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.resource_id, target_id)


class AttachmentVersionMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0003_api_token_expiry")]
    migrate_to = [("core", "0004_attachment_versions")]

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_attachments_receive_distinct_document_keys_on_reapply(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldOrganization = old_apps.get_model("core", "Organization")
        OldUser = old_apps.get_model("core", "User")
        OldAttachment = old_apps.get_model("core", "Attachment")
        organization = OldOrganization.objects.create(name="Legacy Fleet", slug="legacy-docs")
        uploader = OldUser.objects.create(username="legacy-uploader", organization=organization)
        for number in (1, 2):
            OldAttachment.objects.create(
                organization=organization,
                uploader=uploader,
                resource_type="asset",
                resource_id="00000000-0000-0000-0000-000000000731",
                file=f"legacy/document-{number}.txt",
                original_name=f"document-{number}.txt",
                content_type="text/plain",
                size=number,
                sha256=str(number) * 64,
            )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        NewAttachment = new_apps.get_model("core", "Attachment")
        migrated = list(NewAttachment.objects.order_by("original_name"))
        self.assertEqual(len(migrated), 2)
        self.assertEqual(len({row.document_key for row in migrated}), 2)
        self.assertEqual([row.title for row in migrated], [row.original_name for row in migrated])
        self.assertEqual([row.version for row in migrated], [1, 1])
