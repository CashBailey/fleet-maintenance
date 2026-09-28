from decimal import Decimal
from uuid import uuid4

from assets.models import Asset, AssetType
from core.exceptions import DomainError
from core.models import AuditEvent, Organization, Role, User
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .models import LaborEntry, WorkOrder
from .services import create_labor_entry, create_work_order


class LaborCorrectionTests(TransactionTestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(name="Labor Fleet", slug="labor-fleet")
        asset_type = AssetType.objects.create(organization=self.organization, name="Truck")
        self.asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            unit_number="LAB-1",
        )
        self.technician = self._user("labor-tech", "technician")
        self.supervisor = self._user("labor-supervisor", "supervisor")
        self.fleet_manager = self._user("labor-manager", "fleet_manager")
        self.work_order = create_work_order(
            organization=self.organization,
            actor=self.supervisor,
            asset=self.asset,
            assigned_to=self.technician,
            summary="Labor correction test",
        )

    def _user(self, username: str, role_slug: str) -> User:
        user = User.objects.create_user(username=username, organization=self.organization)
        role = Role.objects.create(
            organization=self.organization,
            slug=role_slug,
            name=role_slug.title(),
        )
        user.roles.add(role)
        return user

    def _record(self, minutes: int = 60, corrects: LaborEntry | None = None) -> LaborEntry:
        return create_labor_entry(
            work_order=self.work_order,
            actor=self.supervisor if corrects else self.technician,
            technician=self.technician,
            minutes=minutes,
            hourly_rate="60",
            note="Corrected from signed time sheet" if corrects else "Initial entry",
            corrects=corrects,
        )

    def test_corrections_are_single_successor_chains_and_totals_use_only_the_leaf(self) -> None:
        original = self._record()
        first_correction = self._record(30, original)
        final_correction = self._record(45, first_correction)

        with self.assertRaises(DomainError) as duplicate:
            self._record(15, original)
        self.assertEqual(duplicate.exception.code, "labor_already_corrected")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LaborEntry.objects.create(
                    organization=self.organization,
                    work_order=self.work_order,
                    technician=self.technician,
                    minutes=15,
                    hourly_rate=Decimal("60"),
                    note="Competing correction",
                    corrects=original,
                )

        client = APIClient()
        client.force_authenticate(self.fleet_manager)
        detail = client.get(f"/api/v1/maintenance/work-orders/{self.work_order.pk}/")
        report = client.get("/api/v1/reports/operations/")

        self.assertEqual((detail.status_code, report.status_code), (200, 200))
        payload = detail.json()["work_order"]
        entries = {entry["id"]: entry for entry in payload["labor_entries"]}
        self.assertEqual(payload["current_labor_minutes"], 45)
        self.assertEqual(payload["current_labor_cost"], "45.00")
        self.assertFalse(entries[str(original.pk)]["is_current"])
        self.assertEqual(entries[str(original.pk)]["superseded_by_id"], str(first_correction.pk))
        self.assertFalse(entries[str(first_correction.pk)]["is_current"])
        self.assertTrue(entries[str(final_correction.pk)]["is_current"])
        self.assertEqual(report.json()["summary"]["labor_cost"], "45.00")
        self.assertEqual(
            AuditEvent.objects.filter(
                organization=self.organization, action="labor.corrected"
            ).count(),
            2,
        )

    def test_terminal_state_is_rechecked_under_lock_and_reopened_work_accepts_labor(self) -> None:
        original = self._record()
        stale_work_order = self.work_order

        for status in ("Completed", "Closed", "Cancelled"):
            WorkOrder.objects.filter(pk=self.work_order.pk).update(status=status)
            with self.assertRaises(DomainError) as create_error:
                create_labor_entry(
                    work_order=stale_work_order,
                    actor=self.technician,
                    technician=self.technician,
                    minutes=10,
                )
            self.assertEqual(create_error.exception.code, "work_order_not_editable")
            with self.assertRaises(DomainError) as correction_error:
                self._record(50, original)
            self.assertEqual(correction_error.exception.code, "work_order_not_editable")

        WorkOrder.objects.filter(pk=self.work_order.pk).update(status="Reopened")
        accepted = create_labor_entry(
            work_order=stale_work_order,
            actor=self.technician,
            technician=self.technician,
            minutes=10,
        )
        self.assertEqual(accepted.minutes, 10)

    def test_only_supervisors_correct_labor_and_the_original_technician_is_preserved(self) -> None:
        original = self._record()
        path = f"/api/v1/maintenance/work-orders/{self.work_order.pk}/labor/"
        payload = {
            "minutes": 50,
            "note": "Corrected from supervisor review",
            "corrects_id": str(original.pk),
        }
        client = APIClient()
        client.force_authenticate(self.technician)
        denied = client.post(path, payload, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(denied.status_code, 403)

        client.force_authenticate(self.fleet_manager)
        corrected = client.post(path, payload, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(corrected.status_code, 201)
        self.assertEqual(corrected.json()["labor_entry"]["technician_id"], str(self.technician.pk))

    def test_database_rejects_update_and_delete_bypasses(self) -> None:
        entry = self._record()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                LaborEntry.objects.filter(pk=entry.pk).update(minutes=1)
        with self.assertRaises(DatabaseError):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("DELETE FROM maintenance_laborentry WHERE id = %s", [entry.pk])
        self.assertTrue(LaborEntry.objects.filter(pk=entry.pk, minutes=60).exists())
