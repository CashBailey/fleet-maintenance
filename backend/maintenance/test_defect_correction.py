from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from assets.models import Asset, AssetType
from core.models import Attachment, AuditEvent, Location, Organization, Role, User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Defect, MaintenanceRequest, WorkOrder


class TechnicianDefectCorrectionApiTests(TestCase):
    organization: Organization
    location: Location
    asset: Asset
    technician: User
    other_technician: User
    supervisor: User
    client: Any
    defect: Defect
    work_order: WorkOrder
    evidence: Attachment

    @classmethod
    def setUpTestData(cls) -> None:
        cls.organization = Organization.objects.create(name="Repair Fleet", slug="repair-fleet")
        cls.location = Location.objects.create(
            organization=cls.organization, name="Main Shop", code="MAIN"
        )
        asset_type = AssetType.objects.create(organization=cls.organization, name="Truck")
        cls.asset = Asset.objects.create(
            organization=cls.organization,
            asset_type=asset_type,
            home_location=cls.location,
            unit_number="REPAIR-01",
        )
        technician_role = Role.objects.create(
            organization=cls.organization, slug="technician", name="Technician"
        )
        supervisor_role = Role.objects.create(
            organization=cls.organization, slug="supervisor", name="Supervisor"
        )
        cls.technician = User.objects.create_user(
            username="assigned-technician", organization=cls.organization
        )
        cls.technician.roles.add(technician_role)
        cls.other_technician = User.objects.create_user(
            username="other-technician", organization=cls.organization
        )
        cls.other_technician.roles.add(technician_role)
        cls.supervisor = User.objects.create_user(
            username="repair-supervisor", organization=cls.organization
        )
        cls.supervisor.roles.add(supervisor_role)

    def setUp(self) -> None:
        self.client = APIClient()
        self.defect = Defect.objects.create(
            organization=self.organization,
            asset=self.asset,
            reported_by=self.supervisor,
            category="brakes",
            description="Right rear service brake drags",
            severity="safety",
            safety_related=True,
            status="InRepair",
        )
        request = MaintenanceRequest.objects.create(
            organization=self.organization,
            asset=self.asset,
            defect=self.defect,
            submitted_by=self.supervisor,
            status="Converted",
            priority="safety",
            summary="Repair right rear brake",
        )
        self.work_order = WorkOrder.objects.create(
            organization=self.organization,
            number=f"WO-{uuid.uuid4()}",
            asset=self.asset,
            request=request,
            created_by=self.supervisor,
            assigned_to=self.technician,
            status="InProgress",
            priority="safety",
            summary="Repair right rear brake",
        )
        self.evidence = Attachment.objects.create(
            organization=self.organization,
            uploader=self.technician,
            resource_type="Defect",
            resource_id=str(self.defect.pk),
            file="attachments/test/brake-after-repair.jpg",
            original_name="brake-after-repair.jpg",
            content_type="image/jpeg",
            size=128,
            sha256="a" * 64,
        )

    def post_transition(
        self, user: User, payload: Mapping[str, object], *, key: str | None = None
    ) -> Any:
        self.client.force_authenticate(user)
        return self.client.post(
            reverse("defect-transition", args=[self.defect.pk]),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        )

    def test_assigned_technician_documents_correction_but_supervisor_verifies(self) -> None:
        key = str(uuid.uuid4())
        payload = {
            "status": "Corrected",
            "repair_details": "Replaced seized caliper and verified free wheel rotation.",
            "evidence_attachment_ids": [str(self.evidence.pk)],
        }

        response = self.post_transition(self.technician, payload, key=key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defect"]["status"], "Corrected")
        self.assertEqual(
            response.data["repair_evidence"],
            {
                "repair_details": payload["repair_details"],
                "attachment_ids": [str(self.evidence.pk)],
            },
        )
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "Corrected")
        self.assertEqual(self.defect.disposition_reason, payload["repair_details"])

        transition = AuditEvent.objects.get(
            organization=self.organization,
            resource_type="Defect",
            resource_id=str(self.defect.pk),
            action="defect.transitioned",
            new_state="Corrected",
        )
        repair_record = AuditEvent.objects.get(
            organization=self.organization,
            resource_type="Defect",
            resource_id=str(self.defect.pk),
            action="defect.repair_documented",
        )
        self.assertEqual(transition.actor_id, self.technician.pk)
        self.assertEqual(transition.context["reason"], payload["repair_details"])
        self.assertEqual(repair_record.actor_id, self.technician.pk)
        self.assertEqual(repair_record.context["attachment_ids"], [str(self.evidence.pk)])
        self.assertEqual(repair_record.correlation_id, key)

        replay = self.post_transition(self.technician, payload, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.organization,
                resource_type="Defect",
                resource_id=str(self.defect.pk),
                action="defect.repair_documented",
            ).count(),
            1,
        )

        denied = self.post_transition(self.technician, {"status": "Verified"})
        self.assertEqual(denied.status_code, 403)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "Corrected")

        verified = self.post_transition(self.supervisor, {"status": "Verified"})
        self.assertEqual(verified.status_code, 200)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "Verified")
        self.assertEqual(
            AuditEvent.objects.get(pk=repair_record.pk).context["repair_details"],
            payload["repair_details"],
        )

    def test_unassigned_technician_cannot_correct_defect(self) -> None:
        response = self.post_transition(
            self.other_technician,
            {"status": "Corrected", "repair_details": "Not my assigned repair"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "permission_denied")
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "InRepair")

    def test_correction_requires_details_and_evidence_linked_to_same_defect(self) -> None:
        missing_details = self.post_transition(self.technician, {"status": "Corrected"})
        self.assertEqual(missing_details.status_code, 400)
        self.assertEqual(missing_details.data["error"]["code"], "repair_details_required")

        unrelated_defect = Defect.objects.create(
            organization=self.organization,
            asset=self.asset,
            reported_by=self.supervisor,
            category="lighting",
            description="Marker lamp out",
            status="InRepair",
        )
        unrelated_evidence = Attachment.objects.create(
            organization=self.organization,
            uploader=self.technician,
            resource_type="Defect",
            resource_id=str(unrelated_defect.pk),
            file="attachments/test/unrelated.jpg",
            original_name="unrelated.jpg",
            content_type="image/jpeg",
            size=64,
            sha256="b" * 64,
        )
        wrong_evidence = self.post_transition(
            self.technician,
            {
                "status": "Corrected",
                "repair_details": "Repaired brake",
                "evidence_attachment_ids": [str(unrelated_evidence.pk)],
            },
        )
        self.assertEqual(wrong_evidence.status_code, 400)
        self.assertEqual(wrong_evidence.data["error"]["code"], "invalid_repair_evidence")
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "InRepair")

    def test_cross_organization_defect_is_not_disclosed(self) -> None:
        other_org = Organization.objects.create(name="Other Fleet", slug="other-repair-fleet")
        other_location = Location.objects.create(
            organization=other_org, name="Other Shop", code="MAIN"
        )
        other_type = AssetType.objects.create(organization=other_org, name="Truck")
        other_asset = Asset.objects.create(
            organization=other_org,
            asset_type=other_type,
            home_location=other_location,
            unit_number="OTHER-01",
        )
        outsider = User.objects.create_user(username="other-reporter", organization=other_org)
        other_defect = Defect.objects.create(
            organization=other_org,
            asset=other_asset,
            reported_by=outsider,
            category="brakes",
            description="Other fleet brake",
            status="InRepair",
        )
        self.client.force_authenticate(self.technician)

        response = self.client.post(
            reverse("defect-transition", args=[other_defect.pk]),
            {"status": "Corrected", "repair_details": "Should not be visible"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        self.assertEqual(response.status_code, 404)
        other_defect.refresh_from_db()
        self.assertEqual(other_defect.status, "InRepair")
