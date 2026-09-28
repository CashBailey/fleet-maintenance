from __future__ import annotations

import uuid
from decimal import Decimal

from assets.models import Asset, AssetType
from core.models import Location, Organization, User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from maintenance.models import WorkOrder

from .models import Bin, Part, Reservation, Warehouse


class ReservationDatabaseInvariantTests(TestCase):
    def setUp(self) -> None:
        self.organization = Organization.objects.create(
            name="Reservation Guard", slug="reservation-guard"
        )
        self.user = User.objects.create(
            username="reservation-guard", organization=self.organization
        )
        location = Location.objects.create(
            organization=self.organization, name="Main Shop", code="MAIN"
        )
        asset_type = AssetType.objects.create(
            organization=self.organization, name="Truck", category="vehicle"
        )
        asset = Asset.objects.create(
            organization=self.organization,
            asset_type=asset_type,
            home_location=location,
            unit_number="GUARD-01",
        )
        work_order = WorkOrder.objects.create(
            organization=self.organization,
            number="WO-RESERVATION-GUARD",
            asset=asset,
            created_by=self.user,
            summary="Reservation constraint test",
        )
        warehouse = Warehouse.objects.create(
            organization=self.organization,
            location=location,
            code="MAIN",
            name="Main Warehouse",
        )
        stock_bin = Bin.objects.create(
            organization=self.organization,
            warehouse=warehouse,
            code="A-01",
            name="Guard Bin",
        )
        part = Part.objects.create(
            organization=self.organization,
            number="GUARD-PART",
            name="Guarded part",
        )
        self.reservation = Reservation.objects.create(
            organization=self.organization,
            part=part,
            bin=stock_bin,
            work_order=work_order,
            requested_quantity=Decimal("5.000"),
            issued_quantity=Decimal("3.000"),
            released_quantity=Decimal("2.000"),
            status=Reservation.Status.FULFILLED,
            operation_id=uuid.uuid4(),
            created_by=self.user,
        )

    def test_raw_sql_cannot_overconsume_a_reservation(self) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE inventory_reservation SET issued_quantity = %s WHERE id = %s",
                        [Decimal("3.001"), self.reservation.pk],
                    )

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.issued_quantity, Decimal("3.000"))
        self.assertEqual(self.reservation.remaining_quantity, Decimal("0.000"))
