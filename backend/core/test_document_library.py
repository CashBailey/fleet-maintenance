from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import patch

from assets.models import Asset, AssetType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from .document_library import (
    DocumentProcessingError,
    ExtractedPage,
    Extraction,
    _extract_pdf_pages,
    _ocr_page,
    _ocr_runtime_ready,
    _render_ocr_image,
    ocr_available,
)
from .management.commands.runworker import Command
from .models import (
    ApiToken,
    Attachment,
    AuditEvent,
    Document,
    DocumentApplicability,
    DocumentPage,
    Location,
    Organization,
    OutboxEvent,
    Role,
    User,
)

TEST_PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test credential


def _embedded_pdf(text: str) -> bytes:
    """Produce a small valid PDF without a document-generation dependency."""

    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 12 Tf\n72 720 Td\n({escaped}) Tj\nET\n".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(value)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(result)


def _blocked_pdf_extractor(*_: object) -> Extraction:
    """A fork-inherited extractor fixture for deadline enforcement."""

    time.sleep(5)
    return Extraction(pages=[])


class DocumentLibraryApiTests(TestCase):
    organization: ClassVar[Organization]
    other_organization: ClassVar[Organization]
    location: ClassVar[Location]
    other_location: ClassVar[Location]
    manager_role: ClassVar[Role]
    technician_role: ClassVar[Role]
    parts_role: ClassVar[Role]
    other_technician_role: ClassVar[Role]
    manager: ClassVar[User]
    technician: ClassVar[User]
    parts_clerk: ClassVar[User]
    outsider: ClassVar[User]
    asset: ClassVar[Asset]
    matching_asset: ClassVar[Asset]

    @classmethod
    def setUpTestData(cls) -> None:
        cls.organization = Organization.objects.create(name="Manual Fleet", slug="manual-fleet")
        cls.other_organization = Organization.objects.create(
            name="Other Manual Fleet", slug="other-manual-fleet"
        )
        cls.location = Location.objects.create(
            organization=cls.organization, name="Main Shop", code="MAIN"
        )
        cls.other_location = Location.objects.create(
            organization=cls.other_organization, name="Other Shop", code="MAIN"
        )
        cls.manager_role = Role.objects.create(
            organization=cls.organization, slug="fleet_manager", name="Fleet Manager"
        )
        cls.technician_role = Role.objects.create(
            organization=cls.organization, slug="technician", name="Technician"
        )
        cls.parts_role = Role.objects.create(
            organization=cls.organization, slug="parts_clerk", name="Parts Clerk"
        )
        cls.other_technician_role = Role.objects.create(
            organization=cls.other_organization, slug="technician", name="Technician"
        )
        cls.manager = cls._user("manual-manager", cls.organization, cls.location, cls.manager_role)
        cls.technician = cls._user(
            "manual-technician", cls.organization, cls.location, cls.technician_role
        )
        cls.parts_clerk = cls._user("manual-parts", cls.organization, cls.location, cls.parts_role)
        cls.outsider = cls._user(
            "manual-outsider",
            cls.other_organization,
            cls.other_location,
            cls.other_technician_role,
        )
        asset_type = AssetType.objects.create(organization=cls.organization, name="Truck")
        cls.asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="MAN-100",
            make="Freightliner",
            model="M2",
            specs={"equipment": {"engine": {"type": "Cummins ISB"}}},
        )
        cls.matching_asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="MAN-101",
            make="Freightliner",
            model="M2",
            specs={"equipment": {"engine": {"type": "Cummins ISB"}}},
        )

    @classmethod
    def _user(
        cls, username: str, organization: Organization, location: Location, role: Role
    ) -> User:
        user = User.objects.create_user(
            username=username,
            password=TEST_PASSWORD,
            organization=organization,
            default_location=location,
        )
        user.roles.add(role)
        return user

    def setUp(self) -> None:
        self.client = APIClient()
        cast(APIClient, self.client).force_authenticate(self.manager)
        self.media_dir = tempfile.mkdtemp(prefix="fleetline-document-library-test-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_dir, True)

    def _upload(self, key: str, **fields: object):
        payload: dict[str, object] = {
            "asset_id": str(self.asset.pk),
            "title": "M2 Brake Service Manual",
            "category": "service_manual",
            "manufacturer": "Freightliner",
            "model": "M2",
            "engine_type": "Cummins ISB",
            "file": SimpleUploadedFile(
                "m2-brakes.pdf",
                b"%PDF-1.4\nSynthetic manual fixture\n",
                content_type="application/pdf",
            ),
        }
        payload.update(fields)
        return self.client.post(
            reverse("documents"),
            payload,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def _approve(self, document: Document, key: str):
        return self.client.post(
            reverse("document-approve", args=[document.pk]),
            {
                "review_note": "Approved after controlled malware scan.",
                "security_review_reference": "scanner-job-0042",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def _index(self, document: Document) -> None:
        with patch(
            "core.document_library._extract_pdf_pages",
            return_value=Extraction(
                pages=[
                    ExtractedPage(
                        page_number=1,
                        text="Brake torque procedure requires a calibrated wrench.",
                        extraction_method=DocumentPage.ExtractionMethod.EMBEDDED,
                        confidence=None,
                        provenance={"tool": "pdftotext"},
                    )
                ]
            ),
        ):
            self._process_extraction_event(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.INDEXED)

    def _process_extraction_event(self, document: Document) -> None:
        extraction_event = OutboxEvent.objects.get(
            event_type="document.extraction_requested", resource_id=str(document.pk)
        )
        # Extraction emits a follow-up event. Drain the small, known test outbox
        # until this document's actual extraction event is handled, rather than
        # assuming it is globally first.
        for _ in range(3):
            extraction_event.refresh_from_db()
            if extraction_event.processed_at:
                return
            self.assertTrue(Command().process_one())
        self.fail("Document extraction event was not processed")

    def test_document_is_quarantined_then_indexed_with_authorized_grounded_search(self) -> None:
        response = self._upload("00000000-0000-0000-0000-000000000801")
        self.assertEqual(response.status_code, 201)
        document = Document.objects.select_related("attachment").get(
            pk=response.json()["document"]["id"]
        )
        replay = self._upload("00000000-0000-0000-0000-000000000801")
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json()["document"]["id"], str(document.pk))
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(document.status, Document.Status.QUARANTINED)
        self.assertEqual(document.attachment.resource_type, "asset")
        self.assertEqual(document.attachment.resource_id, str(self.asset.pk))
        self.assertEqual(
            self.client.get(
                reverse("attachments"),
                {"resource_type": "asset", "resource_id": str(self.asset.pk)},
            ).json()["attachments"],
            [],
        )

        cast(APIClient, self.client).force_authenticate(self.technician)
        self.assertEqual(
            self.client.get(reverse("documents"), {"asset_id": str(self.asset.pk)}).json()[
                "documents"
            ],
            [],
        )
        cast(APIClient, self.client).force_authenticate(self.manager)
        self.assertEqual(
            self.client.post(
                reverse("document-approve", args=[document.pk]),
                {"review_note": "missing scanner record"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000802",
            ).json()["error"]["code"],
            "security_review_required",
        )
        approved = self._approve(document, "00000000-0000-0000-0000-000000000803")
        self.assertEqual(approved.status_code, 202)
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000803").status_code, 202
        )
        self.assertTrue(
            OutboxEvent.objects.filter(
                event_type="document.extraction_requested", resource_id=str(document.pk)
            ).exists()
        )
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type="document.extraction_requested", resource_id=str(document.pk)
            ).count(),
            1,
        )
        self._index(document)

        cast(APIClient, self.client).force_authenticate(self.technician)
        listed = self.client.get(reverse("documents"), {"asset_id": str(self.asset.pk)})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["documents"][0]["id"], str(document.pk))
        self.assertNotIn("security_review_reference", listed.json()["documents"][0])
        search = self.client.get(reverse("document-search"), {"q": "brake torque"})
        self.assertEqual(search.status_code, 200)
        result = search.json()["results"][0]
        self.assertIn("calibrated wrench", result["excerpt"])
        self.assertEqual(result["citation"]["document_key"], str(document.attachment.document_key))
        self.assertEqual(result["citation"]["page_number"], 1)
        download = self.client.get(reverse("document-download", args=[document.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(
            b"".join(cast(Any, download).streaming_content),
            b"%PDF-1.4\nSynthetic manual fixture\n",
        )
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

        cast(APIClient, self.client).force_authenticate(self.parts_clerk)
        self.assertEqual(
            self.client.get(reverse("document-download", args=[document.pk])).status_code, 403
        )
        self.assertEqual(
            self.client.get(
                reverse("attachment-download", args=[document.attachment.pk])
            ).status_code,
            403,
        )

        scoped_token = ApiToken.objects.create(
            organization=self.organization,
            user=self.technician,
            name="Document-only token",
            prefix="docscope",
            token_hash=hashlib.sha256(b"document-only-token").hexdigest(),
            scopes=["documents.view"],
            expires_at=timezone.now() + timedelta(days=1),
        )
        cast(APIClient, self.client).force_authenticate(self.technician, token=scoped_token)
        self.assertEqual(self.client.get(reverse("documents")).json()["documents"], [])
        self.assertEqual(
            self.client.get(reverse("document-search"), {"q": "brake torque"}).json()["results"],
            [],
        )
        self.assertEqual(
            self.client.get(reverse("document-download", args=[document.pk])).status_code, 403
        )
        self.assertEqual(
            self.client.get(
                reverse("attachment-download", args=[document.attachment.pk])
            ).status_code,
            403,
        )

        cast(APIClient, self.client).force_authenticate(self.outsider)
        self.assertEqual(
            self.client.get(reverse("document-download", args=[document.pk])).status_code, 404
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action="document.extracted", resource_id=str(document.pk), source="worker"
            ).exists()
        )

    def test_replacement_reuses_attachment_lineage_and_generic_attachment_cannot_bypass_it(
        self,
    ) -> None:
        first = self._upload(
            "00000000-0000-0000-0000-000000000811",
            applicability=json.dumps(
                [{"make": "Freightliner", "model": "M2", "engine_type": "Cummins ISB"}]
            ),
        )
        self.assertEqual(first.status_code, 201)
        original = Document.objects.select_related("attachment").get(
            pk=first.json()["document"]["id"]
        )
        generic = self.client.post(
            reverse("attachments"),
            {
                "resource_type": "asset",
                "resource_id": str(self.asset.pk),
                "supersedes_id": str(original.attachment.pk),
                "file": SimpleUploadedFile(
                    "generic.pdf", b"%PDF-1.4\nreplacement", content_type="application/pdf"
                ),
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000812",
        )
        self.assertEqual(generic.status_code, 409)
        self.assertEqual(generic.json()["error"]["code"], "document_replacement_required")

        replacement = self._upload(
            "00000000-0000-0000-0000-000000000813",
            supersedes_document_id=str(original.pk),
        )
        self.assertEqual(replacement.status_code, 201)
        current = Document.objects.select_related("attachment").get(
            pk=replacement.json()["document"]["id"]
        )
        self.assertEqual(current.supersedes_id, original.pk)
        self.assertEqual(current.attachment.document_key, original.attachment.document_key)
        self.assertEqual(current.attachment.version, 2)
        self.assertEqual(current.applicability.count(), 1)
        applicability = current.applicability.get()
        self.assertIsNone(applicability.asset_id)
        self.assertEqual(applicability.make, "Freightliner")
        matching_rows = self._visible_document_rows(self.matching_asset)
        self.assertIn(current.pk, [row.pk for row in matching_rows])

    def test_last_published_revision_stays_visible_while_replacement_is_quarantined(self) -> None:
        first = self._upload("00000000-0000-0000-0000-000000000831")
        original = Document.objects.get(pk=first.json()["document"]["id"])
        self.assertEqual(
            self._approve(original, "00000000-0000-0000-0000-000000000832").status_code, 202
        )
        self._index(original)
        replacement = self._upload(
            "00000000-0000-0000-0000-000000000833",
            supersedes_document_id=str(original.pk),
        )
        current = Document.objects.get(pk=replacement.json()["document"]["id"])

        cast(APIClient, self.client).force_authenticate(self.technician)
        technician_rows = self.client.get(reverse("documents"), {"asset_id": str(self.asset.pk)})
        self.assertEqual(technician_rows.status_code, 200)
        self.assertEqual(technician_rows.json()["documents"][0]["id"], str(original.pk))
        cast(APIClient, self.client).force_authenticate(self.manager)
        manager_rows = self.client.get(reverse("documents"), {"asset_id": str(self.asset.pk)})
        self.assertEqual(
            {row["id"] for row in manager_rows.json()["documents"]},
            {str(original.pk), str(current.pk)},
        )
        stale_approval = self._approve(original, "00000000-0000-0000-0000-000000000834")
        self.assertEqual(stale_approval.status_code, 409)
        self.assertEqual(stale_approval.json()["error"]["code"], "document_not_current")

        self.assertEqual(
            self._approve(current, "00000000-0000-0000-0000-000000000835").status_code, 202
        )
        self._index(current)
        cast(APIClient, self.client).force_authenticate(self.technician)
        technician_rows = self.client.get(reverse("documents"), {"asset_id": str(self.asset.pk)})
        self.assertEqual(technician_rows.json()["documents"][0]["id"], str(current.pk))

    def _visible_document_rows(self, asset: Asset) -> list[Document]:
        cast(APIClient, self.client).force_authenticate(self.manager)
        response = self.client.get(reverse("documents"), {"asset_id": str(asset.pk)})
        return [Document.objects.get(pk=row["id"]) for row in response.json()["documents"]]

    def test_ocr_unavailable_is_explicit_and_page_text_cannot_be_mutated(self) -> None:
        with patch("core.document_library.shutil.which", return_value=None):
            self.assertFalse(ocr_available())
        created = self._upload("00000000-0000-0000-0000-000000000821")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000822").status_code, 202
        )
        with patch(
            "core.document_library._extract_pdf_pages",
            return_value=Extraction(
                pages=[],
                ocr_unavailable=True,
                detail="1 page has no embedded text; OCR tools are unavailable.",
            ),
        ):
            self.assertTrue(Command().process_one())
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.OCR_UNAVAILABLE)
        self.assertIn("OCR tools are unavailable", document.processing_detail)

        indexed = self._upload("00000000-0000-0000-0000-000000000823")
        indexed_document = Document.objects.get(pk=indexed.json()["document"]["id"])
        self.assertEqual(
            self._approve(indexed_document, "00000000-0000-0000-0000-000000000824").status_code,
            202,
        )
        self._index(indexed_document)
        page = DocumentPage.objects.get(document=indexed_document)
        page.text = "tampered"
        with self.assertRaises(ValidationError):
            page.save()
        with self.assertRaises(DatabaseError):
            DocumentPage.objects.filter(pk=page.pk).update(text="tampered")

    def test_unreadable_ocr_result_is_needs_review(self) -> None:
        created = self._upload("00000000-0000-0000-0000-000000000827")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000828").status_code, 202
        )
        with patch(
            "core.document_library._extract_pdf_pages",
            return_value=Extraction(
                pages=[],
                needs_review=True,
                detail="1 page produced no readable OCR text and needs review.",
            ),
        ):
            self._process_extraction_event(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.NEEDS_REVIEW)
        self.assertIn("needs review", document.processing_detail)

    def test_failed_ocr_runtime_is_not_published_as_unavailable(self) -> None:
        with (
            patch("core.document_library.shutil.which", return_value="/usr/bin/tesseract"),
            patch(
                "core.document_library._run_document_command",
                side_effect=DocumentProcessingError("Document processing exceeded its time limit"),
            ),
        ):
            with self.assertRaises(DocumentProcessingError):
                _ocr_runtime_ready(time.monotonic() + 30)

        created = self._upload("00000000-0000-0000-0000-000000000825")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000826").status_code, 202
        )
        with patch(
            "core.document_library._extract_pdf_pages",
            side_effect=DocumentProcessingError("Document processing exceeded its time limit"),
        ):
            self._process_extraction_event(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertNotEqual(document.status, Document.Status.OCR_UNAVAILABLE)

    def test_ocr_bitmap_is_removed_after_each_page(self) -> None:
        def render_fixture(_: Path, __: int, image_path: Path) -> None:
            image_path.write_bytes(b"synthetic OCR bitmap")

        tsv = (
            b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\t"
            b"height\tconf\ttext\n5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t96.5\tTORQUE\n"
        )
        with tempfile.TemporaryDirectory(prefix="fleetline-document-ocr-cleanup-") as directory:
            work_dir = Path(directory)
            with (
                patch("core.document_library.shutil.which", return_value="/usr/bin/tesseract"),
                patch("core.document_library._render_ocr_image", side_effect=render_fixture),
                patch("core.document_library._run_document_command", return_value=tsv),
            ):
                page = _ocr_page(Path("fixture.pdf"), 1, work_dir, time.monotonic() + 30)
                self.assertFalse((work_dir / "page-1.png").exists())
        self.assertIsNotNone(page)

    def test_worker_extracts_embedded_text_from_a_valid_pdf(self) -> None:
        created = self._upload(
            "00000000-0000-0000-0000-000000000841",
            file=SimpleUploadedFile(
                "embedded-manual.pdf",
                _embedded_pdf("Fleetline embedded brake torque manual"),
                content_type="application/pdf",
            ),
        )
        document = Document.objects.get(pk=created.json()["document"]["id"])
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000842").status_code, 202
        )
        self._process_extraction_event(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.INDEXED)
        page = DocumentPage.objects.get(document=document, page_number=1)
        self.assertEqual(page.extraction_method, DocumentPage.ExtractionMethod.EMBEDDED)
        self.assertIn("brake torque", page.text)
        self.assertEqual(page.provenance["tool"], "pypdf")

    def test_parser_child_enforces_the_document_deadline(self) -> None:
        created = self._upload("00000000-0000-0000-0000-000000000843")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        started_at = time.monotonic()
        with (
            override_settings(DOCUMENT_PROCESS_TIMEOUT_SECONDS=0.1),
            patch(
                "core.document_library._extract_pdf_file",
                side_effect=_blocked_pdf_extractor,
            ),
        ):
            with self.assertRaises(DocumentProcessingError):
                _extract_pdf_pages(document.attachment)
        self.assertLess(time.monotonic() - started_at, 1)

    def test_pdfium_renderer_is_bounded_and_creates_a_valid_png(self) -> None:
        pdf_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        image_path = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        pdf_path.close()
        image_path.close()
        self.addCleanup(Path(pdf_path.name).unlink, missing_ok=True)
        self.addCleanup(Path(image_path.name).unlink, missing_ok=True)
        Path(pdf_path.name).write_bytes(_embedded_pdf("PDFium renderer brake manual"))

        _render_ocr_image(Path(pdf_path.name), 1, Path(image_path.name))
        with Image.open(image_path.name) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width * image.height, 0)
        with override_settings(DOCUMENT_MAX_OCR_IMAGE_BYTES=1024):
            with self.assertRaises(DocumentProcessingError):
                _render_ocr_image(Path(pdf_path.name), 1, Path(image_path.name))

    def test_database_guards_reject_cross_tenant_and_destructive_document_mutations(self) -> None:
        created = self._upload("00000000-0000-0000-0000-000000000851")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        other_asset_type = AssetType.objects.create(
            organization=self.other_organization, name="Other Truck"
        )
        other_asset = Asset.objects.create(
            organization=self.other_organization,
            asset_type=other_asset_type,
            home_location=self.other_location,
            unit_number="OTHER-MAN-100",
        )
        bytes_ = b"%PDF-1.4\nother tenant manual\n"
        foreign_attachment = Attachment.objects.create(
            organization=self.other_organization,
            uploader=self.outsider,
            resource_type="asset",
            resource_id=str(other_asset.pk),
            document_key=uuid.uuid4(),
            version=1,
            file=SimpleUploadedFile("other.pdf", bytes_, content_type="application/pdf"),
            original_name="other.pdf",
            content_type="application/pdf",
            size=len(bytes_),
            sha256=hashlib.sha256(bytes_).hexdigest(),
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.create(
                organization=self.organization,
                attachment=foreign_attachment,
                asset=self.asset,
                title="Cross tenant",
                category="service_manual",
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            DocumentApplicability.objects.create(
                organization=self.organization,
                document=document,
                asset=other_asset,
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            DocumentPage.objects.create(
                organization=self.other_organization,
                document=document,
                page_number=1,
                text="cross tenant page",
                extraction_method=DocumentPage.ExtractionMethod.EMBEDDED,
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.filter(pk=document.pk).update(title="Tampered title")
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.filter(pk=document.pk).update(status=Document.Status.INDEXED)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.filter(pk=document.pk).update(
                security_review_reference="forged-review"
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.filter(pk=document.pk).update(processing_detail="forged-result")

        divergent_bytes = b"%PDF-1.4\ndivergent replacement\n"
        divergent_attachment = Attachment.objects.create(
            organization=self.organization,
            uploader=self.manager,
            resource_type="asset",
            resource_id=str(self.asset.pk),
            document_key=document.attachment.document_key,
            version=2,
            file=SimpleUploadedFile(
                "divergent.pdf", divergent_bytes, content_type="application/pdf"
            ),
            original_name="divergent.pdf",
            content_type="application/pdf",
            size=len(divergent_bytes),
            sha256=hashlib.sha256(divergent_bytes).hexdigest(),
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.create(
                organization=self.organization,
                attachment=divergent_attachment,
                asset=self.asset,
                title="Divergent document lineage",
                category="service_manual",
                supersedes=document,
            )

        financial_bytes = b"%PDF-1.4\ncommercial pricing\n"
        financial_attachment = Attachment.objects.create(
            organization=self.organization,
            uploader=self.manager,
            resource_type="asset",
            resource_id=str(self.asset.pk),
            sensitivity=Attachment.Sensitivity.FINANCIAL,
            document_key=uuid.uuid4(),
            version=1,
            file=SimpleUploadedFile(
                "commercial-pricing.pdf", financial_bytes, content_type="application/pdf"
            ),
            original_name="commercial-pricing.pdf",
            content_type="application/pdf",
            size=len(financial_bytes),
            sha256=hashlib.sha256(financial_bytes).hexdigest(),
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            Document.objects.create(
                organization=self.organization,
                attachment=financial_attachment,
                asset=self.asset,
                title="Commercial pricing",
                category="service_manual",
            )

    def test_worker_retries_an_unexpected_failure_without_losing_the_outbox_claim(self) -> None:
        created = self._upload("00000000-0000-0000-0000-000000000861")
        document = Document.objects.get(pk=created.json()["document"]["id"])
        self.assertEqual(
            self._approve(document, "00000000-0000-0000-0000-000000000862").status_code, 202
        )
        event = OutboxEvent.objects.get(
            event_type="document.extraction_requested", resource_id=str(document.pk)
        )
        with patch(
            "core.document_library._extract_pdf_pages", side_effect=RuntimeError("transient")
        ):
            self.assertTrue(Command().process_one())
        event.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(event.attempts, 1)
        self.assertIsNone(event.processed_at)
        self.assertIn("RuntimeError: transient", event.last_error)
        self.assertEqual(document.status, Document.Status.QUEUED)

        event.available_at = timezone.now()
        event.save(update_fields=["available_at", "updated_at"])
        with patch(
            "core.document_library._extract_pdf_pages",
            return_value=Extraction(
                pages=[
                    ExtractedPage(
                        page_number=1,
                        text="retry-safe manual text",
                        extraction_method=DocumentPage.ExtractionMethod.EMBEDDED,
                        confidence=None,
                        provenance={"tool": "pypdf"},
                    )
                ]
            ),
        ):
            self.assertTrue(Command().process_one())
        event.refresh_from_db()
        document.refresh_from_db()
        self.assertIsNotNone(event.processed_at)
        self.assertEqual(document.status, Document.Status.INDEXED)
        self.assertEqual(DocumentPage.objects.filter(document=document).count(), 1)
