from __future__ import annotations

from assets.models import Asset, AssetType
from core.models import Location, Organization, User
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase

from .models import Inspection, InspectionResponse, InspectionTemplate
from .services import create_inspection, void_inspection_record


class InspectionDatabaseImmutabilityTests(TransactionTestCase):
    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Inspection immutability triggers require PostgreSQL")
        self.organization = Organization.objects.create(name="Guard Fleet", slug="guard-fleet")
        self.location = Location.objects.create(
            organization=self.organization, name="Main", code="MAIN"
        )
        self.user = User.objects.create_user(
            username="inspection-guard", organization=self.organization
        )
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            home_location=self.location,
            unit_number="GUARD-1",
        )
        self.template = InspectionTemplate.objects.create(
            organization=self.organization,
            created_by=self.user,
            name="Pre-trip",
            questions=[{"id": "brakes", "label": "Brakes", "required": True}],
        )
        self.inspection = create_inspection(
            organization=self.organization,
            actor=self.user,
            asset=self.asset,
            template=self.template,
            responses=[{"question_id": "brakes", "result": "pass"}],
            acknowledgment="Attested",
        )
        self.response = self.inspection.responses.get()

    def test_queryset_updates_cannot_bypass_submitted_immutability(self) -> None:
        with self.assertRaises(DatabaseError), transaction.atomic():
            Inspection.objects.filter(pk=self.inspection.pk).update(
                acknowledgment="silently changed"
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            InspectionResponse.objects.filter(pk=self.response.pk).update(notes="silently changed")

        self.inspection.refresh_from_db()
        self.response.refresh_from_db()
        self.assertEqual(self.inspection.acknowledgment, "Attested")
        self.assertEqual(self.response.notes, "")

    def test_raw_sql_cannot_delete_submitted_inspection_or_response(self) -> None:
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM maintenance_inspectionresponse WHERE id = %s",
                [self.response.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM maintenance_inspection WHERE id = %s",
                [self.inspection.pk],
            )

        self.assertTrue(Inspection.objects.filter(pk=self.inspection.pk).exists())
        self.assertTrue(InspectionResponse.objects.filter(pk=self.response.pk).exists())

    def test_bulk_create_cannot_append_response_after_submission(self) -> None:
        duplicate = InspectionResponse(
            organization=self.organization,
            inspection=self.inspection,
            question_id="tires",
            question="Tires",
            result="pass",
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            InspectionResponse.objects.bulk_create([duplicate])

        self.assertEqual(self.inspection.responses.count(), 1)

    def test_void_transition_remains_available_then_record_is_frozen(self) -> None:
        void_inspection_record(
            inspection=self.inspection,
            actor=self.user,
            reason="Wrong asset selected",
        )
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, "Voided")

        with self.assertRaises(DatabaseError), transaction.atomic():
            Inspection.objects.filter(pk=self.inspection.pk).update(void_reason="silently changed")
