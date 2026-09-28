from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid

from assets.models import Asset, AssetType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from inventory.models import Bin, Part, Warehouse
from purchasing.models import PurchaseOrder, PurchaseOrderLine, Receipt, Vendor
from rest_framework.test import APIClient

from .models import Attachment, Location, Organization, Role, User


class FinancialAttachmentAccessTests(TestCase):
    """Opaque attachment bytes respect their immutable server-side classification."""

    def setUp(self) -> None:
        self.media_dir = tempfile.mkdtemp(prefix="fleetline-financial-attachment-test-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_dir, True)

        self.organization = Organization.objects.create(name="Attachment Fleet", slug="att-fin")
        self.location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )
        roles = {
            slug: Role.objects.create(
                organization=self.organization, slug=slug, name=slug.replace("_", " ").title()
            )
            for slug in ("technician", "parts_clerk", "purchasing_manager")
        }
        self.technician = self._user("attachment-technician", roles["technician"])
        self.parts_clerk = self._user("attachment-parts", roles["parts_clerk"])
        self.purchasing_manager = self._user("attachment-purchasing", roles["purchasing_manager"])
        asset_type = AssetType.objects.create(organization=self.organization, name="Truck")
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            home_location=self.location,
            unit_number="ATT-101",
        )
        self.part = Part.objects.create(
            organization=self.organization, number="ATT-FILTER", name="Attachment filter"
        )
        self.vendor = Vendor.objects.create(
            organization=self.organization, code="ATT-VENDOR", name="Attachment Vendor"
        )
        self.purchase_order = PurchaseOrder.objects.create(
            organization=self.organization,
            number="PO-ATT-101",
            vendor=self.vendor,
            created_by=self.purchasing_manager,
        )
        self.purchase_order_line = PurchaseOrderLine.objects.create(
            organization=self.organization,
            purchase_order=self.purchase_order,
            part=self.part,
            description="Attachment filter",
            quantity_ordered="1.000",
            unit_cost="12.5000",
        )
        warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=self.location,
            code="MAIN",
            name="Main warehouse",
        )
        self.bin = Bin.objects.create(
            organization=self.organization, warehouse=warehouse, code="A-01", name="Filters"
        )
        self.receipt = Receipt.objects.create(
            organization=self.organization,
            purchase_order=self.purchase_order,
            number="RCPT-ATT-101",
            operation_id=uuid.uuid4(),
            received_by=self.parts_clerk,
        )

    def _user(self, username: str, role: Role) -> User:
        user = User.objects.create_user(
            username=username,
            organization=self.organization,
            default_location=self.location,
        )
        user.roles.add(role)
        return user

    @staticmethod
    def _client(user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _upload(
        self,
        user: User,
        resource_type: str,
        resource_id: object,
        *,
        sensitivity: str | None,
        name: str,
    ):
        payload: dict[str, object] = {
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "file": SimpleUploadedFile(name, b"attachment bytes", content_type="text/plain"),
            "title": name,
        }
        if sensitivity is not None:
            payload["sensitivity"] = sensitivity
        return self._client(user).post(
            reverse("attachments"),
            payload,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    def test_financial_attachments_are_hidden_from_operational_roles(self) -> None:
        missing = self._upload(
            self.parts_clerk,
            "part",
            self.part.pk,
            sensitivity=None,
            name="unclassified.txt",
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["error"]["code"], "attachment_sensitivity_required")

        invalid = self._upload(
            self.parts_clerk,
            "part",
            self.part.pk,
            sensitivity="restricted",
            name="invalid.txt",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_attachment_sensitivity")

        operational_part = self._upload(
            self.parts_clerk,
            "part",
            self.part.pk,
            sensitivity="operational",
            name="filter-specification.txt",
        )
        self.assertEqual(operational_part.status_code, 201, operational_part.content)
        operational_id = operational_part.json()["attachment"]["id"]

        denied_financial_upload = self._upload(
            self.parts_clerk,
            "part",
            self.part.pk,
            sensitivity="financial",
            name="supplier-quote.txt",
        )
        self.assertEqual(denied_financial_upload.status_code, 403)
        self.assertEqual(
            denied_financial_upload.json()["error"]["code"], "financial_permission_denied"
        )

        financial_part = self._upload(
            self.purchasing_manager,
            "part",
            self.part.pk,
            sensitivity="financial",
            name="supplier-quote.txt",
        )
        financial_purchase_order = self._upload(
            self.purchasing_manager,
            "PurchaseOrder",
            self.purchase_order.pk,
            sensitivity="financial",
            name="purchase-order-invoice.txt",
        )
        financial_receipt = self._upload(
            self.purchasing_manager,
            "Receipt",
            self.receipt.pk,
            sensitivity="financial",
            name="receipt-invoice.txt",
        )
        for response in (financial_part, financial_purchase_order, financial_receipt):
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.json()["attachment"]["sensitivity"], "financial")

        financial_part_id = financial_part.json()["attachment"]["id"]
        financial_purchase_order_id = financial_purchase_order.json()["attachment"]["id"]
        financial_receipt_id = financial_receipt.json()["attachment"]["id"]
        vendor_quote = Attachment.objects.create(
            organization=self.organization,
            uploader=self.purchasing_manager,
            resource_type="Vendor",
            resource_id=str(self.vendor.pk),
            sensitivity=Attachment.Sensitivity.FINANCIAL,
            file=SimpleUploadedFile("vendor-quote.txt", b"quote", content_type="text/plain"),
            original_name="vendor-quote.txt",
            content_type="text/plain",
            size=5,
            sha256=hashlib.sha256(b"quote").hexdigest(),
        )
        staged_financial = Attachment.objects.create(
            organization=self.organization,
            uploader=self.parts_clerk,
            resource_type="stock",
            resource_id=str(uuid.uuid4()),
            sensitivity=Attachment.Sensitivity.FINANCIAL,
            file=SimpleUploadedFile("staged-price.txt", b"price", content_type="text/plain"),
            original_name="staged-price.txt",
            content_type="text/plain",
            size=5,
            sha256=hashlib.sha256(b"price").hexdigest(),
        )

        technician = self._client(self.technician)
        part_rows = technician.get(
            reverse("attachments"), {"resource_type": "part", "resource_id": str(self.part.pk)}
        )
        self.assertEqual(part_rows.status_code, 200, part_rows.content)
        self.assertEqual([row["id"] for row in part_rows.json()["attachments"]], [operational_id])
        self.assertEqual(
            technician.get(reverse("attachment-download", args=[operational_id])).status_code, 200
        )
        self.assertEqual(
            technician.get(reverse("attachment-download", args=[financial_part_id])).status_code,
            403,
        )

        parts_clerk = self._client(self.parts_clerk)
        self.assertEqual(
            parts_clerk.get(
                reverse("attachments"),
                {"resource_type": "Vendor", "resource_id": str(self.vendor.pk)},
            ).status_code,
            403,
        )
        self.assertEqual(
            parts_clerk.get(reverse("attachment-download", args=[vendor_quote.pk])).status_code,
            403,
        )
        self.assertEqual(
            parts_clerk.get(reverse("attachment-download", args=[staged_financial.pk])).status_code,
            403,
        )
        for resource_type, resource_id, attachment_id in (
            ("PurchaseOrder", self.purchase_order.pk, financial_purchase_order_id),
            ("Receipt", self.receipt.pk, financial_receipt_id),
        ):
            listed = parts_clerk.get(
                reverse("attachments"),
                {"resource_type": resource_type, "resource_id": str(resource_id)},
            )
            self.assertEqual(listed.status_code, 200, listed.content)
            self.assertEqual(listed.json()["attachments"], [])
            self.assertEqual(
                parts_clerk.get(reverse("attachment-download", args=[attachment_id])).status_code,
                403,
            )

        purchasing_manager = self._client(self.purchasing_manager)
        for resource_type, resource_id, attachment_id in (
            ("part", self.part.pk, financial_part_id),
            ("PurchaseOrder", self.purchase_order.pk, financial_purchase_order_id),
            ("Receipt", self.receipt.pk, financial_receipt_id),
        ):
            listed = purchasing_manager.get(
                reverse("attachments"),
                {"resource_type": resource_type, "resource_id": str(resource_id)},
            )
            self.assertEqual(listed.status_code, 200, listed.content)
            self.assertIn(attachment_id, [row["id"] for row in listed.json()["attachments"]])
            self.assertEqual(
                purchasing_manager.get(
                    reverse("attachment-download", args=[attachment_id])
                ).status_code,
                200,
            )

        replacement = self._client(self.parts_clerk).post(
            reverse("attachments"),
            {
                "resource_type": "part",
                "resource_id": str(self.part.pk),
                "sensitivity": "operational",
                "supersedes_id": financial_part_id,
                "file": SimpleUploadedFile(
                    "quote-v2.txt", b"replacement", content_type="text/plain"
                ),
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(replacement.status_code, 409)
        self.assertEqual(replacement.json()["error"]["code"], "attachment_sensitivity_mismatch")
        financial_document_key = financial_part.json()["attachment"]["document_key"]
        self.assertEqual(Attachment.objects.filter(document_key=financial_document_key).count(), 1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Attachment.objects.filter(pk=financial_part_id).update(
                sensitivity=Attachment.Sensitivity.OPERATIONAL
            )
